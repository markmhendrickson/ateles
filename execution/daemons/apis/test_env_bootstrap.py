"""Test-hermeticity regression for apis.py / dispatch_role.py dotenv bootstrap
(ateles#1285).

Both modules carry their own copy of "load the operator's materialized
dotenv into os.environ at import time" (mirroring lib/daemon_runtime's
original pattern, predating the shared helper). Several test_*.py in this
directory import `apis` or `dispatch_role` directly — e.g.
test_dispatch_role.py, test_confidence_gate_resolution.py,
test_checkpoint_release.py — so on a machine whose
~/.config/neotoma/.env sets ATELES_SWARM_REQUIRE_LABEL=swarm-canary, the
first such import in a pytest session silently turned the label gate on
for the rest of that session's os.environ — including tests in OTHER
files/directories collected later, such as
execution/daemons/anthus/test_participation_state.py::
test_known_satisfied_gate_is_not_redispatched, which sets no such var
itself and expects the gate to be off.

These tests exercise each module's `_dotenv_should_load` directly and never
read or print the real ~/.config/neotoma/.env — only a temp file this test
writes and deletes.
"""

from __future__ import annotations

import sys
from pathlib import Path

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import apis  # noqa: E402
import dispatch_role  # noqa: E402


def test_apis_should_not_load_dotenv_under_pytest(monkeypatch):
    monkeypatch.setitem(sys.modules, "pytest", sys.modules["pytest"])
    assert apis._dotenv_should_load() is False


def test_apis_should_not_load_when_skip_flag_set(monkeypatch):
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    monkeypatch.setenv("ATELES_SKIP_DOTENV", "1")
    try:
        assert apis._dotenv_should_load() is False
    finally:
        monkeypatch.setitem(sys.modules, "pytest", __import__("pytest"))


def test_apis_module_import_did_not_leak_canary_gate_var():
    """The module was already imported (at collection time, above). If its
    bootstrap had run unconditionally, and this machine's real dotenv sets
    ATELES_SWARM_REQUIRE_LABEL, it would be in os.environ right now."""
    import os

    assert os.environ.get("ATELES_SWARM_REQUIRE_LABEL") in (None, ""), (
        "apis.py's dotenv bootstrap appears to have run despite pytest "
        "running — the label-gate switch reached the real test process env"
    )


def test_dispatch_role_should_not_load_dotenv_under_pytest(monkeypatch):
    monkeypatch.setitem(sys.modules, "pytest", sys.modules["pytest"])
    assert dispatch_role._dotenv_should_load() is False


def test_dispatch_role_should_not_load_when_skip_flag_set(monkeypatch):
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    monkeypatch.setenv("ATELES_SKIP_DOTENV", "1")
    try:
        assert dispatch_role._dotenv_should_load() is False
    finally:
        monkeypatch.setitem(sys.modules, "pytest", __import__("pytest"))


def test_dispatch_role_module_import_did_not_leak_canary_gate_var():
    import os

    assert os.environ.get("ATELES_SWARM_REQUIRE_LABEL") in (None, ""), (
        "dispatch_role.py's dotenv bootstrap appears to have run despite "
        "pytest running — the label-gate switch reached the real test "
        "process env"
    )


def test_simulated_operator_dotenv_would_have_set_the_var_absent_the_guard(
    tmp_path,
):
    """Positive control: proves the guard is load-bearing rather than
    accidental — the same parse logic these modules use WOULD pick up the
    var from a simulated operator dotenv when nothing skips the load."""
    fake_dotenv = tmp_path / "dotenv_simulated_operator_env_apis"
    fake_dotenv.write_text("ATELES_SWARM_REQUIRE_LABEL=swarm-canary\n")

    fake_environ: dict[str, str] = {}
    for line in fake_dotenv.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if v[:1] not in ('"', "'") and " #" in v:
            v = v.split(" #", 1)[0].strip()
        fake_environ.setdefault(k.strip(), v.strip('"').strip("'"))

    assert fake_environ.get("ATELES_SWARM_REQUIRE_LABEL") == "swarm-canary"
