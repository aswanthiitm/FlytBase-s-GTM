"""Dashboard rendering tests.

The renderers are called directly — no socket, no HTTP. What matters is that a
page never crashes on the shapes real data actually takes (an account with no
usage, a removed document, an unextracted account) and that user-controlled text
is escaped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gtm.claims import persist_claims
from gtm.db import connect
from gtm.ingest import ingest
from gtm.models import Claim
from gtm.server import render_account, render_feed, render_portfolio
from gtm.sources.fixture import FixtureAdapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "s.db")
    ingest(c, FixtureAdapter(FIXTURES / "snapshot_a"))
    yield c
    c.close()


def test_portfolio_renders_every_account(conn):
    html = render_portfolio(conn)
    assert "Northwind Utilities" in html
    assert "Cobalt Mining Co" in html
    assert html.startswith("<!doctype html>")


def test_portfolio_surfaces_high_severity_divergence(conn):
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_b"))  # adds the 96h month
    html = render_portfolio(conn)
    assert "disagrees with the aircraft" in html
    assert "health label overstates usage" in html


def test_account_without_usage_does_not_crash(conn):
    """Pre-sale accounts legitimately have no usage history at all."""
    html = render_account(conn, "acct_demo_02")
    assert "Cobalt Mining Co" in html
    assert "Flight activity" not in html


def test_account_with_no_claims_says_so_rather_than_showing_nothing(conn):
    html = render_account(conn, "acct_demo_01")
    assert "No claims extracted yet" in html


def test_claim_evidence_is_shown_with_its_source(conn):
    persist_claims(conn, [Claim(
        account_id="acct_demo_01", claim_type="risk", subject="champion",
        value="Champion has gone quiet", source_doc_id="doc_a3",
        verbatim_quote="Priya has gone quiet since the P1", doc_date="2026-03-05")])
    html = render_account(conn, "acct_demo_01")
    assert "Champion has gone quiet" in html
    assert "Priya has gone quiet since the P1" in html
    assert "Internal - renewal risk" in html  # the source document


def test_removed_document_is_marked_in_the_drilldown(conn):
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_b"))  # removes doc_a3
    html = render_account(conn, "acct_demo_01")
    assert "removed upstream" in html
    assert "revised r2" in html  # doc_a2 was edited


def test_unknown_account_is_a_page_not_an_exception(conn):
    assert "No such account" in render_account(conn, "does-not-exist")


def test_feed_shows_runs_and_their_trigger(conn):
    html = render_feed(conn)
    assert "Change feed" in html
    assert "manual" in html
    assert "New account tracked" in html


def test_user_text_is_escaped(conn):
    """Document titles and claim text come from upstream; they are not trusted."""
    conn.execute("UPDATE accounts SET name=? WHERE account_id=?",
                 ("<script>alert(1)</script>", "acct_demo_01"))
    conn.commit()
    html = render_portfolio(conn)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
