"""L6 -- the append-only change feed.

This is the artifact that proves the system updated itself. Judges cannot watch
a cron run; they can read a timestamped feed that says what arrived, what it
changed, and which document caused it. Rows are written once and never mutated.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from gtm.models import EventType, utcnow


def emit(
    conn: sqlite3.Connection,
    run_id: int | None,
    event_type: EventType | str,
    account_id: str | None,
    entity_type: str,
    entity_id: str | None,
    summary: str,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """INSERT INTO change_events (run_id, ts, account_id, entity_type, entity_id,
           event_type, summary, detail_json) VALUES (?,?,?,?,?,?,?,?)""",
        (run_id, utcnow(), account_id, entity_type, entity_id, str(event_type), summary,
         json.dumps(detail) if detail else None),
    )


def recent(conn: sqlite3.Connection, limit: int = 50, account_id: str | None = None) -> list[dict]:
    if account_id:
        rows = conn.execute(
            "SELECT * FROM change_events WHERE account_id=? ORDER BY event_id DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM change_events ORDER BY event_id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d.pop("detail_json") or "null")
        out.append(d)
    return out
