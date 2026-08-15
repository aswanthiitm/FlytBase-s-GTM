"""L1 -- ingest and reconcile. No LLM anywhere in this file.

One pass does four things:
  1. fetch the current upstream snapshot, per account
  2. diff it against what we already hold, by content hash
  3. tombstone whatever vanished, and retract the claims it supported
  4. write every difference to the append-only change feed

Rule that shapes the whole module: *absence is only evidence of deletion when
the fetch that produced the absence was complete.* Every reconcile step is
gated on AccountSnapshot.complete.
"""

from __future__ import annotations

import json
from typing import Any

from gtm.changefeed import emit
from gtm.db import Connection
from gtm.models import (
    AccountSnapshot,
    ClaimStatus,
    Delta,
    DocStatus,
    EventType,
    RawAccount,
    utcnow,
)
from gtm.sources.base import SourceAdapter


def start_run(conn: Connection, trigger: str, source: str) -> int:
    run_id = conn.insert_returning(
        "ingest_runs",
        {"started_at": utcnow(), "trigger": trigger, "source": source},
        returning="run_id",
    )
    conn.commit()
    return int(run_id)


def finish_run(conn: Connection, run_id: int, delta: Delta, status: str, error: str | None = None) -> None:
    conn.execute(
        """UPDATE ingest_runs SET finished_at=?, status=?, docs_new=?, docs_changed=?,
           docs_removed=?, docs_restored=?, accounts_touched=?, error=? WHERE run_id=?""",
        (
            utcnow(),
            status,
            len(delta.new_docs),
            len(delta.changed_docs),
            len(delta.removed_docs),
            len(delta.restored_docs),
            len(delta.touched_accounts),
            error,
            run_id,
        ),
    )
    conn.commit()


# --------------------------------------------------------------------------


def _upsert_account(conn: Connection, acct: RawAccount, delta: Delta) -> None:
    now = utcnow()
    fp = acct.fingerprint()
    row = conn.execute(
        "SELECT content_hash, status FROM accounts WHERE account_id=?", (acct.account_id,)
    ).fetchone()

    if row is None:
        conn.execute(
            """INSERT INTO accounts (account_id, name, stage, content_hash, status,
               first_seen_at, last_seen_at, updated_at, raw_json)
               VALUES (?,?,?,?,'active',?,?,?,?)""",
            (acct.account_id, acct.name, acct.stage, fp, now, now, now, json.dumps(acct.raw)),
        )
        delta.new_accounts.append(acct.account_id)
        delta.touched_accounts.add(acct.account_id)
        emit(conn, delta.run_id, EventType.ACCOUNT_NEW, acct.account_id, "account",
             acct.account_id, f"New account tracked: {acct.name}")
        return

    if row["content_hash"] != fp:
        prior = conn.execute(
            "SELECT name, stage FROM accounts WHERE account_id=?", (acct.account_id,)
        ).fetchone()
        conn.execute(
            """UPDATE accounts SET name=?, stage=?, content_hash=?, status='active',
               last_seen_at=?, updated_at=?, removed_at=NULL, raw_json=? WHERE account_id=?""",
            (acct.name, acct.stage, fp, now, now, json.dumps(acct.raw), acct.account_id),
        )
        delta.changed_accounts.append(acct.account_id)
        delta.touched_accounts.add(acct.account_id)
        changed_stage = prior["stage"] != acct.stage
        summary = (
            f"Stage changed: {prior['stage']} -> {acct.stage}"
            if changed_stage
            else "CRM record updated"
        )
        emit(conn, delta.run_id, EventType.ACCOUNT_CHANGED, acct.account_id, "account",
             acct.account_id, summary,
             {"before": {"stage": prior["stage"]}, "after": {"stage": acct.stage}})
    else:
        conn.execute("UPDATE accounts SET last_seen_at=? WHERE account_id=?", (now, acct.account_id))


