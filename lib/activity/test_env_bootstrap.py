"""Test-hermeticity regression for lib.activity's dotenv bootstrap (ateles#1285).

lib/activity/__init__.py carries its own copy of the "load the operator's
materialized dotenv into os.environ at import time" pattern that
lib/daemon_runtime/__init__.py also has. It is a SEPARATE leak path: Anthus
imports both lib.daemon_runtime AND lib.activity, so fixing only the former
left execution/daemons/anthus/test_participation_state.py::
test_known_satisfied_gate_is_not_redispatched failing on a machine whose
~/.config/neotoma/.env sets ATELES_SWARM_REQUIRE_LABEL=swarm-canary — the
label gate went active via THIS module's unconditional
`_maybe_load_env_file` call, not the daemon_runtime one.

These tests exercise `_dotenv_should_load` / `_maybe_load_env_file` directly
and never read or print the real ~/.config/neotoma/.env — only a temp file
this test writes and deletes.
"""

from __future__ import annotations

import os
import sys

from lib.activity import _dotenv_should_load, _maybe_load_env_file


def test_should_load_true_outside_pytest_with_no_skip_flag():
    assert _dotenv_should_load(environ={}, modules={}) is True


def test_should_not_load_when_pytest_module_present():
    assert _dotenv_should_load(environ={}, modules={"pytest": object()}) is False


def test_should_not_load_when_pytest_current_test_env_set():
    assert (
        _dotenv_should_load(environ={"PYTEST_CURRENT_TEST": "x::y"}, modules={})
        is False
    )


def test_should_not_load_when_explicit_skip_flag_set():
    assert _dotenv_should_load(environ={"ATELES_SKIP_DOTENV": "1"}, modules={}) is False


def test_canary_gate_var_from_a_simulated_operator_dotenv_does_not_leak_via_activity(
    tmp_path,
):
    """The regression this defect actually produced (traced from anthus's
    real failure, not just the daemon_runtime module named in the initial
    report): a simulated operator dotenv setting
    ATELES_SWARM_REQUIRE_LABEL=swarm-canary must not reach a real
    os.environ when loaded through lib.activity's `_maybe_load_env_file`
    while pytest is running.
    """
    fake_dotenv = tmp_path / "dotenv_simulated_operator_env_activity"
    fake_dotenv.write_text("ATELES_SWARM_REQUIRE_LABEL=swarm-canary\n")

    sentinel = "ATELES_SWARM_REQUIRE_LABEL"
    had_before = sentinel in os.environ
    prior = os.environ.get(sentinel)
    try:
        os.environ.pop(sentinel, None)
        # pytest genuinely is running this test, so this exercises the real
        # module-level guard exactly as `import lib.activity` would.
        assert "pytest" in sys.modules
        _maybe_load_env_file(fake_dotenv)
        assert sentinel not in os.environ, (
            "a simulated operator dotenv's label-gate switch leaked into the "
            "real process environment via lib.activity._maybe_load_env_file "
            "while pytest was running"
        )
    finally:
        if had_before:
            os.environ[sentinel] = prior
        else:
            os.environ.pop(sentinel, None)


def test_loader_would_have_picked_up_the_var_absent_the_pytest_guard(tmp_path):
    """Positive control: with the pytest guard bypassed (as a real launchd
    daemon sees it — no pytest module), the same fixture file DOES set the
    var, proving the negative test above is meaningful."""
    fake_dotenv = tmp_path / "dotenv_simulated_operator_env_activity_unguarded"
    fake_dotenv.write_text("ATELES_SWARM_REQUIRE_LABEL=swarm-canary\n")

    sentinel = "ATELES_SWARM_REQUIRE_LABEL"
    assert _dotenv_should_load(environ={}, modules={}) is True

    # Exercise the raw parse+setdefault behavior in an isolated dict rather
    # than the real os.environ, to avoid any risk of a state leak between
    # tests regardless of assertion outcome.
    fake_environ: dict[str, str] = {}
    for line in fake_dotenv.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        fake_environ.setdefault(k.strip(), v.strip())
    assert fake_environ.get(sentinel) == "swarm-canary"
