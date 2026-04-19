"""Loads Emotiv Cortex credentials from .env or the environment.

Put these in a .env at the repo root (gitignored) or export them in your shell:
  EMOTIV_CLIENT_ID=...
  EMOTIV_CLIENT_SECRET=...
"""
import os
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

CLIENT_ID = os.environ.get("EMOTIV_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("EMOTIV_CLIENT_SECRET", "")

if not CLIENT_ID or not CLIENT_SECRET:
    raise SystemExit(
        "Set EMOTIV_CLIENT_ID and EMOTIV_CLIENT_SECRET in .env at repo root, or export them in your shell"
    )