def _upsert_documents(conn: Connection, snap: AccountSnapshot, delta: Delta) -> set[str]:
    """Insert/update documents. Returns the set of doc_ids seen this pass."""
    now = utcnow()
    seen: set[str] = set()
    aid = snap.account.account_id

    for doc in snap.documents:
        seen.add(doc.doc_id)
        fp = doc.fingerprint()
        row = conn.execute(
            "SELECT content_hash, status, revision, body FROM documents WHERE doc_id=?",
            (doc.doc_id,),
        ).fetchone()

        if row is None:
            conn.execute(
                """INSERT INTO documents (doc_id, account_id, doc_type, title, doc_date, body,
                   content_hash, status, revision, first_seen_at, last_seen_at, updated_at, raw_json)
                   VALUES (?,?,?,?,?,?,?,'active',1,?,?,?,?)""",
                (doc.doc_id, aid, str(doc.doc_type), doc.title, doc.doc_date, doc.body,
                 fp, now, now, now, json.dumps(doc.raw)),
            )
            conn.execute(
                """INSERT INTO document_versions (doc_id, revision, content_hash, body, captured_at)
                   VALUES (?,1,?,?,?)""",
                (doc.doc_id, fp, doc.body, now),
            )
            delta.new_docs.append(doc.doc_id)
            delta.touched_accounts.add(aid)
            emit(conn, delta.run_id, EventType.DOC_NEW, aid, "document", doc.doc_id,
                 f"New {doc.doc_type}: {doc.title or doc.doc_id}",
                 {"doc_type": str(doc.doc_type), "doc_date": doc.doc_date,
                  "chars": len(doc.body)})
            continue

        if row["status"] == DocStatus.REMOVED:
            # Came back from the dead.
            rev = int(row["revision"]) + 1
            conn.execute(
                """UPDATE documents SET status='active', removed_at=NULL, body=?, content_hash=?,
                   doc_type=?, title=?, doc_date=?, revision=?, last_seen_at=?, updated_at=?, raw_json=?
                   WHERE doc_id=?""",
                (doc.body, fp, str(doc.doc_type), doc.title, doc.doc_date, rev, now, now,
                 json.dumps(doc.raw), doc.doc_id),
            )
            conn.upsert("document_versions", {
                "doc_id": doc.doc_id, "revision": rev, "content_hash": fp,
                "body": doc.body, "captured_at": now,
            }, pk=["doc_id", "revision"])
            reinstated = _reinstate_claims(conn, doc.doc_id, fp)
            delta.restored_docs.append(doc.doc_id)
            delta.touched_accounts.add(aid)
            emit(conn, delta.run_id, EventType.DOC_RESTORED, aid, "document", doc.doc_id,
                 f"Document restored upstream: {doc.title or doc.doc_id}",
                 {"claims_reinstated": reinstated})
            if reinstated:
                emit(conn, delta.run_id, EventType.CLAIMS_REINSTATED, aid, "claims", doc.doc_id,
                     f"{reinstated} claim(s) reinstated from restored document")
            continue

        if row["content_hash"] != fp:
            rev = int(row["revision"]) + 1
            conn.execute(
                """UPDATE documents SET body=?, content_hash=?, doc_type=?, title=?, doc_date=?,
                   revision=?, last_seen_at=?, updated_at=?, raw_json=? WHERE doc_id=?""",
                (doc.body, fp, str(doc.doc_type), doc.title, doc.doc_date, rev, now, now,
                 json.dumps(doc.raw), doc.doc_id),
            )
            conn.upsert("document_versions", {
                "doc_id": doc.doc_id, "revision": rev, "content_hash": fp,
                "body": doc.body, "captured_at": now,
            }, pk=["doc_id", "revision"])
            # Claims were extracted from the *old* text; they no longer have a
            # verifiable source until re-extraction runs against the new body.
            stale = _retract_claims(
                conn, doc.doc_id, delta.run_id, aid,
                reason=f"source document revised to r{rev}", event=False,
            )
            delta.changed_docs.append(doc.doc_id)
            delta.touched_accounts.add(aid)
            emit(conn, delta.run_id, EventType.DOC_CHANGED, aid, "document", doc.doc_id,
                 f"Document edited upstream: {doc.title or doc.doc_id}",
                 {"revision": rev, "chars_before": len(row["body"] or ""),
                  "chars_after": len(doc.body), "claims_invalidated": stale})
        else:
            conn.execute("UPDATE documents SET last_seen_at=? WHERE doc_id=?", (now, doc.doc_id))

    return seen


def _reconcile_removals(conn: Connection, snap: AccountSnapshot,
                        seen: set[str], delta: Delta) -> None:
    """Tombstone documents we hold but upstream no longer serves.

    Gated on snap.complete -- see module docstring.
    """
    if not snap.complete:
        return

    aid = snap.account.account_id
    now = utcnow()
    held = conn.execute(
        "SELECT doc_id, doc_type, title FROM documents WHERE account_id=? AND status='active'",
        (aid,),
    ).fetchall()

    for row in held:
        if row["doc_id"] in seen:
            continue
        conn.execute(
            "UPDATE documents SET status='removed', removed_at=? WHERE doc_id=?",
            (now, row["doc_id"]),
        )
        retracted = _retract_claims(
            conn, row["doc_id"], delta.run_id, aid,
            reason="source document removed upstream", event=False,
        )
        delta.removed_docs.append(row["doc_id"])
        delta.touched_accounts.add(aid)
        emit(conn, delta.run_id, EventType.DOC_REMOVED, aid, "document", row["doc_id"],
             f"Document no longer available upstream: {row['title'] or row['doc_id']}",
             {"doc_type": row["doc_type"], "claims_retracted": retracted})
        if retracted:
            emit(conn, delta.run_id, EventType.CLAIMS_RETRACTED, aid, "claims", row["doc_id"],
                 f"{retracted} claim(s) retracted -- their only evidence was withdrawn",
                 {"source_doc_id": row["doc_id"]})


