"""Environment, constants, thresholds."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

BASE_URL = "https://public-api.system.com"
API_PREFIX = "/v1"
API_KEY_ENV = "SYSTEM_API_KEY"
API_KEY_HEADER = "x-api-key"

ENV_FILE = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
PROBE_RESULTS = DATA_DIR / "probe" / "results.json"


class MissingApiKey(RuntimeError):
    """SYSTEM_API_KEY is not available from the environment or .env."""


def load_env(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE lines from .env into os.environ. The real environment wins."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        os.environ.setdefault(name, value.strip().strip("'\""))


def api_key(required: bool = True) -> str | None:
    """Return the API key. Callers must never log, print, cache, or persist it."""
    load_env()
    key = os.environ.get(API_KEY_ENV, "").strip()
    if key:
        return key
    if required:
        raise MissingApiKey(f"{API_KEY_ENV} is not set. Add a line SYSTEM_API_KEY=your-key to {ENV_FILE}.")
    return None
