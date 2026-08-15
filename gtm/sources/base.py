from __future__ import annotations

from typing import Protocol, runtime_checkable

from gtm.models import AccountSnapshot, RawAccount


@runtime_checkable
class SourceAdapter(Protocol):
    """The one seam between us and the Book of Business.

    Contract, and the reason each clause exists:

    * ``list_accounts`` raises on failure. It never returns a short list on a
      partial error -- a truncated account list would look like accounts were
      deleted.

    * ``fetch_account`` never raises. It returns an AccountSnapshot with
      ``complete=False`` and an error string. Reconciliation reads that flag and
      declines to tombstone anything for that account, so one flaky endpoint
      degrades a single account instead of corrupting the portfolio.
    """

    name: str

    def list_accounts(self) -> list[RawAccount]: ...

    def fetch_account(self, account: RawAccount) -> AccountSnapshot: ...
