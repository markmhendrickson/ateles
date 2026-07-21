"""Pytest path bootstrap: daemons import repo-root packages and sibling
modules as top-level (same as the standalone-script runtime path setup)."""

import os
import sys
from pathlib import Path

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent

for p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# skill_runner.py (imported by several test modules in this directory, some
# transitively via swarm_dispatch.py) resolves NEOTOMA_BASE_URL at import
# time and fails loud if unset (ateles#243) — default it here so collection
# succeeds in a clean CI environment that doesn't already export it.
os.environ.setdefault("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")
