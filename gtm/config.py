from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]

DB_PATH = Path(os.getenv("GTM_DB_PATH", ROOT / "data" / "gtm.db"))

# Source selection: "flytbase" (live API) or "fixture" (local JSON, for tests
# and for rehearsing the deletion path before it happens for real).
SOURCE = os.getenv("GTM_SOURCE", "fixture")

FLYTBASE_BASE_URL = os.getenv("FLYTBASE_BASE_URL", "").rstrip("/")
FLYTBASE_API_KEY = os.getenv("FLYTBASE_API_KEY", "")
FIXTURE_DIR = Path(os.getenv("GTM_FIXTURE_DIR", ROOT / "fixtures" / "snapshot_a"))

POLL_SECONDS = int(os.getenv("GTM_POLL_SECONDS", "300"))
HTTP_TIMEOUT = float(os.getenv("GTM_HTTP_TIMEOUT", "30"))
