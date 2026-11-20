"""Start the local API, using the project's optional virtualenv dependencies."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if sys.platform == "win32":
    candidate = ROOT / ".venv" / "Lib" / "site-packages"
    if candidate.is_dir():
        sys.path.insert(0, str(candidate))
else:
    for candidate in (ROOT / ".venv" / "lib").glob("python*/site-packages"):
        sys.path.insert(0, str(candidate))
        break

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
import server  # noqa: E402

if __name__ == "__main__":
    server.main()
