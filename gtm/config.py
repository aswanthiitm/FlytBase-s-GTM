from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]

# DATABASE_URL wins when present — that is what Railway, Neon, and Supabase
# inject. Falling back to a local SQLite file keeps tests and local runs offline.
DB_TARGET = os.getenv("DATABASE_URL") or str(
    Path(os.getenv("GTM_DB_PATH", ROOT / "data" / "gtm.db"))
)
DB_PATH = DB_TARGET  # backwards-compatible alias

# Source selection: "flytbase" (live API) or "fixture" (local JSON, for tests
# and for rehearsing the deletion path before it happens for real).
SOURCE = os.getenv("GTM_SOURCE", "fixture")

FLYTBASE_BASE_URL = os.getenv("FLYTBASE_BASE_URL", "").rstrip("/")
FLYTBASE_API_KEY = os.getenv("FLYTBASE_API_KEY", "")
FIXTURE_DIR = Path(os.getenv("GTM_FIXTURE_DIR", ROOT / "fixtures" / "snapshot_a"))

POLL_SECONDS = int(os.getenv("GTM_POLL_SECONDS", "300"))
HTTP_TIMEOUT = float(os.getenv("GTM_HTTP_TIMEOUT", "30"))
