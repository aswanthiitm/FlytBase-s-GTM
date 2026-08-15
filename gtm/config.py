from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]

def _in_railway() -> bool:
    return bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))


def _db_target() -> str:
    """Pick the store URL.

    Railway exposes Postgres twice: DATABASE_URL on a private `*.railway.internal`
    host (correct and faster inside the platform, unresolvable anywhere else) and
    DATABASE_PUBLIC_URL through a TCP proxy. Preferring the internal URL only
    when actually running on Railway means the same .env works on a laptop and
    in the deployed service, with no per-environment editing.
    """
    internal = os.getenv("DATABASE_URL")
    public = os.getenv("DATABASE_PUBLIC_URL")

    if internal and ".railway.internal" in internal and not _in_railway():
        if public:
            return public
        # No public URL available: fall through to SQLite rather than spending
        # the run on DNS failures against a host that cannot resolve here.
        internal = None

    return (internal or public
            or str(Path(os.getenv("GTM_DB_PATH", ROOT / "data" / "gtm.db"))))


DB_TARGET = _db_target()
DB_PATH = DB_TARGET  # backwards-compatible alias

# Source selection: "flytbase" (live API) or "fixture" (local JSON, for tests
# and for rehearsing the deletion path before it happens for real).
SOURCE = os.getenv("GTM_SOURCE", "fixture")

FLYTBASE_BASE_URL = os.getenv("FLYTBASE_BASE_URL", "").rstrip("/")
FLYTBASE_API_KEY = os.getenv("FLYTBASE_API_KEY", "")
FIXTURE_DIR = Path(os.getenv("GTM_FIXTURE_DIR", ROOT / "fixtures" / "snapshot_a"))

POLL_SECONDS = int(os.getenv("GTM_POLL_SECONDS", "300"))
HTTP_TIMEOUT = float(os.getenv("GTM_HTTP_TIMEOUT", "30"))
