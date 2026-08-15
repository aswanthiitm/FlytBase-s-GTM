"""Live adapter for the FlytBase Book of Business read-only API.

Endpoint paths and auth style are configurable because they are confirmed by
`gtm probe` at runtime, not guessed at design time. Field extraction goes
through `_first`, which accepts a list of candidate key names -- the upstream
export mixes naming conventions and this keeps that mess out of the rest of the
codebase.

The important behaviour is in fetch_account: any failure becomes
complete=False rather than an exception, so a flaky endpoint degrades one
account instead of tombstoning the portfolio.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from gtm.models import AccountSnapshot, DocType, RawAccount, RawDocument, RawUsagePeriod

# Overridable without a code change, so a probe surprise costs an env var.
ACCOUNTS_PATH = os.getenv("FLYTBASE_ACCOUNTS_PATH", "/api/accounts")
DOCS_PATH = os.getenv("FLYTBASE_DOCS_PATH", "/api/accounts/{account_id}/documents")
USAGE_PATH = os.getenv("FLYTBASE_USAGE_PATH", "/api/accounts/{account_id}/usage")
DOC_DETAIL_PATH = os.getenv("FLYTBASE_DOC_DETAIL_PATH", "")  # set if list omits bodies

_DOC_TYPE_MAP = {
    "call": DocType.TRANSCRIPT, "call_transcript": DocType.TRANSCRIPT,
    "transcript": DocType.TRANSCRIPT, "meeting": DocType.TRANSCRIPT,
    "email": DocType.EMAIL, "email_thread": DocType.EMAIL, "thread": DocType.EMAIL,
    "ticket": DocType.TICKET, "support_ticket": DocType.TICKET, "support": DocType.TICKET,
    "note": DocType.NOTE, "internal_note": DocType.NOTE, "internal": DocType.NOTE,
    "crm": DocType.CRM, "crm_record": DocType.CRM,
    "contract": DocType.CONTRACT, "renewal": DocType.CONTRACT,
}


def _first(d: dict, keys: list[str], default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _as_list(payload: Any, *container_keys: str) -> list[dict]:
    """Upstream may return a bare list or wrap it in {data: [...]}/{results:[...]}."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in (*container_keys, "data", "results", "items", "records"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []


def _body_of(d: dict) -> str:
    """Documents arrive with the substance under different keys, and sometimes
    as a list of transcript turns. Flatten to text without losing speakers."""
    raw = _first(d, ["body", "content", "text", "transcript", "notes", "description",
                     "message", "summary"], "")
    if isinstance(raw, list):
        parts = []
        for turn in raw:
            if isinstance(turn, dict):
                speaker = _first(turn, ["speaker", "from", "author", "name"], "")
                text = _first(turn, ["text", "content", "message", "body"], "")
                parts.append(f"{speaker}: {text}" if speaker else str(text))
            else:
                parts.append(str(turn))
        return "\n".join(parts)
    if isinstance(raw, dict):
        return "\n".join(f"{k}: {v}" for k, v in raw.items())
    return str(raw)


class FlytBaseAdapter:
    name = "flytbase"

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 30.0):
        if not base_url:
            raise ValueError("FLYTBASE_BASE_URL is not set")
        self.base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if api_key:
            # Probe confirms which the server wants; sending both is harmless.
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        self.client = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout,
                                   follow_redirects=True)

    def close(self) -> None:
        self.client.close()

    def _get(self, path: str) -> Any:
        r = self.client.get(path)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    def list_accounts(self) -> list[RawAccount]:
        """Raises on failure -- deliberately. A short account list must never be
        mistaken for accounts having been deleted."""
        payload = self._get(ACCOUNTS_PATH)
        out: list[RawAccount] = []
        for r in _as_list(payload, "accounts"):
            aid = _first(r, ["account_id", "id", "accountId", "slug"])
            if aid is None:
                continue
            out.append(RawAccount(
                account_id=str(aid),
                name=str(_first(r, ["name", "account_name", "company", "customer"], str(aid))),
                stage=_first(r, ["stage", "lifecycle_stage", "lifecycleStage", "status"]),
                raw=r,
            ))
        return out

    def fetch_account(self, account: RawAccount) -> AccountSnapshot:
        aid = account.account_id
        try:
            docs = self._fetch_documents(aid)
        except Exception as exc:  # noqa: BLE001
            return AccountSnapshot(account=account, complete=False,
                                   error=f"documents: {type(exc).__name__}: {exc}")

        # Usage is optional (only accounts that fly have it). A 404 here is a
        # legitimate "no usage", not a partial fetch.
        usage: list[RawUsagePeriod] = []
        try:
            usage = self._fetch_usage(aid)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                return AccountSnapshot(account=account, documents=docs, complete=False,
                                       error=f"usage: {exc}")
        except Exception as exc:  # noqa: BLE001
            return AccountSnapshot(account=account, documents=docs, complete=False,
                                   error=f"usage: {type(exc).__name__}: {exc}")

        return AccountSnapshot(account=account, documents=docs, usage=usage, complete=True)

    # ------------------------------------------------------------------
    def _fetch_documents(self, aid: str) -> list[RawDocument]:
        payload = self._get(DOCS_PATH.format(account_id=aid))
        out: list[RawDocument] = []
        for r in _as_list(payload, "documents", "docs"):
            did = _first(r, ["doc_id", "id", "document_id", "documentId"])
            if did is None:
                continue
            body = _body_of(r)
            if not body and DOC_DETAIL_PATH:
                try:
                    detail = self._get(DOC_DETAIL_PATH.format(account_id=aid, doc_id=did))
                    body = _body_of(detail if isinstance(detail, dict) else {})
                    r = {**r, "_detail": detail}
                except Exception:  # noqa: BLE001 - detail miss is not fatal
                    pass
            kind = str(_first(r, ["doc_type", "type", "kind", "category"], "")).lower()
            out.append(RawDocument(
                doc_id=str(did),
                account_id=aid,
                doc_type=_DOC_TYPE_MAP.get(kind, DocType.UNKNOWN),
                title=_first(r, ["title", "subject", "name", "headline"]),
                doc_date=str(_first(r, ["date", "doc_date", "created_at", "createdAt",
                                        "timestamp", "sent_at"], "") or "") or None,
                body=body,
                raw=r,
            ))
        return out

    def _fetch_usage(self, aid: str) -> list[RawUsagePeriod]:
        payload = self._get(USAGE_PATH.format(account_id=aid))
        out: list[RawUsagePeriod] = []
        for r in _as_list(payload, "usage", "history", "months"):
            period = str(_first(r, ["period", "month", "date", "yyyymm"], "") or "")[:7]
            if not period:
                continue
            out.append(RawUsagePeriod(
                account_id=aid,
                period=period,
                flight_hours=float(_first(r, ["flight_hours", "hours", "flightHours",
                                              "flight_time_hours"], 0) or 0),
                missions=int(_first(r, ["missions", "mission_count", "missionCount",
                                        "flights"], 0) or 0),
                raw=r,
            ))
        return out
