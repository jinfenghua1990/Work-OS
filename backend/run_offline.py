"""Start Work OS locally without loading .env or contacting remote services."""
from __future__ import annotations

import os
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["WORKOS_LOAD_ENV_FILE"] = "0"
os.environ["WORKOS_DB_BACKEND"] = "sqlite"
os.environ["WORKOS_DB_PATH"] = str(ROOT / "backend" / "data" / "workos.sqlite3")
os.environ["WORKOS_HOST"] = "127.0.0.1"
os.environ["WORKOS_PORT"] = "8106"
runpy.run_path(str(Path(__file__).with_name("run_local.py")), run_name="__main__")
