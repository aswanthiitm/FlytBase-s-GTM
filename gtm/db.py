"""Store, with a thin dialect layer over SQLite and Postgres.

Why both: Postgres is the deployed store — durable across redeploys, and it lets
the poller and the dashboard run as separate services. SQLite stays supported so
the test suite runs offline in milliseconds. The same schema and the same call
sites drive both, and `pytest` runs against Postgres too when DATABASE_URL is
set, so the production dialect is never untested.

Design notes that carry over from the SQLite-only version:

* Documents are versioned, not overwritten, so the change feed can show a real
  before/after instead of asserting that something changed.
* Nothing is hard-deleted. A document that vanishes upstream is tombstoned and
  the claims it supported are retracted — the audit trail survives.
* change_events is append-only. It is the user-visible proof that the system
  updated itself with no human in the loop.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Sequence

POSTGRES_PREFIXES = ("postgres://", "postgresql://")


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and str(url).startswith(POSTGRES_PREFIXES)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def _schema(pg: bool) -> str:
    serial = "BIGSERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    real = "DOUBLE PRECISION" if pg else "REAL"
    return f"""
CREATE TABLE IF NOT EXISTS accounts (
    account_id     TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    stage          TEXT,
    content_hash   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    removed_at     TEXT,
    raw_json       TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id         TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL,
    doc_type       TEXT NOT NULL,
    title          TEXT,
    doc_date       TEXT,
    body           TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    revision       INTEGER NOT NULL DEFAULT 1,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    removed_at     TEXT,
    raw_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_account ON documents(account_id, status);

CREATE TABLE IF NOT EXISTS document_versions (
    doc_id        TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    body          TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    PRIMARY KEY (doc_id, revision)
);

CREATE TABLE IF NOT EXISTS usage_periods (
    account_id    TEXT NOT NULL,
    period        TEXT NOT NULL,
    flight_hours  {real} NOT NULL DEFAULT 0,
    missions      INTEGER NOT NULL DEFAULT 0,
    content_hash  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (account_id, period)
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id            TEXT PRIMARY KEY,
    account_id          TEXT NOT NULL,
    claim_type          TEXT NOT NULL,
    subject             TEXT,
    value               TEXT NOT NULL,
    confidence          {real} NOT NULL DEFAULT 0.5,
    source_doc_id       TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    verbatim_quote      TEXT NOT NULL,
    doc_date            TEXT,
    status              TEXT NOT NULL DEFAULT 'active',
    created_at          TEXT NOT NULL,
    retracted_at        TEXT,
    retraction_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_account ON claims(account_id, status);
CREATE INDEX IF NOT EXISTS idx_claims_doc ON claims(source_doc_id);

CREATE TABLE IF NOT EXISTS extractions (
    doc_id       TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    claim_count  INTEGER NOT NULL DEFAULT 0,
    model        TEXT,
    PRIMARY KEY (doc_id, content_hash)
);

CREATE TABLE IF NOT EXISTS dossiers (
    account_id     TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    health_score   {real},
    payload_json   TEXT NOT NULL,
    claim_set_hash TEXT NOT NULL,
    generated_at   TEXT NOT NULL,
    run_id         INTEGER,
    PRIMARY KEY (account_id, revision)
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    revision     {serial},
    payload_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    run_id       INTEGER
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id           {serial},
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL DEFAULT 'running',
    trigger          TEXT NOT NULL DEFAULT 'manual',
    source           TEXT,
    docs_seen        INTEGER DEFAULT 0,
    docs_new         INTEGER DEFAULT 0,
    docs_changed     INTEGER DEFAULT 0,
    docs_removed     INTEGER DEFAULT 0,
    docs_restored    INTEGER DEFAULT 0,
    accounts_touched INTEGER DEFAULT 0,
    error            TEXT
);

CREATE TABLE IF NOT EXISTS change_events (
    event_id    {serial},
    run_id      INTEGER,
    ts          TEXT NOT NULL,
    account_id  TEXT,
    entity_type TEXT NOT NULL,
    entity_id   TEXT,
    event_type  TEXT NOT NULL,
    summary     TEXT NOT NULL,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON change_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_account ON change_events(account_id, ts DESC);
"""


# --------------------------------------------------------------------------
# Connection wrapper
# --------------------------------------------------------------------------


class Connection:
    """Uniform surface over sqlite3 and psycopg.

    Call sites keep writing `?` placeholders and reading rows with `row["col"]`;
    the wrapper translates for Postgres. Rows are dict-like in both dialects, so
    `dict(row)` and `row.keys()` work either way.
    """

    def __init__(self, raw: Any, dialect: str):
        self._raw = raw
        self.dialect = dialect

    @property
    def is_postgres(self) -> bool:
        return self.dialect == "postgres"

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_postgres else sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self.is_postgres:
            cur = self._raw.cursor()
            cur.execute(self._sql(sql), tuple(params))
            return cur
        return self._raw.execute(sql, tuple(params))

    def executescript(self, script: str) -> None:
        if self.is_postgres:
            with self._raw.cursor() as cur:
                cur.execute(script)
            self._raw.commit()
        else:
            self._raw.executescript(script)

    def upsert(self, table: str, data: dict[str, Any], pk: list[str]) -> None:
        """Dialect-correct INSERT-or-REPLACE.

        SQLite's `INSERT OR REPLACE` and Postgres' `ON CONFLICT DO UPDATE` are
        not interchangeable syntax, so the difference lives here rather than at
        every call site.
        """
        cols = list(data)
        placeholders = ", ".join("?" * len(cols))
        if self.is_postgres:
            updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in pk)
            sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
                   f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {updates}")
        else:
            sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        self.execute(sql, list(data.values()))

    def insert_returning(self, table: str, data: dict[str, Any], returning: str) -> Any:
        """Insert and get the generated key. SQLite exposes `lastrowid`;
        Postgres needs an explicit RETURNING clause."""
        cols = list(data)
        placeholders = ", ".join("?" * len(cols))
        base = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        if self.is_postgres:
            cur = self.execute(f"{base} RETURNING {returning}", list(data.values()))
            return cur.fetchone()[returning]
        cur = self.execute(base, list(data.values()))
        return cur.lastrowid

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        self._raw.close()


# --------------------------------------------------------------------------


def connect(target: str | Path | None = None) -> Connection:
    """Open the store. A postgres:// URL selects Postgres; anything else is
    treated as a SQLite file path."""
    target = str(target if target is not None else
                 os.getenv("DATABASE_URL") or os.getenv("GTM_DB_PATH", "data/gtm.db"))

    if is_postgres_url(target):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # noqa: TRY003
            raise RuntimeError("psycopg is not installed — `uv pip install 'psycopg[binary]'`") from exc

        # Managed Postgres (Railway, Neon, Supabase) requires TLS.
        if "sslmode=" not in target:
            target += ("&" if "?" in target else "?") + "sslmode=require"
        raw = psycopg.connect(target, row_factory=dict_row, autocommit=False)
        conn = Connection(raw, "postgres")
    else:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(path, timeout=30)
        raw.row_factory = sqlite3.Row
        raw.executescript("PRAGMA journal_mode=WAL;\nPRAGMA foreign_keys=ON;")
        conn = Connection(raw, "sqlite")

    conn.executescript(_schema(conn.is_postgres))
    conn.commit()
    return conn
