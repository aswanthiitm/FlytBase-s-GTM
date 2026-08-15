"""SQLite store.

Design notes that matter:

* Documents are *versioned*, not overwritten. When a doc's content changes we
  keep the prior body in document_versions so the change feed can show a real
  before/after instead of asserting that something changed.

* Nothing is ever hard-deleted. A document that disappears upstream is
  tombstoned (status='removed'); the claims it supported are retracted, not
  dropped. That preserves the audit trail and makes the retraction itself a
  reportable event.

* change_events is append-only. It is the user-visible proof that the system
  updated itself without a human in the loop.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

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
    flight_hours  REAL NOT NULL DEFAULT 0,
    missions      INTEGER NOT NULL DEFAULT 0,
    content_hash  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (account_id, period)
);

-- L2: evidence units. Retracted rather than deleted when their source dies.
CREATE TABLE IF NOT EXISTS claims (
    claim_id            TEXT PRIMARY KEY,
    account_id          TEXT NOT NULL,
    claim_type          TEXT NOT NULL,
    subject             TEXT,
    value               TEXT NOT NULL,
    confidence          REAL NOT NULL DEFAULT 0.5,
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

-- L2 cache: which documents have been read, at which content hash. The join
-- against documents.content_hash is what makes a re-poll cost nothing.
CREATE TABLE IF NOT EXISTS extractions (
    doc_id       TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    claim_count  INTEGER NOT NULL DEFAULT 0,
    model        TEXT,
    PRIMARY KEY (doc_id, content_hash)
);

-- L3: per-account synthesis, versioned so we can show score movement.
CREATE TABLE IF NOT EXISTS dossiers (
    account_id     TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    health_score   REAL,
    payload_json   TEXT NOT NULL,
    claim_set_hash TEXT NOT NULL,
    generated_at   TEXT NOT NULL,
    run_id         INTEGER,
    PRIMARY KEY (account_id, revision)
);

-- L4: portfolio-wide output (NBA queue, forecast, expansion register).
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    revision     INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    run_id       INTEGER
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
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

-- L6: append-only change feed. Never updated, never deleted.
CREATE TABLE IF NOT EXISTS change_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
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


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
