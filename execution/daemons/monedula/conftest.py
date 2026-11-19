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

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime import logging_setup  # noqa: E402

REAL_LOG_DIR = logging_setup.LOG_DIR
TEST_LOG_DIR = Path(tempfile.mkdtemp(prefix="monedula-test-logs-"))
logging_setup.LOG_DIR = TEST_LOG_DIR


@pytest.fixture(autouse=True)
def isolate_default_payment_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may resolve Monedula's default journal to the live file."""
    import consent_email

    monkeypatch.setattr(consent_email, "STATE_FILE", tmp_path / "consent.json")


def initialize_test_journal(directory: Path) -> Path:
    """Explicitly bootstrap an isolated journal for payment-route tests."""
    from payment_journal import JOURNAL_NAME, init_journal

    path = directory / JOURNAL_NAME
    init_journal(path)
    return path
