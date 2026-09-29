"""Pytest path bootstrap: daemons import repo-root packages and sibling
modules as top-level (same as the standalone-script runtime path setup)."""

import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent

for p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _isolate_dispatch_failure_logs(monkeypatch, tmp_path):
    """Never let a test write diagnostics into the operator's real log directory.

    `write_dispatch_failure_log` resolves `DISPATCH_FAILURE_LOG_DIR` at call time
    and defaults to ~/Library/Logs/ateles/dispatch-failures/ — the same directory
    a live daemon writes to. Any test that reaches a dispatch-failure path (now
    including the exit-0-but-no-PR post-condition path) would deposit fixture
    output like `owner/repo#100` there, polluting real diagnostic evidence.
    Autouse so a future test cannot reintroduce the leak by forgetting to patch.
    """
    import skill_runner

    monkeypatch.setattr(
        skill_runner, "DISPATCH_FAILURE_LOG_DIR", tmp_path / "dispatch-failures"
    )


@pytest.fixture(autouse=True)
def _isolate_harness_usage_snapshot(monkeypatch, tmp_path):
    """Never let a test read or write the operator's live plan-usage snapshot.

    The router folds ``~/.config/ateles/harness-usage.json`` into every
    selection, so an unisolated test would pass or fail on the host's real
    quota state. Tests that exercise the snapshot set their own path.
    """
    monkeypatch.setenv(
        "APIS_HARNESS_USAGE_FILE", str(tmp_path / "isolated-harness-usage.json")
    )


@pytest.fixture(autouse=True)
def _isolate_tier_ledger(monkeypatch, tmp_path):
    """Never let a test append to the operator's real tier-dispatch ledger.

    Every dispatch appends a row (model_tiering.record_dispatch); an unisolated
    test run would pollute the per-tier counts the operator paces the weekly
    budget against. Tests that read the ledger set their own path.
    """
    monkeypatch.setenv("APIS_TIER_LEDGER_FILE", str(tmp_path / "isolated-tier-dispatch.jsonl"))
