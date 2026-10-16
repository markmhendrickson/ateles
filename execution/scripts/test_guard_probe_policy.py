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
    effects = [command for command in calls if hlr._REF_EFFECT_PROGRAM in command]
    assert len(effects) == len(hlr._REF_EFFECT_PATHS) * len(hlr._REF_EFFECT_OPERATIONS) * 2
    for operation in hlr._REF_EFFECT_OPERATIONS:
        assert sum(operation in command for command in effects) == 8


def test_injected_prohibited_probe_plan_refuses_without_any_process(tmp_path, monkeypatch):
    monkeypatch.setattr(hlr, "trusted_sandbox_exec_path", lambda: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(
        hlr, "_ref_effect_command", lambda *args: ["git", "stash", "push"]
    )
    launched = []

    def unexpected_launch(*args, **kwargs):
        launched.append(args)
        raise AssertionError("invalid probe plan reached subprocess")

    monkeypatch.setattr(hlr.subprocess, "run", unexpected_launch)
    assert hlr.probe_stash_ref_effects_denied(
        ["/usr/bin/sandbox-exec", "-f", str(tmp_path / "profile.sb")], tmp_path
    ) is False
    assert launched == []


@pytest.mark.parametrize("wrapper", [[], ["sandbox-exec"], ["/tmp/sandbox-exec", "-f", "/tmp/p"]])
def test_invalid_wrapper_refuses_without_any_process(tmp_path, monkeypatch, wrapper):
    monkeypatch.setattr(
        hlr.subprocess, "run", lambda *a, **k: pytest.fail("unbound probe launched")
    )
    assert hlr.probe_stash_ref_effects_denied(wrapper, tmp_path) is False


def test_failed_observations_do_not_count_as_unchanged(tmp_path, monkeypatch):
    def unreadable(*args, **kwargs):
        raise OSError("synthetic observation failure")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert hlr.probe_stash_ref_effects_denied([], tmp_path) is False


def test_denial_of_every_command_does_not_count_as_a_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(hlr, "trusted_sandbox_exec_path", lambda: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(
        hlr.subprocess, "run",
        lambda command, **kw: subprocess.CompletedProcess(command, 1, "", "failed"),
    )
    assert hlr.probe_stash_ref_effects_denied(
        ["/usr/bin/sandbox-exec", "-f", str(tmp_path / "profile.sb")], tmp_path
    ) is False


@pytest.mark.skipif(
    hlr.trusted_sandbox_exec_path() is None,
    reason="actual removed-deny control requires the trusted macOS sandbox",
)
def test_removed_ref_deny_makes_full_preflight_refuse(tmp_path, monkeypatch):
    auth = tmp_path / "fixture-auth.json"
    auth.write_text("synthetic auth capability", encoding="utf-8")
    monkeypatch.setattr(hlr, "_codex_auth_source", lambda: auth)
    monkeypatch.setattr(hlr, "_STASH_REF_DENY_REGEXES", ())

    sandbox = hlr.HarnessSandbox.build("codex", tmp_path / "sandbox")

    assert sandbox.credential_read_denied is True
    assert sandbox.credential_binding_protected is True
    assert sandbox.user_config_write_denied is True
    assert sandbox.review_write_confined is True
    assert sandbox.git_stash_denied is False
    assert sandbox.fully_guarded is False
    assert hlr.refuse_if_guard_required(sandbox) is not None


def test_advisory_shim_preserves_ordinary_git_commands(tmp_path):
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path / "sandbox")
    shim = Path(sandbox.env_extra["PATH"].split(":")[0]) / "git"
    result = subprocess.run([str(shim), "-C", str(tmp_path), "--version"], capture_output=True)
    assert result.returncode == 0
    assert result.stdout.startswith(b"git version")
