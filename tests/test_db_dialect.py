"""Dialect-layer tests.

The Postgres path is the deployed one, so it must not be exercised for the first
time in production. The generation tests below need no database; the round-trip
tests run the real suite against Postgres whenever DATABASE_URL is set.
"""

from __future__ import annotations

import os

import pytest

from gtm.db import Connection, connect, is_postgres_url


class Recorder:
    """Captures SQL instead of executing it."""

    def __init__(self):
        self.sql: list[tuple[str, tuple]] = []

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.sql.append((sql, tuple(params)))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass


def pg() -> tuple[Connection, Recorder]:
    rec = Recorder()
    return Connection(rec, "postgres"), rec


def lite() -> tuple[Connection, Recorder]:
    rec = Recorder()
    return Connection(rec, "sqlite"), rec


# ------------------------------------------------------------------ url


@pytest.mark.parametrize("url,expected", [
    ("postgresql://u:p@host/db", True),
    ("postgres://u:p@host/db", True),
    ("data/gtm.db", False),
    ("/tmp/x.sqlite", False),
])
def test_url_detection(url, expected):
    assert is_postgres_url(url) is expected


# -------------------------------------------------------- placeholders


def test_placeholders_translated_for_postgres_only():
    conn, rec = pg()
    conn.execute("SELECT * FROM accounts WHERE account_id=? AND status=?", ("a", "active"))
    assert rec.sql[0][0] == "SELECT * FROM accounts WHERE account_id=%s AND status=%s"

    conn, rec = lite()
    conn.execute("SELECT * FROM accounts WHERE account_id=?", ("a",))
    assert rec.sql[0][0] == "SELECT * FROM accounts WHERE account_id=?"


def test_schema_has_no_literal_percent_that_psycopg_would_misread():
    """psycopg treats % as a format marker; a stray one in DDL would break."""
    from gtm.db import _schema

    assert "%" not in _schema(pg=True)


# --------------------------------------------------------------- upsert


def test_upsert_uses_on_conflict_for_postgres():
    conn, rec = pg()
    conn.upsert("extractions",
                {"doc_id": "d1", "content_hash": "h", "claim_count": 3},
                pk=["doc_id", "content_hash"])
    sql, params = rec.sql[0]
    assert "ON CONFLICT (doc_id, content_hash) DO UPDATE SET" in sql
    # Only non-key columns are updated — rewriting the key is a no-op at best.
    assert "claim_count=EXCLUDED.claim_count" in sql
    assert "doc_id=EXCLUDED.doc_id" not in sql
    assert params == ("d1", "h", 3)


def test_upsert_uses_insert_or_replace_for_sqlite():
    conn, rec = lite()
    conn.upsert("extractions", {"doc_id": "d1", "content_hash": "h"},
                pk=["doc_id", "content_hash"])
    assert rec.sql[0][0].startswith("INSERT OR REPLACE INTO extractions")


def test_insert_returning_uses_returning_clause_on_postgres():
    conn, rec = pg()

    class WithRow(Recorder):
        def fetchone(self):
            return {"run_id": 42}

    rec2 = WithRow()
    conn = Connection(rec2, "postgres")
    run_id = conn.insert_returning("ingest_runs", {"started_at": "t"}, returning="run_id")
    assert "RETURNING run_id" in rec2.sql[0][0]
    assert run_id == 42


def test_insert_returning_uses_lastrowid_on_sqlite():
    class WithLastRow(Recorder):
        lastrowid = 7

    rec = WithLastRow()
    conn = Connection(rec, "sqlite")
    assert conn.insert_returning("ingest_runs", {"started_at": "t"}, returning="run_id") == 7
    assert "RETURNING" not in rec.sql[0][0]


# ------------------------------------------------- live Postgres round-trip

pgtest = pytest.mark.skipif(
    not is_postgres_url(os.getenv("DATABASE_URL")),
    reason="set DATABASE_URL to a postgres:// URL to run the round-trip tests",
)


@pgtest
def test_postgres_roundtrip_ingest_and_tombstone():
    """The full L1 cycle against real Postgres: load, no-op, then a removal."""
    from pathlib import Path

    from gtm.claims import active_claims, persist_claims
    from gtm.ingest import ingest
    from gtm.models import Claim
    from gtm.sources.fixture import FixtureAdapter

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    conn = connect(os.environ["DATABASE_URL"])
    for table in ("claims", "extractions", "document_versions", "documents",
                  "usage_periods", "change_events", "ingest_runs", "accounts"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()

    delta = ingest(conn, FixtureAdapter(fixtures / "snapshot_a"))
    assert len(delta.new_docs) == 4

    assert ingest(conn, FixtureAdapter(fixtures / "snapshot_a")).is_empty

    persist_claims(conn, [Claim(
        account_id="acct_demo_01", claim_type="risk", subject="s", value="v",
        source_doc_id="doc_a3", verbatim_quote="Priya has gone quiet since the P1")])
    assert len(active_claims(conn, "acct_demo_01")) == 1

    delta = ingest(conn, FixtureAdapter(fixtures / "snapshot_b"))
    assert delta.removed_docs == ["doc_a3"]
    assert active_claims(conn, "acct_demo_01") == []
    conn.close()
