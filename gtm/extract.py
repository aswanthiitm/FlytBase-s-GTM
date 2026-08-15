"""L2 -- document to claims.

Idempotent and incremental. A document is extracted once per content hash; a
poll that finds nothing changed does no LLM work and costs nothing. When the
4:30 batch lands, only the new and edited documents are re-read, which is why
the whole re-run finishes in seconds rather than re-processing the portfolio.

The extractor is injected rather than hard-wired to the Anthropic client, so the
whole pipeline is testable end to end without a credential.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from gtm.claims import PersistResult, persist_claims
from gtm.db import Connection
from gtm.models import Claim, utcnow

ClaimType = Literal[
    "contact",           # a named person and their role
    "risk",              # something that could cost us the account
    "opportunity",       # expansion, upsell, new use case
    "blocker",           # what stands in the way of an opportunity or renewal
    "commitment",        # something we or they promised
    "objection",         # a stated reason not to buy or renew
    "competitor",        # a named alternative in play
    "renewal_signal",    # evidence about renewal intent, timing, or terms
    "churn_reason",      # why a lost account left
    "sentiment",         # expressed satisfaction or frustration
    "product_feedback",  # a request, gap, or praise about the product
    "usage_signal",      # a statement about how much or how they are flying
    "commercial",        # pricing, contract value, procurement, budget
]


class ExtractedClaim(BaseModel):
    """What the model is allowed to return. Deliberately narrow."""

    claim_type: ClaimType
    subject: str | None = Field(
        default=None,
        description="Who or what this is about: a person's name, a feature, a site.",
    )
    value: str = Field(description="The assertion itself, in one sentence.")
    confidence: float = Field(
        default=0.6, ge=0.0, le=1.0,
        description="0.9 if stated outright; 0.5 if implied; below 0.4 do not emit it.",
    )
    verbatim_quote: str = Field(
        description="Text copied EXACTLY from the document that supports this claim.",
    )


class ExtractionResult(BaseModel):
    claims: list[ExtractedClaim] = Field(default_factory=list)


EXTRACTION_SYSTEM = """You extract evidence from customer-account documents for a \
GTM team at FlytBase, a company selling autonomous drone operations software \
(docks, BVLOS flights, fleet management).

You return CLAIMS. A claim is one atomic assertion supported by one exact quote \
from the document you were given.

Rules, in order of importance:

1. Every claim MUST carry a `verbatim_quote` copied character-for-character from \
the document. Do not paraphrase, do not tidy up grammar, do not join two \
sentences with an ellipsis. If you cannot find an exact supporting span, do not \
make the claim. A claim whose quote does not appear in the source is discarded \
and counts as a failure.

2. Extract what the document says, not what it implies about the wider account. \
If a transcript says a champion is frustrated, that is a claim. If you think that \
means they will churn, that is not -- inference happens downstream, where it can \
be weighed against every other document.

3. Prefer several precise claims over one broad one. "Priya Raman is Head of \
Inspections" and "Priya Raman wants a third site by Q3" are two claims.

4. Contradictions are signal, not noise. If the document contradicts something it \
says elsewhere, extract both sides. Never reconcile them.

5. Name people exactly as the document names them, including their stated role. \
The CRM contact list is thin and incomplete; the real decision-makers usually \
appear only in transcripts and email threads.

6. Do not compute anything. No arithmetic on flight hours, revenue, dates, or \
percentages. Quote the number as written and let deterministic code do the maths.

