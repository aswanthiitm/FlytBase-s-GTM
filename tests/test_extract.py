"""Tests for L2.

The extractor is injected, so the whole pipeline is exercised here with no
Anthropic credential and no network. The stubs deliberately include a
hallucinating extractor and a crashing one -- both are things a real model does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gtm.claims import active_claims
from gtm.db import connect
from gtm.extract import ExtractedClaim, ExtractionResult, pending_documents, run_extraction
from gtm.ingest import ingest
from gtm.sources.fixture import FixtureAdapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "x.db")
    ingest(c, FixtureAdapter(FIXTURES / "snapshot_a"))
    yield c
    c.close()


def quoting_extractor(doc):
    """Well-behaved: quotes the first sentence of whatever it is given."""
    first = doc["body"].split(".")[0]
    return ExtractionResult(claims=[
        ExtractedClaim(claim_type="sentiment", subject="tone", value="a claim",
                       confidence=0.7, verbatim_quote=first)
    ])


def hallucinating_extractor(doc):
    return ExtractionResult(claims=[
        ExtractedClaim(claim_type="risk", subject="invented",
                       value="Customer is about to churn",
                       verbatim_quote="we are cancelling effective immediately")
    ])


def exploding_extractor(doc):
    raise RuntimeError("upstream 529 overloaded")


def test_extraction_persists_claims(conn):
    report = run_extraction(conn, quoting_extractor, max_workers=2)
    assert report.documents_considered == 4
    assert report.documents_extracted == 4
    assert report.claims_inserted == 4
    assert report.claims_rejected == 0
    assert len(active_claims(conn, "acct_demo_01")) == 3


def test_second_run_is_fully_cached(conn):
    """The property the 4:30 update depends on: unchanged documents cost nothing."""
    run_extraction(conn, quoting_extractor)
    assert pending_documents(conn) == []

    report = run_extraction(conn, exploding_extractor)  # would fail if it ran
    assert report.documents_considered == 0
    assert report.documents_extracted == 0
    assert report.documents_failed == 0


def test_only_changed_documents_are_re_extracted(conn):
    run_extraction(conn, quoting_extractor)
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_b"))

    pending = {d["doc_id"] for d in pending_documents(conn)}
    # doc_a2 was edited, doc_a4 is new. doc_a1 and doc_b1 are untouched, and
    # doc_a3 was removed upstream so it is not a candidate at all.
    assert pending == {"doc_a2", "doc_a4"}

    report = run_extraction(conn, quoting_extractor)
    assert report.documents_extracted == 2


def test_hallucinated_quotes_are_rejected_and_counted(conn):
    report = run_extraction(conn, hallucinating_extractor)
    assert report.claims_inserted == 0
    assert report.claims_rejected == 4
    assert report.rejection_rate == 1.0
    assert active_claims(conn, "acct_demo_01") == []


def test_document_with_all_claims_rejected_is_not_retried(conn):
    """Re-reading it would produce the same rejections at the same cost."""
    run_extraction(conn, hallucinating_extractor)
    assert pending_documents(conn) == []


def test_one_failing_document_does_not_stop_the_run(conn):
    def flaky(doc):
        if doc["doc_id"] == "doc_a2":
            raise RuntimeError("529 overloaded")
        return quoting_extractor(doc)

    report = run_extraction(conn, flaky, max_workers=2)
    assert report.documents_failed == 1
    assert report.documents_extracted == 3
    assert report.errors[0][0] == "doc_a2"

    # The failed document stays pending, so the next run retries just that one.
    assert {d["doc_id"] for d in pending_documents(conn)} == {"doc_a2"}


def test_extraction_can_be_scoped_to_touched_accounts(conn):
    """L2 only re-reads the accounts the delta named."""
    report = run_extraction(conn, quoting_extractor, account_ids={"acct_demo_02"})
    assert report.documents_extracted == 1
    assert len(active_claims(conn, "acct_demo_01")) == 0
    assert len(active_claims(conn, "acct_demo_02")) == 1


def test_removed_document_claims_retract_after_extraction(conn):
    """L1 and L2 compose: extract, then lose the source upstream."""
    run_extraction(conn, quoting_extractor)
    assert len(active_claims(conn, "acct_demo_01")) == 3

    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_b"))

    remaining = {c["source_doc_id"] for c in active_claims(conn, "acct_demo_01")}
    assert "doc_a3" not in remaining  # removed upstream
    assert "doc_a2" not in remaining  # edited, so its old claim is stale
    assert remaining == {"doc_a1"}
