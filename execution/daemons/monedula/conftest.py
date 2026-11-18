"""Keep pytest runs that import monedula out of the production daemon log.

``monedula.py`` calls ``configure_daemon_logging("monedula", ...)`` at import,
which opens ``~/Library/Logs/ateles/monedula.log`` and so wrote test output
into the live daemon's log. ``logging_setup`` exposes no environment variable
for the directory (only ``LOG_DIR``, a module global), so the redirect happens
here, before any test module imports monedula.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime import logging_setup  # noqa: E402

REAL_LOG_DIR = logging_setup.LOG_DIR
TEST_LOG_DIR = Path(tempfile.mkdtemp(prefix="monedula-test-logs-"))
logging_setup.LOG_DIR = TEST_LOG_DIR


# ── Keep every test off the daemon directory's real state files ──────────────
#
# The payment journal, its lock, the consent mark and the Telegram offset all
# default to files beside this module. In the daemon checkout those are the
# operator's real payment state. Every test gets them under its own tmp_path,
# the BTC explorer has no network, and the session fails if any state file in
# this directory was created or modified during the run.

import pytest  # noqa: E402

_DAEMON_DIR = Path(__file__).resolve().parent
_STATE_GLOB = ".monedula_*"


def _state_files() -> dict[str, float]:
    return {p.name: p.stat().st_mtime for p in _DAEMON_DIR.glob(_STATE_GLOB)}


_STATE_BEFORE = _state_files()


@pytest.fixture(autouse=True)
def _isolate_daemon_state(monkeypatch, tmp_path):
    import importlib

    import chain_reconcile

    for module_name, attr in (
        ("consent_email", "STATE_FILE"),
        ("consent_email", "CORRECTION_DEDUPE_FILE"),
        ("monedula", "STATE_FILE"),
        ("monedula", "TG_OFFSET_FILE"),
        ("monedula", "GATE_HEALTH_FILE"),
        ("strandings", "STATE_FILE"),
    ):
        # Import eagerly: a module first imported after this fixture ran
        # would otherwise keep its real path.
        module = importlib.import_module(module_name)
        if hasattr(module, attr):
            name = Path(getattr(module, attr)).name
            monkeypatch.setattr(module, attr, tmp_path / f"{module_name}-{name}")

    def _no_network(url):
        raise AssertionError(f"test tried to reach the network: {url}")

    monkeypatch.setattr(chain_reconcile, "_default_fetch", _no_network)
    yield


def pytest_sessionfinish(session, exitstatus):
    after = _state_files()
    touched = sorted(
        name
        for name, mtime in after.items()
        if name not in _STATE_BEFORE or _STATE_BEFORE[name] != mtime
    )
    if touched:
        print(
            f"\nFAIL: tests created or modified daemon state files in "
            f"{_DAEMON_DIR}: {touched}"
        )
        session.exitstatus = 1