7. If the document supports no claims, return an empty list. An empty list is a \
valid, useful answer. Do not manufacture claims to seem thorough."""


def build_user_prompt(doc: Any) -> str:
    return (
        f"Account: {doc['account_id']}\n"
        f"Document type: {doc['doc_type']}\n"
        f"Title: {doc['title'] or '(untitled)'}\n"
        f"Date: {doc['doc_date'] or '(undated)'}\n"
        f"--- BEGIN DOCUMENT ---\n{doc['body']}\n--- END DOCUMENT ---\n\n"
        "Extract the claims this document supports."
    )


# Extractor signature: takes a document row, returns the model's parsed result.
Extractor = Callable[[Any], ExtractionResult]


def groq_extractor(client=None) -> Extractor:
    from gtm.llm import get_client, parse_structured

    resolved = client or get_client()

    def _extract(doc: Any) -> ExtractionResult:
        return parse_structured(
            resolved,
            system=EXTRACTION_SYSTEM,
            user=build_user_prompt(doc),
            schema=ExtractionResult,
        )

    return _extract


# --------------------------------------------------------------------------


@dataclass
class ExtractionReport:
    documents_considered: int = 0
    documents_extracted: int = 0
    documents_skipped_cached: int = 0
    documents_failed: int = 0
    claims_inserted: int = 0
    claims_updated: int = 0
    claims_rejected: int = 0
    rejections: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def rejection_rate(self) -> float:
        total = self.claims_inserted + self.claims_updated + self.claims_rejected
        return self.claims_rejected / total if total else 0.0

    def summary(self) -> str:
        return (
            f"{self.documents_extracted} extracted, "
            f"{self.documents_skipped_cached} cached, "
            f"{self.documents_failed} failed -> "
            f"{self.claims_inserted} new claims, {self.claims_updated} refreshed, "
            f"{self.claims_rejected} rejected ({self.rejection_rate:.0%})"
        )


def pending_documents(
    conn: Connection, account_ids: set[str] | None = None
) -> list[Any]:
    """Active documents with no extraction recorded at their current hash.

    The hash join is the whole caching story: edit a document and it reappears
    here; leave it alone and it never does.
    """
    sql = """
        SELECT d.* FROM documents d
        LEFT JOIN extractions e
          ON e.doc_id = d.doc_id AND e.content_hash = d.content_hash
        WHERE d.status = 'active' AND e.doc_id IS NULL
    """
    params: list = []
    if account_ids:
        sql += f" AND d.account_id IN ({','.join('?' * len(account_ids))})"
        params.extend(sorted(account_ids))
    sql += " ORDER BY d.account_id, d.doc_date"
    return conn.execute(sql, params).fetchall()


def _record_extraction(conn: Connection, doc: Any, n_claims: int,
                       model: str) -> None:
    conn.upsert("extractions", {
        "doc_id": doc["doc_id"],
        "content_hash": doc["content_hash"],
        "extracted_at": utcnow(),
        "claim_count": n_claims,
        "model": model,
    }, pk=["doc_id", "content_hash"])


def run_extraction(
    conn: Connection,
    extractor: Extractor,
    account_ids: set[str] | None = None,
    max_workers: int = 2,
    model_label: str = "unknown",
) -> ExtractionReport:
    """Extract every pending document, in parallel, and persist the claims.

    Documents are embarrassingly independent, so the fan-out is free
    parallelism. Persistence stays single-threaded -- SQLite connections are not
    shared across threads, and the write is microseconds against a network call.
    """
    report = ExtractionReport()
    docs = pending_documents(conn, account_ids)
    report.documents_considered = len(docs)
    if not docs:
        return report

    def _safe(doc: Any):
        try:
            return doc, extractor(doc), None
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
            return doc, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_safe, docs))

    for doc, result, error in results:
        if error is not None:
            report.documents_failed += 1
            report.errors.append((doc["doc_id"], error))
            continue

        claims = [
            Claim(
                account_id=doc["account_id"],
                claim_type=c.claim_type,
                subject=c.subject,
                value=c.value,
                confidence=c.confidence,
                source_doc_id=doc["doc_id"],
                verbatim_quote=c.verbatim_quote,
                doc_date=doc["doc_date"],
            )
            for c in result.claims
        ]
        persisted: PersistResult = persist_claims(conn, claims)

        report.documents_extracted += 1
        report.claims_inserted += persisted.inserted
        report.claims_updated += persisted.updated
        report.claims_rejected += persisted.rejected
        report.rejections.extend(persisted.rejections)

        # Record the extraction even when every claim was rejected: the document
        # was read at this hash, and re-reading it would produce the same
        # rejections at the same cost.
        _record_extraction(conn, doc, persisted.inserted + persisted.updated, model_label)

    conn.commit()
    return report
