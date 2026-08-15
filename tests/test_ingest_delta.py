"""The gate tests for L1.

If these pass, the 4:30 PM update works mechanically. Every one of them maps to
a way the system could silently fail on the day.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gtm.claims import active_claims, persist_claims
from gtm.db import connect
from gtm.ingest import ingest
from gtm.models import AccountSnapshot, Claim
from gtm.sources.fixture import FixtureAdapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def _count(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_first_ingest_loads_everything(conn):
    delta = ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    assert len(delta.new_accounts) == 2
    assert len(delta.new_docs) == 4
    assert len(delta.usage_changed) == 2
    assert _count(conn, "SELECT COUNT(*) FROM documents WHERE status='active'") == 4


def test_second_identical_run_is_a_noop(conn):
    """The idempotency gate. If this fails, every poll rewrites the world and
    the change feed becomes noise nobody can read."""
    adapter = FixtureAdapter(FIXTURES / "snapshot_a")
    ingest(conn, adapter)
    events_after_first = _count(conn, "SELECT COUNT(*) FROM change_events")

    delta = ingest(conn, adapter)
    assert delta.is_empty
    assert delta.touched_accounts == set()
    assert _count(conn, "SELECT COUNT(*) FROM change_events") == events_after_first


def test_update_batch_detects_new_changed_and_removed(conn):
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    delta = ingest(conn, FixtureAdapter(FIXTURES / "snapshot_b"))

    assert delta.new_docs == ["doc_a4"]
    assert delta.changed_docs == ["doc_a2"]
    assert delta.removed_docs == ["doc_a3"]
    assert "acct_demo_01:2026-03" in delta.usage_changed
    assert "acct_demo_02" in delta.changed_accounts  # stage prospect -> negotiation

    row = conn.execute("SELECT status, removed_at FROM documents WHERE doc_id='doc_a3'").fetchone()
    assert row["status"] == "removed"
    assert row["removed_at"] is not None

    # Edited document keeps its history rather than overwriting it.
    assert _count(conn, "SELECT COUNT(*) FROM document_versions WHERE doc_id='doc_a2'") == 2


def test_removed_document_retracts_its_claims(conn):
    """T6: deletion must withdraw the conclusions the document supported, not
    just hide the document."""
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    res = persist_claims(conn, [
        Claim(
            account_id="acct_demo_01",
            claim_type="risk",
            subject="champion_disengaged",
            value="Champion has gone quiet since the P1 incident",
            source_doc_id="doc_a3",
            verbatim_quote="Priya has gone quiet since the P1",
            doc_date="2026-03-05",
        )
    ])
    assert res.inserted == 1
    assert len(active_claims(conn, "acct_demo_01")) == 1

    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_b"))

    assert active_claims(conn, "acct_demo_01") == []
    row = conn.execute("SELECT status, retraction_reason FROM claims").fetchone()
    assert row["status"] == "retracted"
    assert "removed upstream" in row["retraction_reason"]
    # Retracted, not deleted -- the audit trail survives.
    assert _count(conn, "SELECT COUNT(*) FROM claims") == 1


def test_restored_document_reinstates_identical_claims(conn):
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    persist_claims(conn, [
        Claim(account_id="acct_demo_01", claim_type="risk", subject="champion_disengaged",
              value="Champion quiet", source_doc_id="doc_a3",
              verbatim_quote="Priya has gone quiet since the P1", doc_date="2026-03-05")
    ])
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_b"))
    assert active_claims(conn, "acct_demo_01") == []

    delta = ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    assert delta.restored_docs == ["doc_a3"]
    assert len(active_claims(conn, "acct_demo_01")) == 1


def test_edited_document_invalidates_stale_claims(conn):
    """A claim extracted from r1 must not keep vouching for r2's text."""
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    persist_claims(conn, [
        Claim(account_id="acct_demo_01", claim_type="risk", subject="outage",
              value="Dock offline 4 days", source_doc_id="doc_a2",
              verbatim_quote="Dock 2 offline for 4 days", doc_date="2026-03-02")
    ])
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_b"))

    row = conn.execute("SELECT status, retraction_reason FROM claims").fetchone()
    assert row["status"] == "retracted"
    assert "revised" in row["retraction_reason"]


class _BrokenAdapter:
    """Upstream is up enough to list accounts, but detail fetches fail."""

    name = "broken"

    def __init__(self, accounts):
        self._accounts = accounts

    def list_accounts(self):
        return self._accounts

    def fetch_account(self, account):
        return AccountSnapshot(account=account, complete=False, error="502 upstream")


def test_partial_fetch_never_tombstones(conn):
    """The failure that would have destroyed the 4:30 demo: a transient upstream
    error reads as 'every document was deleted' and wipes the portfolio."""
    good = FixtureAdapter(FIXTURES / "snapshot_a")
    ingest(conn, good)
    assert _count(conn, "SELECT COUNT(*) FROM documents WHERE status='active'") == 4

    delta = ingest(conn, _BrokenAdapter(good.list_accounts()))

    assert delta.removed_docs == []
    assert _count(conn, "SELECT COUNT(*) FROM documents WHERE status='active'") == 4
    assert len(delta.incomplete_accounts) == 2
    assert _count(conn, "SELECT COUNT(*) FROM ingest_runs WHERE status='partial'") == 1


def test_hallucinated_quote_is_rejected(conn):
    """T2: a claim whose evidence does not appear in the source never lands."""
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    res = persist_claims(conn, [
        Claim(account_id="acct_demo_01", claim_type="risk", subject="fabricated",
              value="Customer threatened to cancel",
              source_doc_id="doc_a1",
              verbatim_quote="we are cancelling our contract immediately"),
    ])
    assert res.inserted == 0
    assert res.rejected == 1
    assert "quote not found" in res.rejections[0][1]
    assert _count(conn, "SELECT COUNT(*) FROM claims") == 0


def test_quote_matching_tolerates_whitespace_reflow(conn):
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    res = persist_claims(conn, [
        Claim(account_id="acct_demo_01", claim_type="opportunity", subject="third_site",
              value="Wants a third site by Q3", source_doc_id="doc_a1",
              verbatim_quote="want a third\n   site by Q3"),
    ])
    assert res.inserted == 1
