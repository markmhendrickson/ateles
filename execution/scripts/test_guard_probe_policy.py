"""The real guard preflight must respect the same command policy as its child.

Every prohibited-plan control is intercepted before subprocess execution.
Real effect probes use only disposable file operations, never Git subcommands.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "execution" / "scripts"))
import harness_lens_runner as hlr  # noqa: E402


@pytest.mark.skipif(
    hlr.trusted_sandbox_exec_path() is None,
    reason="actual effect controls require the trusted macOS sandbox",
)
def test_full_preflight_never_executes_prohibited_git_plan(tmp_path, monkeypatch):
    auth = tmp_path / "fixture-auth.json"
    auth.write_text("synthetic auth capability", encoding="utf-8")
    monkeypatch.setattr(hlr, "_codex_auth_source", lambda: auth)
    real_run = subprocess.run
    calls = []

    def checked_run(command, *args, **kwargs):
        calls.append(command)
        if "stash" in command:
            raise AssertionError("prohibited preflight command planned; not executed")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(hlr.subprocess, "run", checked_run)
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path / "sandbox")

    assert calls
    assert sandbox.git_stash_denied is True
    assert sandbox.fully_guarded is True
    assert hlr.refuse_if_guard_required(sandbox) is None
