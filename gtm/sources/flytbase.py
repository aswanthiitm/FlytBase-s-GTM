"""Live adapter for the FlytBase Book of Business.

The Book of Business is **not a REST API** — it is an MCP server speaking
JSON-RPC 2.0 over HTTP POST at a single endpoint. Everything is a `tools/call`.
Discovered tools:

    list_accounts()                    -> [account records]
    list_account_documents(id)         -> [{file, title, type, date?}]
    get_account_document(id, file)     -> markdown string
    get_account_usage(id)              -> [{month, flightHours, missions}]

Two notes that cost real time to establish, recorded so nobody re-derives them:

* **The API key already contains the `Bearer ` prefix.** Sending
  `Authorization: Bearer <key>` yields `Bearer Bearer …` and a 401. We pass it
  through as-is unless it looks bare.
* **The server is stateless** — `tools/call` works without an `initialize`
  handshake and returns no session id, so we do not maintain one.

Document bodies must be fetched individually to detect edits (the listing
carries no hash or version), so the per-account fetch fans out across a thread
pool. That is ~9 calls per account, ~126 per full poll.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from gtm.models import AccountSnapshot, DocType, RawAccount, RawDocument, RawUsagePeriod

_DOC_TYPE_MAP = {
    "profile": DocType.CRM,
    "transcript": DocType.TRANSCRIPT,
    "email": DocType.EMAIL,
    "tickets": DocType.TICKET,
    "ticket": DocType.TICKET,
    "notes": DocType.NOTE,
    "note": DocType.NOTE,
    "renewal": DocType.CONTRACT,
    "contract": DocType.CONTRACT,
}

# Lifecycle stage lives under `category` in this export, not `stage`.
_STAGE_KEYS = ["category", "lifecycle_stage", "stage", "categoryFolder"]


class MCPError(RuntimeError):
    pass


class FlytBaseAdapter:
    name = "flytbase"

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 60.0,
                 doc_workers: int = 8):
        if not base_url:
            raise ValueError("FLYTBASE_BASE_URL is not set")
        # This is the full MCP endpoint, path included — do not strip or append.
        self.url = base_url.rstrip("/")
        self.doc_workers = doc_workers

        key = (api_key or "").strip()
        if key and not key.lower().startswith("bearer "):
            key = f"Bearer {key}"
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if key:
            self.headers["Authorization"] = key

        self.client = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------
    def _call(self, tool: str, arguments: dict | None = None) -> Any:
        """One MCP tools/call. Raises MCPError on a JSON-RPC or transport error."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }
        response = self.client.post(self.url, headers=self.headers, json=payload)
        if response.status_code != 200:
            raise MCPError(f"{tool}: HTTP {response.status_code} {response.text[:200]}")

        try:
            body = response.json()
        except ValueError as exc:
            raise MCPError(f"{tool}: non-JSON response {response.text[:200]}") from exc

        if "error" in body:
            raise MCPError(f"{tool}: {body['error']}")

        blocks = body.get("result", {}).get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text  # document bodies come back as raw markdown

    # ------------------------------------------------------------------
    def list_accounts(self) -> list[RawAccount]:
        """Raises on failure, deliberately: a short account list must never be
        mistaken for accounts having been deleted."""
        rows = self._call("list_accounts")
        if not isinstance(rows, list):
            raise MCPError(f"list_accounts returned {type(rows).__name__}, expected list")

        out: list[RawAccount] = []
        for r in rows:
            aid = r.get("id") or r.get("accountId")
            if not aid:
                continue
            stage = next((r[k] for k in _STAGE_KEYS if r.get(k)), None)
            out.append(RawAccount(
                account_id=str(aid),
                name=str(r.get("name") or aid),
                stage=str(stage) if stage else None,
                raw=r,
            ))
        return out

    def fetch_account(self, account: RawAccount) -> AccountSnapshot:
        """Never raises. A failure returns complete=False so reconciliation
        skips removal detection for this account instead of tombstoning it."""
        aid = account.account_id
        try:
            listing = self._call("list_account_documents", {"id": aid})
        except Exception as exc:  # noqa: BLE001
            return AccountSnapshot(account=account, complete=False,
                                   error=f"list_account_documents: {exc}")
        if not isinstance(listing, list):
            return AccountSnapshot(account=account, complete=False,
                                   error=f"document listing was {type(listing).__name__}")

        try:
            documents = self._fetch_documents(aid, listing)
        except Exception as exc:  # noqa: BLE001
            return AccountSnapshot(account=account, complete=False,
                                   error=f"get_account_document: {exc}")

        # Usage is legitimately absent for pre-sale accounts; an empty list is a
        # complete answer, not a partial fetch.
        usage: list[RawUsagePeriod] = []
        try:
            usage = self._fetch_usage(aid)
        except Exception as exc:  # noqa: BLE001
            return AccountSnapshot(account=account, documents=documents, complete=False,
                                   error=f"get_account_usage: {exc}")

        return AccountSnapshot(account=account, documents=documents, usage=usage,
                               complete=True)

    # ------------------------------------------------------------------
    def _fetch_documents(self, aid: str, listing: list[dict]) -> list[RawDocument]:
        """Fan out over the account's documents.

        A single failed body aborts the whole account (the exception propagates
        to fetch_account, which marks it incomplete). That is deliberate: a
        partial document set would look like a deletion.
        """
        entries = [e for e in listing if isinstance(e, dict) and e.get("file")]

        def _one(entry: dict) -> RawDocument:
            file = entry["file"]
            body = self._call("get_account_document", {"id": aid, "file": file})
            if not isinstance(body, str):
                body = json.dumps(body)
            kind = str(entry.get("type") or "").lower()
            return RawDocument(
                # Composite id: file names are only unique within an account.
                doc_id=f"{aid}:{file}",
                account_id=aid,
                doc_type=_DOC_TYPE_MAP.get(kind, DocType.UNKNOWN),
                title=entry.get("title") or file,
                doc_date=entry.get("date"),
                body=body,
                raw=entry,
            )

        if not entries:
            return []
        with ThreadPoolExecutor(max_workers=self.doc_workers) as pool:
            return list(pool.map(_one, entries))

    def _fetch_usage(self, aid: str) -> list[RawUsagePeriod]:
        rows = self._call("get_account_usage", {"id": aid})
        if not isinstance(rows, list):
            return []
        out: list[RawUsagePeriod] = []
        for r in rows:
            period = str(r.get("month") or r.get("period") or "")[:7]
            if not period:
                continue
            out.append(RawUsagePeriod(
                account_id=aid,
                period=period,
                flight_hours=float(r.get("flightHours") or r.get("flight_hours") or 0),
                missions=int(r.get("missions") or 0),
                raw=r,
            ))
        return out
