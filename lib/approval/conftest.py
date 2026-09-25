"""Pytest path bootstrap so `from lib.approval import ...` resolves when the
tests run from anywhere (mirrors the daemon runtime path setup)."""

import sys
from pathlib import Path

_LIB_APPROVAL = Path(__file__).resolve().parent
_REPO_ROOT = _LIB_APPROVAL.parent.parent  # .../ateles (lib/approval → lib → repo root)

for _p in (str(_REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _swarm_mailbox(monkeypatch, tmp_path):
    """Every gws call in lib.approval runs as the swarm's own mailbox and makes
    no call at all without one (ateles#1221). Configure one by default so the
    tests exercise the working path; tests of the unconfigured case remove it.
    Hermetic: never depends on the host's env or gws config.
    """
    cfg = tmp_path / "swarm-gws"
    cfg.mkdir(exist_ok=True)
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.net")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("ATELES_MAIL_AUTHSERV_ID", raising=False)
    monkeypatch.delenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", raising=False)
