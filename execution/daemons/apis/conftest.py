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

# Redact host environment values from every failure report (see the module
# docstring: the 2026-09-29 incident printed a real host secret into an agent
# transcript via an env-capturing test's assertion output). Importing a
# `pytest_*` name into a conftest registers it as a hook.
from lib.pytest_env_guard import (  # noqa: E402,F401
    clear_host_env,
    pytest_collection_finish,
    pytest_configure,
    pytest_make_collect_report,
    pytest_runtest_makereport,
    pytest_warning_recorded,
)


@pytest.fixture(autouse=True)
def _hermetic_host_env(monkeypatch):
    """Start every test in this suite from a synthetic environment.

    Daemon code builds child-process environments from ``os.environ``. If the
    host shell already exports a variable the code under test is supposed to
    inject itself (e.g. ``NEOTOMA_AAUTH_PRIVATE_JWK_PATH``), a "must not be
    injected" test fails on the host and passes in CI, and a failure message
    can print the host's whole environment. Dropping every non-allowlisted
    host variable makes the outcome independent of the machine. Tests that need
    a variable set it themselves with ``monkeypatch.setenv``.
    """
    clear_host_env(monkeypatch)


@pytest.fixture(autouse=True)
def _isolate_dispatch_failure_logs(_hermetic_host_env, monkeypatch, tmp_path):
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
def _isolate_harness_usage_snapshot(_hermetic_host_env, monkeypatch, tmp_path):
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


@pytest.fixture(autouse=True)
def _isolate_tiering_config(monkeypatch, tmp_path):
    """Never let a test read the operator's live tiering config.

    `~/.config/ateles/action-policy.json` and `vendor-binding.json` are read
    fresh on every dispatch, and a FILE beats the env var. Once the operator
    installs them, a test that sets `APIS_VENDOR_BINDING` (or expects no
    binding at all) silently reads the host's instead: two #1348 tests failed
    exactly this way on the host after the config was installed. Point both at
    absent files; tests that exercise config set their own.
    """
    monkeypatch.setenv("APIS_ACTION_POLICY_FILE", str(tmp_path / "isolated-action-policy.json"))
    monkeypatch.setenv("APIS_VENDOR_BINDING_FILE", str(tmp_path / "isolated-vendor-binding.json"))