def _retract_claims(conn: Connection, doc_id: str, run_id: int,
                    account_id: str, reason: str, event: bool = True) -> int:
    cur = conn.execute(
        """UPDATE claims SET status='retracted', retracted_at=?, retraction_reason=?
           WHERE source_doc_id=? AND status='active'""",
        (utcnow(), reason, doc_id),
    )
    n = cur.rowcount or 0
    if n and event:
        emit(conn, run_id, EventType.CLAIMS_RETRACTED, account_id, "claims", doc_id,
             f"{n} claim(s) retracted: {reason}")
    return n


def _reinstate_claims(conn: Connection, doc_id: str, content_hash: str) -> int:
    """Reinstate only claims whose source text is byte-identical to what they
    were extracted from. If the document came back *edited*, the old claims stay
    retracted and re-extraction produces fresh ones."""
    cur = conn.execute(
        """UPDATE claims SET status='active', retracted_at=NULL, retraction_reason=NULL
           WHERE source_doc_id=? AND status='retracted' AND source_content_hash=?""",
        (doc_id, content_hash),
    )
    return cur.rowcount or 0


def _upsert_usage(conn: Connection, snap: AccountSnapshot, delta: Delta) -> None:
    now = utcnow()
    aid = snap.account.account_id
    for u in snap.usage:
        fp = u.fingerprint()
        row = conn.execute(
            "SELECT content_hash, flight_hours, missions FROM usage_periods WHERE account_id=? AND period=?",
            (aid, u.period),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO usage_periods (account_id, period, flight_hours, missions,
                   content_hash, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)""",
                (aid, u.period, u.flight_hours, u.missions, fp, now, now),
            )
            delta.usage_changed.append(f"{aid}:{u.period}")
            delta.touched_accounts.add(aid)
            emit(conn, delta.run_id, EventType.USAGE_NEW, aid, "usage", u.period,
                 f"Usage data for {u.period}: {u.flight_hours:g} flight hours, {u.missions} missions",
                 {"period": u.period, "flight_hours": u.flight_hours, "missions": u.missions})
        elif row["content_hash"] != fp:
            conn.execute(
                """UPDATE usage_periods SET flight_hours=?, missions=?, content_hash=?, last_seen_at=?
                   WHERE account_id=? AND period=?""",
                (u.flight_hours, u.missions, fp, now, aid, u.period),
            )
            delta.usage_changed.append(f"{aid}:{u.period}")
            delta.touched_accounts.add(aid)
            emit(conn, delta.run_id, EventType.USAGE_CHANGED, aid, "usage", u.period,
                 f"Usage restated for {u.period}: {row['flight_hours']:g} -> {u.flight_hours:g} hours",
                 {"period": u.period, "before": {"flight_hours": row["flight_hours"],
                  "missions": row["missions"]},
                  "after": {"flight_hours": u.flight_hours, "missions": u.missions}})
        else:
            conn.execute(
                "UPDATE usage_periods SET last_seen_at=? WHERE account_id=? AND period=?",
                (now, aid, u.period),
            )


# --------------------------------------------------------------------------


def ingest(conn: Connection, adapter: SourceAdapter, trigger: str = "manual") -> Delta:
    """Run one full reconciliation pass. Idempotent: a second run over an
    unchanged upstream produces an empty Delta and zero change events."""
    run_id = start_run(conn, trigger, adapter.name)
    delta = Delta(run_id=run_id)

    try:
        accounts = adapter.list_accounts()
    except Exception as exc:  # noqa: BLE001 - a failed listing must not tombstone
        finish_run(conn, run_id, delta, status="failed", error=f"list_accounts: {exc}")
        emit(conn, run_id, EventType.RUN_PARTIAL, None, "run", str(run_id),
             f"Poll failed before reading the account list: {exc}. Nothing was changed.")
        conn.commit()
        raise

    docs_seen = 0
    for acct in accounts:
        snap = adapter.fetch_account(acct)
        _upsert_account(conn, snap.account, delta)

        if not snap.complete:
            delta.incomplete_accounts.append(acct.account_id)
            emit(conn, run_id, EventType.RUN_PARTIAL, acct.account_id, "account", acct.account_id,
                 f"Partial fetch for {acct.name}; removal detection skipped to avoid false "
                 f"tombstones ({snap.error})")
            # Still record what we did get -- new docs are safe to add.
            _upsert_documents(conn, snap, delta)
            _upsert_usage(conn, snap, delta)
            continue

        seen = _upsert_documents(conn, snap, delta)
        docs_seen += len(seen)
        _reconcile_removals(conn, snap, seen, delta)
        _upsert_usage(conn, snap, delta)

    status = "partial" if delta.incomplete_accounts else "ok"
    conn.execute("UPDATE ingest_runs SET docs_seen=? WHERE run_id=?", (docs_seen, run_id))
    finish_run(conn, run_id, delta, status=status)
    conn.commit()
    return delta
