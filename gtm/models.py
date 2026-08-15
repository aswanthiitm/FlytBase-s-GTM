"""Canonical internal data model.

Everything downstream of ingest speaks these types, never the source API's
wire format. Source-specific quirks are confined to gtm.sources.*, so a change
in the upstream API touches one adapter and nothing else.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(*parts: Any) -> str:
    """Stable content fingerprint.

    Dicts are dumped with sorted keys so that upstream key reordering does not
    masquerade as a content change and trigger a pointless re-extraction.
    """
    h = hashlib.sha256()
    for p in parts:
        if p is None:
            h.update(b"\x00")
        elif isinstance(p, (dict, list)):
            h.update(json.dumps(p, sort_keys=True, default=str).encode())
        else:
            h.update(str(p).strip().encode())
        h.update(b"\x1f")
    return h.hexdigest()


class DocStatus(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"


class ClaimStatus(StrEnum):
    ACTIVE = "active"
    RETRACTED = "retracted"


class DocType(StrEnum):
    """Normalized document taxonomy.

    The adapter maps whatever the source calls things into these buckets;
    UNKNOWN is deliberately allowed so an unrecognized new type at 4:30 PM is
    ingested and flagged rather than silently dropped.
    """

    TRANSCRIPT = "transcript"
    EMAIL = "email"
    TICKET = "ticket"
    NOTE = "note"
    CRM = "crm"
    CONTRACT = "contract"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------
# Raw layer -- what an adapter returns
# --------------------------------------------------------------------------


class RawAccount(BaseModel):
    account_id: str
    name: str
    stage: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    def fingerprint(self) -> str:
        return content_hash(self.account_id, self.name, self.stage, self.raw)


class RawDocument(BaseModel):
    doc_id: str
    account_id: str
    doc_type: DocType = DocType.UNKNOWN
    title: str | None = None
    doc_date: str | None = None
    body: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

    def fingerprint(self) -> str:
        # Body plus the metadata that changes meaning. Deliberately excludes
        # the full raw payload: a server-side `fetched_at` stamp must not read
        # as a content change.
        return content_hash(self.doc_id, self.doc_type, self.title, self.doc_date, self.body)


class RawUsagePeriod(BaseModel):
    account_id: str
    period: str  # YYYY-MM
    flight_hours: float = 0.0
    missions: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)

    def fingerprint(self) -> str:
        return content_hash(self.account_id, self.period, self.flight_hours, self.missions)


class AccountSnapshot(BaseModel):
    """One adapter fetch for one account.

    `complete` is the single most important field in this file. A partial or
    failed upstream fetch must never be mistaken for "the documents were
    deleted" -- that would tombstone the entire portfolio on a transient 500.
    Reconciliation is skipped unless complete is True.
    """

    account: RawAccount
    documents: list[RawDocument] = Field(default_factory=list)
    usage: list[RawUsagePeriod] = Field(default_factory=list)
    complete: bool = True
    error: str | None = None


# --------------------------------------------------------------------------
# Claim layer -- the evidence unit (L2)
# --------------------------------------------------------------------------


class Claim(BaseModel):
    """One atomic, evidence-backed assertion extracted from exactly one document.

    A claim with no source document and no verbatim quote is not a claim, it is
    an opinion; the persistence layer rejects it.
    """

    account_id: str
    claim_type: str
    subject: str | None = None
    value: str
    confidence: float = 0.5
    source_doc_id: str
    verbatim_quote: str
    doc_date: str | None = None

    def claim_id(self) -> str:
        return content_hash(self.source_doc_id, self.claim_type, self.subject, self.value)


# --------------------------------------------------------------------------
# Change feed (L6)
# --------------------------------------------------------------------------


class EventType(StrEnum):
    DOC_NEW = "doc_new"
    DOC_CHANGED = "doc_changed"
    DOC_REMOVED = "doc_removed"
    DOC_RESTORED = "doc_restored"
    ACCOUNT_NEW = "account_new"
    ACCOUNT_CHANGED = "account_changed"
    USAGE_NEW = "usage_new"
    USAGE_CHANGED = "usage_changed"
    CLAIMS_RETRACTED = "claims_retracted"
    CLAIMS_REINSTATED = "claims_reinstated"
    RUN_PARTIAL = "run_partial"


class Delta(BaseModel):
    """Result of one reconciliation pass. Drives every downstream engine:
    only accounts appearing in `touched_accounts` get re-reasoned."""

    run_id: int
    new_docs: list[str] = Field(default_factory=list)
    changed_docs: list[str] = Field(default_factory=list)
    removed_docs: list[str] = Field(default_factory=list)
    restored_docs: list[str] = Field(default_factory=list)
    new_accounts: list[str] = Field(default_factory=list)
    changed_accounts: list[str] = Field(default_factory=list)
    usage_changed: list[str] = Field(default_factory=list)
    touched_accounts: set[str] = Field(default_factory=set)
    incomplete_accounts: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.touched_accounts

    def summary(self) -> str:
        return (
            f"{len(self.new_docs)} new, {len(self.changed_docs)} changed, "
            f"{len(self.removed_docs)} removed, {len(self.restored_docs)} restored, "
            f"{len(self.usage_changed)} usage updates across "
            f"{len(self.touched_accounts)} account(s)"
        )
