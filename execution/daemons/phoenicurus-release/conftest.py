"""Pytest path bootstrap: daemons import repo-root packages and sibling
modules as top-level (same as the standalone-script runtime path setup)."""

import sys
from pathlib import Path

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent

for p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)
