"""Test-hermeticity regression for the dotenv bootstrap (ateles#1285).

`lib/daemon_runtime/__init__.py` loads the operator's materialized dotenv
(~/.config/neotoma/.env) into os.environ at import time, so launchd-run
daemons inherit real credentials. On any machine where that file also sets
an operator-behaviour switch — e.g. `ATELES_SWARM_REQUIRE_LABEL=swarm-canary`
for the bootstrap canary gate (ateles#1269) — importing lib.daemon_runtime
from a *test* silently turned that switch on too, so a test asserting
label-gate-off behaviour (e.g.
execution/daemons/anthus/test_participation_state.py::
test_known_satisfied_gate_is_not_redispatched) failed locally while passing
in CI, which has no such file. The fix is `_dotenv_should_load()`: it
refuses to load the dotenv under pytest (or when ATELES_SKIP_DOTENV is set),
so an operator env var never reaches a test process's os.environ unless the
test opts in itself.

These tests exercise the decision function and the loader directly, and
never read or print the real ~/.config/neotoma/.env — only a temp file this
test writes and deletes.
"""

from __future__ import annotations

import os

from lib.daemon_runtime import (
    _dotenv_path,
    _dotenv_should_load,
    _load_dotenv_into_environ,
)


def test_should_load_true_outside_pytest_with_no_skip_flag():
    """Baseline: absent both signals, the loader would run (this is what a
    launchd-started daemon sees — no pytest module, no skip flag)."""
    assert _dotenv_should_load(environ={}, modules={}) is True


def test_should_not_load_when_pytest_module_present():
    assert _dotenv_should_load(environ={}, modules={"pytest": object()}) is False


def test_should_not_load_when_pytest_current_test_env_set():
    """PYTEST_CURRENT_TEST is set by pytest itself for the duration of each
    test; honoring it (not just `"pytest" in sys.modules`) covers a caller
    that imports lib.daemon_runtime from a subprocess pytest spawns without
    the parent's sys.modules."""
    assert (
        _dotenv_should_load(environ={"PYTEST_CURRENT_TEST": "x::y"}, modules={})
        is False
    )


def test_should_not_load_when_explicit_skip_flag_set():
    assert _dotenv_should_load(environ={"ATELES_SKIP_DOTENV": "1"}, modules={}) is False
    assert (
        _dotenv_should_load(environ={"ATELES_SKIP_DOTENV": "true"}, modules={})
        is False
    )


def test_should_load_when_skip_flag_falsy_or_absent():
    assert _dotenv_should_load(environ={"ATELES_SKIP_DOTENV": "0"}, modules={}) is True
    assert _dotenv_should_load(environ={"ATELES_SKIP_DOTENV": ""}, modules={}) is True


def test_dotenv_path_overridable_for_tests():
    """ATELES_DOTENV_FILE lets a test point the loader at a fixture file
    instead of the operator's real one."""
    assert _dotenv_path(environ={"ATELES_DOTENV_FILE": "/tmp/fixture.env"}) == __import__(
        "pathlib"
    ).Path("/tmp/fixture.env")


def test_canary_gate_var_from_a_simulated_operator_dotenv_does_not_leak_into_test_env(
    tmp_path,
):
    """The regression this defect actually produced: a machine whose
    materialized dotenv sets ATELES_SWARM_REQUIRE_LABEL=swarm-canary must
    NOT have that value appear in a test process's environment merely
    because something imported lib.daemon_runtime.

    Simulates the operator's file with a temp dotenv (never the real one)
    and drives the loader exactly as module import does: through
    `_dotenv_should_load` gating `_load_dotenv_into_environ`. Before the fix
    (i.e. calling `_load_dotenv_into_environ` unconditionally, as the old
    module-level code did), this assertion fails because the fake file's
    ATELES_SWARM_REQUIRE_LABEL lands in `environ`.
    """
    fake_dotenv = tmp_path / "dotenv_simulated_operator_env"
    fake_dotenv.write_text(
        "ATELES_SWARM_REQUIRE_LABEL=swarm-canary\n"
        "SOME_OTHER_OPERATOR_SWITCH=on\n"
    )

    # A fresh dict standing in for os.environ — pytest genuinely is running
    # this test, so `modules` reflects that truthfully.
    simulated_environ: dict[str, str] = {"ATELES_DOTENV_FILE": str(fake_dotenv)}
    import sys

    if _dotenv_should_load(environ=simulated_environ, modules=sys.modules):
        _load_dotenv_into_environ(
            _dotenv_path(environ=simulated_environ), simulated_environ
        )

    assert "ATELES_SWARM_REQUIRE_LABEL" not in simulated_environ, (
        "the label-gate switch from a simulated operator dotenv leaked into "
        "the test environment; the dotenv loader must be skipped under pytest"
    )
    assert "SOME_OTHER_OPERATOR_SWITCH" not in simulated_environ


def test_loader_would_have_picked_up_the_var_absent_the_pytest_guard(tmp_path):
    """Positive control for the test above: proves the fixture file and the
    loader both actually work, so the prior test's negative result is
    meaningful rather than a fixture that could never fail. Calls
    `_load_dotenv_into_environ` directly (bypassing `_dotenv_should_load`,
    as a real launchd-started daemon does), confirming the var WOULD be
    set if nothing skipped the load.
    """
    fake_dotenv = tmp_path / "dotenv_simulated_operator_env_unguarded"
    fake_dotenv.write_text("ATELES_SWARM_REQUIRE_LABEL=swarm-canary\n")

    environ: dict[str, str] = {}
    _load_dotenv_into_environ(fake_dotenv, environ)

    assert environ.get("ATELES_SWARM_REQUIRE_LABEL") == "swarm-canary"


def test_real_process_environ_unaffected_by_import(monkeypatch):
    """End-to-end sanity: importing lib.daemon_runtime in THIS test process
    (already done at module load, transitively, by every other test in this
    file importing from lib.daemon_runtime) must not have set
    ATELES_SWARM_REQUIRE_LABEL in the real os.environ from any real
    operator dotenv that might exist on this machine. This never reads or
    prints that file's contents — it only asserts the var's absence unless
    this test process's own environment already carried it in before pytest
    started (which it does not, on CI or a clean shell)."""
    # If the parent shell happened to export this for some other reason,
    # this test cannot distinguish that from a leak — so only assert when
    # it was not already present before this test ran.
    if "ATELES_SWARM_REQUIRE_LABEL_PYTEST_PRECHECK" not in os.environ:
        assert os.environ.get("ATELES_SWARM_REQUIRE_LABEL") in (None, ""), (
            "ATELES_SWARM_REQUIRE_LABEL is set in the real test process "
            "environment; the dotenv bootstrap in lib/daemon_runtime "
            "may have loaded the operator's real dotenv despite pytest running"
        )
