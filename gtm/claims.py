"""Claim persistence and the evidence guard.

The extraction engine (L2) is an LLM and will occasionally invent a quote that
reads plausibly but appears nowhere in the source. `persist_claims` refuses
those. This is the cheapest, highest-value check in the system: it converts
"the evidence trail is a feature we hope holds" into an invariant enforced at
write time.

Rejections are counted, not silently swallowed -- a rejection rate is a health
metric for the prompt, and a non-zero one is worth reporting honestly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from gtm.db import Connection
from gtm.models import Claim, utcnow


def _normalize(text: str) -> str:
    """Collapse whitespace so a quote that differs only in line wrapping still
    matches. Anything looser than this would defeat the point of the check."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


@dataclass
class PersistResult:
    inserted: int = 0
    updated: int = 0
    rejected: int = 0
    rejections: list[tuple[str, str]] = field(default_factory=list)  # (claim_type, reason)

    @property
    def attempted(self) -> int:
        return self.inserted + self.updated + self.rejected

    @property
    def rejection_rate(self) -> float:
        return self.rejected / self.attempted if self.attempted else 0.0


def persist_claims(conn: Connection, claims: list[Claim]) -> PersistResult:
    res = PersistResult()
    now = utcnow()

    for c in claims:
        doc = conn.execute(
            "SELECT body, content_hash, status FROM documents WHERE doc_id=?", (c.source_doc_id,)
        ).fetchone()

        if doc is None:
            res.rejected += 1
            res.rejections.append((c.claim_type, f"unknown source_doc_id {c.source_doc_id}"))
            continue
        if not c.verbatim_quote.strip():
            res.rejected += 1
            res.rejections.append((c.claim_type, "empty verbatim_quote"))
            continue
        if _normalize(c.verbatim_quote) not in _normalize(doc["body"]):
            res.rejected += 1
            res.rejections.append((c.claim_type, "quote not found in source document"))
            continue

        cid = c.claim_id()
        existing = conn.execute("SELECT claim_id FROM claims WHERE claim_id=?", (cid,)).fetchone()
        if existing:
            conn.execute(
                """UPDATE claims SET status='active', retracted_at=NULL, retraction_reason=NULL,
                   confidence=?, source_content_hash=? WHERE claim_id=?""",
                (c.confidence, doc["content_hash"], cid),
            )
            res.updated += 1
        else:
            conn.execute(
                """INSERT INTO claims (claim_id, account_id, claim_type, subject, value, confidence,
                   source_doc_id, source_content_hash, verbatim_quote, doc_date, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,'active',?)""",
                (cid, c.account_id, c.claim_type, c.subject, c.value, c.confidence,
                 c.source_doc_id, doc["content_hash"], c.verbatim_quote, c.doc_date, now),
            )
            res.inserted += 1

    conn.commit()
    return res


def active_claims(conn: Connection, account_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT c.*, d.title AS source_title, d.doc_type AS source_type
           FROM claims c JOIN documents d ON d.doc_id = c.source_doc_id
           WHERE c.account_id=? AND c.status='active' ORDER BY c.doc_date DESC""",
        (account_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def claim_set_hash(conn: Connection, account_id: str) -> str:
    """Fingerprint of an account's active evidence. If this is unchanged, the
    account's synthesis cannot have changed, so L3 skips it -- this is what
    makes the 4:30 re-run cost pennies instead of a full portfolio re-read."""
    from gtm.models import content_hash

    rows = conn.execute(
        "SELECT claim_id FROM claims WHERE account_id=? AND status='active' ORDER BY claim_id",
        (account_id,),
    ).fetchall()
    return content_hash([r["claim_id"] for r in rows])
