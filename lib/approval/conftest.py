"""Pytest path bootstrap so `from lib.approval import ...` resolves when the
tests run from anywhere (mirrors the daemon runtime path setup)."""

import sys
from pathlib import Path

_LIB_APPROVAL = Path(__file__).resolve().parent
_REPO_ROOT = _LIB_APPROVAL.parent.parent  # .../ateles (lib/approval → lib → repo root)

for _p in (str(_REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)
