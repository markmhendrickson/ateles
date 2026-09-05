"""Tests for the named-role dispatch entrypoint (dispatch_role).

The entrypoint is deliberately thin — routing, identity, and audit all live in
skill_runner/harness_router and are covered by their own suites. What is tested
here is the surface this module actually owns: role resolution against the
SKILL.md set, refusal before any dispatch is attempted, forwarding of the
provider override, and the headroom-source reporting that makes the
file-beats-env precedence visible.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import dispatch_role  # noqa: E402
from skill_runner import SkillResult  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """An ATELES_REPO whose .claude/skills holds two roles."""
    skills = tmp_path / ".claude" / "skills"
    for role in ("cicada", "pavo"):
        (skills / role).mkdir(parents=True)
        (skills / role / "SKILL.md").write_text(f"# {role}\n", encoding="utf-8")
    # A directory without a SKILL.md must NOT count as dispatchable.
    (skills / "not-a-role").mkdir(parents=True)
    monkeypatch.setattr(dispatch_role, "ATELES_REPO", tmp_path)
    return tmp_path


def test_available_roles_requires_a_skill_md(fake_repo) -> None:
    assert dispatch_role.available_roles() == ["cicada", "pavo"]


def test_preflight_accepts_a_known_role(fake_repo) -> None:
    assert dispatch_role._preflight("cicada", provider=None) is None


def test_preflight_refuses_an_unknown_role(fake_repo) -> None:
    refusal = dispatch_role._preflight("nosuchrole", provider=None)
    assert refusal is not None
    # The refusal must name the valid options, not just say "no".
    assert "cicada" in refusal and "pavo" in refusal


def test_preflight_refuses_a_provider_outside_the_configured_order(
    fake_repo, monkeypatch
) -> None:
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude,cursor")
    refusal = dispatch_role._preflight("cicada", provider="codex")
    assert refusal is not None and "codex" in refusal


def test_preflight_allows_a_provider_inside_the_configured_order(
    fake_repo, monkeypatch
) -> None:
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude,codex,cursor")
    assert dispatch_role._preflight("cicada", provider="codex") is None


def test_unknown_role_exits_nonzero_without_dispatching(
    fake_repo, monkeypatch
) -> None:
    """A refused role must never reach run_skill — a rejected request should
    leave no harness_event suggesting work was attempted."""
    called = False

    async def _boom(*a, **k):  # pragma: no cover - must not run
        nonlocal called
        called = True
        raise AssertionError("run_skill called for a refused role")

    monkeypatch.setattr(dispatch_role, "run_skill", _boom)
    rc = dispatch_role.main(["--role", "nosuchrole", "--task", "hi"])
    assert rc == 1
    assert called is False


def test_dispatch_forwards_role_provider_and_cwd(monkeypatch) -> None:
    """The role name must be passed as BOTH skill and role: skill_runner reads
    <skill>/SKILL.md and loads the <role> agent_definition, and in this codebase
    those are the same string."""
    seen: dict = {}

    async def _capture(skill, prompt, **kwargs):
        seen["skill"] = skill
        seen["prompt"] = prompt
        seen.update(kwargs)
        return SkillResult(skill, True, 0, "out", "", provider="codex")

    monkeypatch.setattr(dispatch_role, "run_skill", _capture)

    import asyncio

    result = asyncio.run(
        dispatch_role.dispatch(
            "cicada", "do the thing", provider="codex", cwd="/tmp/wt", timeout=42
        )
    )
    assert result.ok
    assert seen["skill"] == "cicada"
    assert seen["role"] == "cicada"
    assert seen["provider"] == "codex"
    assert seen["cwd"] == "/tmp/wt"
    assert seen["timeout"] == 42
    assert seen["prompt"] == "do the thing"


def test_dispatch_without_override_leaves_provider_to_the_router(
    monkeypatch,
) -> None:
    seen: dict = {}

    async def _capture(skill, prompt, **kwargs):
        seen.update(kwargs)
        return SkillResult(skill, True, 0, "", "", provider="cursor")

    monkeypatch.setattr(dispatch_role, "run_skill", _capture)

    import asyncio

    asyncio.run(dispatch_role.dispatch("cicada", "work"))
    # None, not a default string: run_skill treats None as "route normally".
    assert seen["provider"] is None


def test_failed_run_exits_nonzero(fake_repo, monkeypatch) -> None:
    async def _fail(skill, prompt, **kwargs):
        return SkillResult(
            skill, False, 1, "", "boom", error="provider exploded", provider="codex"
        )

    monkeypatch.setattr(dispatch_role, "run_skill", _fail)
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())
    rc = dispatch_role.main(
        ["--role", "cicada", "--task", "x", "--provider", "codex"]
    )
    assert rc == 1


def test_successful_run_exits_zero(fake_repo, monkeypatch) -> None:
    async def _ok(skill, prompt, **kwargs):
        return SkillResult(skill, True, 0, "branch-name", "", provider="codex")

    monkeypatch.setattr(dispatch_role, "run_skill", _ok)
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())
    rc = dispatch_role.main(
        ["--role", "cicada", "--task", "x", "--provider", "codex"]
    )
    assert rc == 0


def _stub_def():
    from lib.daemon_runtime import AgentDefinition

    return AgentDefinition(
        name="cicada",
        aauth_sub="cicada@ateles-swarm",
        tier="T4",
        prompt_markdown="# cicada",
        tool_allowlist=["Bash", "Read"],
    )


def test_headroom_note_names_the_file_when_one_exists(tmp_path, monkeypatch) -> None:
    """configured_headroom() takes the FIRST of (file, env) that parses, so an
    env value does NOT override an existing file. The note must therefore say
    which source actually won, or a stale file stays silently authoritative."""
    hf = tmp_path / "headroom.json"
    hf.write_text('{"claude": 0.15, "codex": 1.0, "cursor": 1.0}', encoding="utf-8")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(hf))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"claude": 1.0}')
    note = dispatch_role._headroom_note()
    assert str(hf) in note
    # The file's values win, not the env's.
    assert "claude=0.15" in note


def test_headroom_note_falls_back_to_env_when_no_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "absent.json")
    )
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"claude": 0.2}')
    note = dispatch_role._headroom_note()
    assert "env APIS_HARNESS_HEADROOM" in note
    assert "claude=0.2" in note


def test_inconsistent_tool_allowlist_types_all_coerce(monkeypatch) -> None:
    """tool_allowlist is inconsistently typed across agent_definition entities
    (arrays, one comma/JSON string, some nulls). Every shape must yield a usable
    list — a null must mean 'all tools', never a crash or an empty allowlist
    that would confine the agent to nothing."""
    from lib.daemon_runtime import AgentDefinition

    assert AgentDefinition(tool_allowlist=None).tools == ["*"]
    assert AgentDefinition(tool_allowlist="*").tools == ["*"]
    assert AgentDefinition(tool_allowlist="").tools == ["*"]
    assert AgentDefinition(tool_allowlist=["Bash", "Read"]).tools == ["Bash", "Read"]
    assert AgentDefinition(tool_allowlist='["Bash", "Read"]').tools == [
        "Bash",
        "Read",
    ]
    assert AgentDefinition(tool_allowlist="Bash,Read").tools == ["Bash", "Read"]


# ── ateles#585: the envelope must be unconditional ────────────────────────────
#
# The defect these cover: dispatch_role wrote its JSON envelope only after
# asyncio.run() returned, so anything killing the process mid-dispatch left a
# 0-byte output file, three healthy-looking banner lines on stderr, and no
# other trace. The caller could not tell "still working" from "died", and read
# the silence as success.
#
# The pre-existing suite passed throughout, because it only ever exercised the
# success path and refusals that return before dispatch. These tests exercise
# what was actually broken: every route by which the process can stop WITHOUT
# a clean SkillResult.


def _envelope(capsys) -> dict:
    """Parse the single JSON envelope from captured stdout.

    Asserts on the count as well as the content: two envelopes in one stream is
    a parse error for the caller, which is as unusable as zero. This is what
    pins the _Emitter idempotency latch.
    """
    out = capsys.readouterr().out.strip()
    assert out, "no envelope was written at all — this is the #585 defect"
    decoder = json.JSONDecoder()
    obj, end = decoder.raw_decode(out)
    assert not out[end:].strip(), f"more than one envelope written: {out!r}"
    return obj


def test_unhandled_exception_in_dispatch_still_writes_a_failure_envelope(
    fake_repo, monkeypatch, capsys
) -> None:
    """An exception escaping run_skill must become ok:false, not a traceback
    over an empty file. run_skill is documented to return a SkillResult for
    every failure it anticipates — this covers the ones it does not."""

    async def _explode(*a, **k):
        raise RuntimeError("provider adapter blew up")

    monkeypatch.setattr(dispatch_role, "run_skill", _explode)
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())

    rc = dispatch_role.main(
        ["--role", "cicada", "--task", "x", "--provider", "codex", "--json"]
    )

    assert rc == 1, "a crashed dispatch must exit non-zero"
    env = _envelope(capsys)
    assert env["ok"] is False
    assert "RuntimeError" in env["reason"]
    assert "provider adapter blew up" in env["reason"]


def test_a_failed_run_envelope_carries_a_reason(fake_repo, monkeypatch, capsys) -> None:
    """ok:false must always be accompanied by a reason. An envelope saying only
    'false' sends the caller back to the stderr they already could not read."""

    async def _fail(skill, prompt, **kwargs):
        return SkillResult(
            skill, False, 1, "", "boom", error="provider exploded", provider="codex"
        )

    monkeypatch.setattr(dispatch_role, "run_skill", _fail)
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())

    rc = dispatch_role.main(
        ["--role", "cicada", "--task", "x", "--provider", "codex", "--json"]
    )

    assert rc == 1
    env = _envelope(capsys)
    assert env["ok"] is False
    assert env["reason"] == "provider exploded"
    assert env["returncode"] == 1


def test_successful_run_envelope_has_no_spurious_reason(
    fake_repo, monkeypatch, capsys
) -> None:
    """The reason key belongs only to failures — a caller keying on its
    presence must not be misled by a successful run."""

    async def _ok(skill, prompt, **kwargs):
        return SkillResult(skill, True, 0, "branch-name", "", provider="codex")

    monkeypatch.setattr(dispatch_role, "run_skill", _ok)
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())

    rc = dispatch_role.main(
        ["--role", "cicada", "--task", "x", "--provider", "codex", "--json"]
    )

    assert rc == 0
    env = _envelope(capsys)
    assert env["ok"] is True
    assert "reason" not in env
    assert env["stdout"] == "branch-name"


def test_refused_role_writes_a_failure_envelope(fake_repo, monkeypatch, capsys) -> None:
    """A refusal is still a dispatch that produced no work. Under --json it
    owes the caller an envelope, not just a stderr line."""
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())

    rc = dispatch_role.main(["--role", "nosuchrole", "--task", "x", "--json"])

    assert rc == 1
    env = _envelope(capsys)
    assert env["ok"] is False
    assert "nosuchrole" in env["reason"]


def test_unreadable_task_file_writes_a_failure_envelope(
    fake_repo, monkeypatch, capsys, tmp_path
) -> None:
    """A missing --task-file used to raise straight out of main() as a
    traceback with an empty envelope."""
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())

    rc = dispatch_role.main(
        [
            "--role",
            "cicada",
            "--task-file",
            str(tmp_path / "does-not-exist.md"),
            "--json",
        ]
    )

    assert rc == 1
    env = _envelope(capsys)
    assert env["ok"] is False
    assert "does-not-exist.md" in env["reason"]


def test_unreadable_stdin_writes_a_failure_envelope(
    fake_repo, monkeypatch, capsys
) -> None:
    """`--task -` reads stdin outside the try in an earlier revision (ateles#592
    review); a closed or blocked stdin raised straight out of main() as a
    traceback with an empty envelope, mirroring the --task-file case above."""
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())

    def _raise():
        raise OSError("stdin is closed")

    monkeypatch.setattr(sys.stdin, "read", _raise)

    rc = dispatch_role.main(["--role", "cicada", "--task", "-", "--json"])

    assert rc == 1
    env = _envelope(capsys)
    assert env["ok"] is False
    assert "stdin" in env["reason"]


def test_empty_task_writes_a_failure_envelope(fake_repo, monkeypatch, capsys) -> None:
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())

    rc = dispatch_role.main(["--role", "cicada", "--task", "   \n  ", "--json"])

    assert rc == 1
    env = _envelope(capsys)
    assert env["ok"] is False
    assert "empty" in env["reason"]


@pytest.mark.parametrize(
    "argv, expected_fragment",
    [
        (["--json"], "--role is required"),
        (["--role", "cicada", "--json"], "one of --task, --task-file is required"),
    ],
    ids=["missing_role", "missing_task"],
)
def test_usage_error_writes_a_failure_envelope(
    fake_repo, monkeypatch, capsys, argv, expected_fragment
) -> None:
    """A usage error owes the --json caller an envelope too (ateles#592 review).

    `parser.error` raises SystemExit(2) straight past the emitter, so these two
    paths produced exit 2 with 0 bytes on stdout — the exact ateles#585
    signature the module's own docstring calls unreachable. Reproduced before
    the fix by running the entrypoint as a subprocess.

    Exit 2 is deliberately preserved: it is the conventional usage status and a
    caller may already branch on it. What changes is that stdout is no longer
    empty.
    """
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())

    rc = dispatch_role.main(argv)

    assert rc == 2, "usage errors keep argparse's conventional exit status"
    env = _envelope(capsys)
    assert env["ok"] is False
    assert expected_fragment in env["reason"]
    assert env["error"] == "usage error"


def test_usage_error_is_silent_on_stdout_without_json(fake_repo, monkeypatch, capsys) -> None:
    """Without --json the human-readable mode keeps a clean stdout; the usage
    message still reaches stderr, as argparse always did."""
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())

    rc = dispatch_role.main(["--role", "cicada"])

    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "one of --task, --task-file is required" in captured.err


@pytest.mark.parametrize(
    "argv, expected_fragment",
    [
        (["--role", "cicada", "--task", "x", "--json", "--timeout", "nope"], "timeout"),
        (["--role", "cicada", "--task", "x", "--json", "--bogus-flag"], "bogus-flag"),
    ],
    ids=["invalid_timeout_type", "unknown_flag"],
)
def test_argparse_validation_error_writes_a_failure_envelope(
    fake_repo, monkeypatch, capsys, argv, expected_fragment
) -> None:
    """``parse_args`` failures must not bypass the emitter (ateles#592 review).

    Unknown flags and bad types call ``parser.error`` during parsing, before
    ``args.json`` is available — the emitter must be armed from a raw ``--json``
    peek instead.
    """
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())

    rc = dispatch_role.main(argv)

    assert rc == 2
    env = _envelope(capsys)
    assert env["ok"] is False
    assert expected_fragment in env["reason"]
    assert env["error"] == "usage error"


def test_invalid_utf8_task_file_writes_a_failure_envelope(
    fake_repo, monkeypatch, capsys, tmp_path
) -> None:
    """``read_text(encoding='utf-8')`` raises ``UnicodeDecodeError``, not
    ``OSError`` — it must be caught like the stdin path."""
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda r: _stub_def())
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe not utf-8")

    rc = dispatch_role.main(
        ["--role", "cicada", "--task-file", str(bad), "--json"]
    )

    assert rc == 1
    env = _envelope(capsys)
    assert env["ok"] is False
    assert "bad.md" in env["reason"]


def test_emitter_writes_at_most_one_envelope(capsys) -> None:
    """The signal path and the normal path race: a SIGTERM can arrive while the
    result is being written. Two concatenated JSON objects are a parse error
    for the caller — as unusable as zero.

    Asserted on STDOUT, not on the private `_done` flag: the flag is an
    implementation detail, and a version that latched it while still writing
    every time passed the old assertion (ateles#592 review, demonstrated). What
    the caller is owed is one parseable document.
    """
    emitter = dispatch_role._Emitter(enabled=True, role="cicada")
    emitter.emit({"ok": True})
    emitter.emit_failure("a later signal")

    out = capsys.readouterr().out
    parsed = json.loads(out)  # raises if a second envelope was appended
    assert parsed["ok"] is True, "the first envelope wins; the later one is dropped"
    assert out.count('"role"') == 1, out


def test_emitter_stays_silent_without_json(capsys) -> None:
    """Human-readable mode keeps its plain-text output; the envelope is a
    --json affordance and must not corrupt a piped agent transcript."""
    emitter = dispatch_role._Emitter(enabled=False, role="cicada")
    emitter.emit_failure("something failed")
    assert capsys.readouterr().out == ""


def test_emitter_survives_a_closed_stdout(monkeypatch) -> None:
    """A broken pipe while writing the envelope must not replace the reported
    failure with a traceback about the reporting itself."""

    class _Broken:
        def write(self, _):
            raise BrokenPipeError("closed")

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", _Broken())
    dispatch_role._Emitter(enabled=True, role="cicada").emit_failure("boom")


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP", "SIGINT"])
def test_fatal_signal_writes_an_envelope_and_dies_by_that_signal(
    signame, tmp_path
) -> None:
    """The #585 reproduction, end to end, as a real subprocess.

    A unit test cannot cover this: the defect was that the process DIED, and
    only a real signal to a real process exercises the disposition that killed
    it. SIGTERM and SIGHUP are the two observed killers — a harness that
    backgrounds or times out its shell call delivers one of them. SIGINT is
    included at this subprocess level too (ateles#592 review, non-blocking):
    it was registered by `_install_signal_envelope` alongside the other two
    but only exercised by name in an earlier revision, not proven end-to-end
    the way SIGTERM/SIGHUP were — it is the operator's Ctrl-C path and worth
    the same proof.

    run_skill is stubbed to sleep so the signal lands mid-dispatch, in exactly
    the window that previously produced a 0-byte file.
    """
    import json as _json
    import os
    import select
    import signal as _signal
    import subprocess
    import time

    skills = tmp_path / ".claude" / "skills" / "cicada"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# cicada\n", encoding="utf-8")

    driver = tmp_path / "driver.py"
    driver.write_text(
        "import asyncio, sys\n"
        f"sys.path.insert(0, {str(_DAEMON_DIR)!r})\n"
        "import dispatch_role\n"
        "from lib.daemon_runtime import AgentDefinition\n"
        "async def _slow(*a, **k):\n"
        "    await asyncio.sleep(120)\n"
        "dispatch_role.run_skill = _slow\n"
        "dispatch_role._load_agent_def = lambda r: AgentDefinition(\n"
        "    name='cicada', aauth_sub='cicada@ateles-swarm', tier='T4',\n"
        "    prompt_markdown='# cicada', tool_allowlist=['Bash'])\n"
        f"dispatch_role.ATELES_REPO = {str(tmp_path)!r}\n"
        "import pathlib\n"
        f"dispatch_role.ATELES_REPO = pathlib.Path({str(tmp_path)!r})\n"
        "raise SystemExit(dispatch_role.main(\n"
        "    ['--role', 'cicada', '--task', 'x', '--provider', 'codex', '--json']))\n",
        encoding="utf-8",
    )

    env = {**os.environ, "PYTHONPATH": str(_DAEMON_DIR.parent.parent.parent)}
    proc = subprocess.Popen(
        [sys.executable, str(driver)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    # Wait until the dispatch banner reaches stderr so the signal lands inside
    # the previously-fatal window, not before handlers are installed.
    deadline = time.time() + 15
    ready = False
    while time.time() < deadline and proc.poll() is None:
        rlist, _, _ = select.select([proc.stderr], [], [], 0.1)
        if rlist:
            line = proc.stderr.readline()
            if "dispatch_role: role=" in line:
                ready = True
                break
    assert ready, "dispatch never reached the in-flight window before timeout"
    proc.send_signal(getattr(_signal, signame))
    stdout, _stderr = proc.communicate(timeout=30)

    # Popen reports a signal death as -signum; a shell renders the same status
    # as 128+signum. Either way the caller sees a non-zero, non-laundered exit:
    # the handler must re-raise through SIG_DFL rather than return 0, or a
    # caller checking only the exit code would be told a killed dispatch
    # succeeded — the precise misreading that #585 caused.
    expected_signum = getattr(_signal, signame).value
    assert proc.returncode == -expected_signum, (
        f"expected death by {signame} (-{expected_signum}), "
        f"got {proc.returncode}"
    )
    assert stdout.strip(), "0-byte output on a killed dispatch — the #585 defect"
    env_obj = _json.loads(stdout)
    assert env_obj["ok"] is False
    assert signame in env_obj["reason"]
