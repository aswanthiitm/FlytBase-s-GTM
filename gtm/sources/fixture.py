"""Local-JSON adapter.

Exists so the deletion / restore path can be exercised on demand instead of
being discovered live. Point GTM_FIXTURE_DIR at snapshot_a, ingest, point it at
snapshot_b (which drops a document), ingest again -- the tombstone path runs
end to end with no network.

Layout:
    <dir>/accounts.json          -> [ {account_id, name, stage, ...}, ... ]
    <dir>/<account_id>.json      -> {documents: [...], usage: [...]}
"""

from __future__ import annotations

import json
from pathlib import Path

from gtm.models import AccountSnapshot, DocType, RawAccount, RawDocument, RawUsagePeriod


class FixtureAdapter:
    name = "fixture"

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def list_accounts(self) -> list[RawAccount]:
        path = self.dir / "accounts.json"
        if not path.exists():
            raise FileNotFoundError(f"fixture account list missing: {path}")
        rows = json.loads(path.read_text())
        return [
            RawAccount(
                account_id=str(r["account_id"]),
                name=r.get("name", r["account_id"]),
                stage=r.get("stage"),
                raw=r,
            )
            for r in rows
        ]

    def fetch_account(self, account: RawAccount) -> AccountSnapshot:
        path = self.dir / f"{account.account_id}.json"
        if not path.exists():
            # An account with no detail file is a real, complete "no documents"
            # state in fixture-land -- but we mark it incomplete anyway, because
            # guessing wrong here is exactly the failure we are guarding against.
            return AccountSnapshot(
                account=account, complete=False, error=f"no fixture file: {path.name}"
            )
        try:
            blob = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            return AccountSnapshot(account=account, complete=False, error=f"bad json: {exc}")

        docs = [
            RawDocument(
                doc_id=str(d["doc_id"]),
                account_id=account.account_id,
                doc_type=DocType(d["doc_type"]) if d.get("doc_type") in set(DocType) else DocType.UNKNOWN,
                title=d.get("title"),
                doc_date=d.get("doc_date"),
                body=d.get("body", ""),
                raw=d,
            )
            for d in blob.get("documents", [])
        ]
        usage = [
            RawUsagePeriod(
                account_id=account.account_id,
                period=u["period"],
                flight_hours=float(u.get("flight_hours", 0)),
                missions=int(u.get("missions", 0)),
                raw=u,
            )
            for u in blob.get("usage", [])
        ]
        return AccountSnapshot(account=account, documents=docs, usage=usage, complete=True)
