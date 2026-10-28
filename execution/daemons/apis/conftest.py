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
def _default_usage_gate_and_probe_off(monkeypatch):
    """Keep the usage gate and its live probe out of tests that do not target them.

    The gate fails closed on a missing snapshot and the probe would launch the
    real ``claude`` CLI; neither belongs in an unrelated selection test.  Tests
    of the gate set ``APIS_USAGE_GATE=on`` (and inject the probe's runner).
    """
    monkeypatch.setenv("APIS_USAGE_GATE", "off")
    monkeypatch.setenv("APIS_USAGE_PROBE", "off")


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


@pytest.fixture(autouse=True)
def _isolate_lens_comment_identities(monkeypatch):
    """Keep the swarm-identity lookups for lens comments off the network and off
    the host's real tokens and App.

    `lens_authors.resolve_authors` reads the App's bot login and, live, the
    account each shared agent token belongs to. A unit test must never do either:
    the default identity is "swarm-lens-account", which a test's lens comments
    carry as their author; a test of another identity sets
    `ATELES_LENS_COMMENT_AUTHORS` itself.
    """
    import lens_authors

    monkeypatch.setenv(lens_authors.ENV_AUTHORS, "swarm-lens-account")
    for name in lens_authors.PAT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    # Keep the real function reachable: a test of the App-login path restores it
    # and stubs only the network boundary (`_github_app_token.bot_login`).
    monkeypatch.setattr(
        lens_authors, "_REAL_APP_BOT_LOGINS", lens_authors._app_bot_logins, raising=False
    )
    monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: (set(), False))
    monkeypatch.setattr(lens_authors, "_login_for_token", lambda token: "")
    lens_authors.clear_cache()


@pytest.fixture(autouse=True)
def _default_github_write_credential(monkeypatch):
    """Give every test the write credential a deployed dispatcher has, with no App.

    The dispatcher's GitHub writes are skipped, with a WARNING, when no write
    credential is usable (ateles#1381), so a test of what a write DOES needs
    one. The fallback is applied inside `_resolve_github_write_token` rather than
    through the environment, so it never changes which agent-token variables the
    lens-identity lookups see. A fixture value, never a real token; the host's
    real App is switched off so a test can never mint one. A test of the App
    path or of the no-credential path restores the real resolver itself
    (`REAL_RESOLVE_GITHUB_WRITE_TOKEN`).
    """
    import swarm_dispatch

    real = swarm_dispatch._resolve_github_write_token
    monkeypatch.setattr(
        swarm_dispatch,
        "_resolve_github_write_token",
        lambda repo, app_token: real(repo, app_token) or "pat-conftest-fixture",
    )
    monkeypatch.setattr(swarm_dispatch, "_REAL_RESOLVE_GITHUB_WRITE_TOKEN", real, raising=False)
    # Disable only dispatcher token minting. The shared helper also resolves
    # per-lens Apps; mocking it globally erases those identities in their tests.
    monkeypatch.setattr(swarm_dispatch, "_app_write_configured", lambda: False)
    swarm_dispatch._clear_app_write_token_cache()


@pytest.fixture(autouse=True)
def _isolate_build_worktrees(monkeypatch, tmp_path):
    """Never let a test add a real worktree to the operator's real clones.

    A delivery-bearing Cicada dispatch provisions a linked worktree off the
    repository's local clone (`build_delivery.prepare_build_worktree`). The
    clone for a fixture slug like `owner/repo` does not exist, and for a real
    slug like `markmhendrickson/neotoma` it does — so an unisolated test would
    either fail on the host's layout or write `git worktree` metadata into the
    operator's checkout. Every test gets a fake; `test_build_delivery.py`
    exercises the real function, kept as `prepare_build_worktree.real`,
    against a throwaway repository.
    """
    import build_delivery

    async def _fake_prepare(repository, label):
        return build_delivery.BuildWorktree(
            path=str(tmp_path / "build-worktree"),
            branch=f"cicada/{label}-test",
            base_sha="0" * 40,
            clone=str(tmp_path / "clone"),
            base_fresh=True,
        )

    _fake_prepare.real = build_delivery.prepare_build_worktree
    monkeypatch.setattr(build_delivery, "prepare_build_worktree", _fake_prepare)
    monkeypatch.setattr(build_delivery, "_inflight", {})
    monkeypatch.setattr(build_delivery, "_release_hook", None)


@pytest.fixture(autouse=True)
def _fly_runner_off_and_isolated(monkeypatch, tmp_path):
    """The Fly executor is off by default (ateles#1456): no test inherits an
    operator's ``APIS_FLY_RUNNER_CLASSES``, and none touches the real ledger."""
    import fly_runner

    monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES", raising=False)
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(tmp_path / "isolated-fly-ledger.json"))
    monkeypatch.setattr(fly_runner, "_EXECUTOR", None)
    fly_runner._LOGIN_LOCKS.clear()


@pytest.fixture(autouse=True)
def _no_real_retry_backoff(monkeypatch):
    """A retried signed write waits 1 s and 3 s in production; never in a test.

    The sleep is replaced by one that returns at once, so a test of a persistent
    failure takes no wall-clock time. Tests of the backoff itself replace it again
    with one that records the delays.
    """
    import gate_waive

    async def _instant(delay):
        return None

    monkeypatch.setattr(gate_waive, "_retry_sleep", _instant)


@pytest.fixture(autouse=True)
def _isolate_codex_login(monkeypatch, tmp_path):
    """A stand-in Codex login, never the host's.

    Local Codex children run in an isolated harness home that links the Codex
    login file (``auth.json``) and refuses to launch without one. A test must
    neither depend on the host having a Codex login nor link the host's real
    one: point ``CODEX_HOME`` at a placeholder login. Tests of other homes
    set their own.
    """
    home = tmp_path / "isolated-codex-login"
    home.mkdir()
    (home / "auth.json").write_text("{}", encoding="utf-8")
    (home / "auth.json").chmod(0o600)
    monkeypatch.setenv("CODEX_HOME", str(home))


@pytest.fixture(autouse=True)
def _panel_check_hold_off_by_default(monkeypatch):
    """The review panel's check-state hold reads GitHub; no unrelated test may.

    `SwarmDispatcher._hold_panel_until_checks_green` fetches the PR and reads
    its check runs before any lens is seated, so every test that drives
    `_handle_pr` or a review sweep without stubbing GitHub would otherwise hit
    the network (and, failing closed, hold). Default it to "not held". The tests
    of the hold itself (`test_panel_check_hold.py`) restore the real method from
    `swarm_dispatch._REAL_HOLD_PANEL_UNTIL_CHECKS_GREEN` and stub only the
    check-state reader.
    """
    import swarm_dispatch

    real = swarm_dispatch.SwarmDispatcher._hold_panel_until_checks_green

    async def _not_held(self, trigger, *, source):
        return False

    monkeypatch.setattr(
        swarm_dispatch, "_REAL_HOLD_PANEL_UNTIL_CHECKS_GREEN", real, raising=False
    )
    monkeypatch.setattr(
        swarm_dispatch.SwarmDispatcher, "_hold_panel_until_checks_green", _not_held
    )
