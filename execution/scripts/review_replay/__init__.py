"""Replay harness for testing review models on a labelled set of PR heads.

The harness extends ``execution/scripts/harness_lens_runner.py`` (the real lens
path: throwaway checkout, probed sandbox profile, lens prompt, shared brief,
the swarm's own verdict reader) with extra provider kinds and a per-run metrics
record. It never posts anything. See ``review_replay.py`` for the CLI and
``score_replay.py`` for the scorer.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent.parent
DAEMON_DIR = REPO_ROOT / "execution" / "daemons" / "apis"

for _p in (str(REPO_ROOT), str(DAEMON_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
