"""Tests for harness_lens_runner.py — the runner that sends one lens review
of one PR head to a chosen harness provider (claude/codex/cursor), reusing
dispatch_role.py / harness_router rather than paralleling them.

No live codex/cursor-agent process is ever started here: every dispatch call
site (`dispatch_role.dispatch`) and every `gh` call (`gh_login`,
`current_pr_head`, `post_verdict`) is monkeypatched individually. Per the
task's own hard rule, this suite makes no model call.

The GUARD tests below are deliberately the opposite of that: they do NOT
monkeypatch `HarnessSandbox.build`, `sandbox-exec`, or the git shim. They run
the REAL probe — a real `sandbox-exec` invocation against a real fixture file
this suite creates, and a real `git stash push` against a real scratch repo
this suite `git init`s — because a mocked-CLI test cannot prove a guard binds;
only running the actual mechanism can. `sandbox-exec` and `git` are both
expected only on macOS developer hosts; the Linux CI lane exercises the
fail-closed result when ``sandbox-exec`` is unavailable. Posting/verdict unit
tests inject a deterministic ready sandbox so host capabilities cannot
short-circuit before the behavior each test names.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_DAEMON_DIR = _REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness_lens_runner as hlr  # noqa: E402
from skill_runner import SkillResult  # noqa: E402

SAMPLE_HEAD = "a" * 40

SIGNED_OFF_VERDICT = (
    f"<!-- review:pm commit={SAMPLE_HEAD} -->\n"
    "**\U0001f916 Pavo — Ateles swarm, pm lens panelist**\n"
    "**SIGNED_OFF**\n"
    "\n"
    "Summary: looks fine.\n"
)

UNREADABLE_VERDICT = "I looked at the PR and it seems okay, ship it.\n"


@pytest.fixture
def brief_file(tmp_path) -> Path:
    path = tmp_path / "lens_brief.md"
    path.write_text("BRIEF BODY\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _default_full_headroom(monkeypatch):
    """Isolate every test from the operator's REAL
    ~/.config/ateles/harness-headroom.json (codex/cursor at 0.0 as of this
    task). Without this, `configured_headroom()` reads that live file and
    every test that does not itself exercise the zero-headroom refusal would
    spuriously fail on this machine. A test that wants to exercise the
    refusal sets APIS_HARNESS_HEADROOM itself, which — per
    `harness_router.configured_headroom`'s own file-beats-env precedence —
    requires ALSO pointing APIS_HARNESS_HEADROOM_FILE at a nonexistent path,
    exactly as this fixture does.
    """
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM", json.dumps({"claude": 1.0, "codex": 1.0, "cursor": 1.0})
    )
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", "/nonexistent/harness-headroom.json")


@pytest.fixture(autouse=True)
def _deterministic_codex_auth_source(tmp_path, monkeypatch):
    """Keep unit tests independent of the developer/CI host's login state.

    Production still resolves the real ``~/.codex/auth.json`` and fails closed
    when it is absent. Tests receive only a fixture path; the explicit
    missing-auth test below proves the production refusal.
    """
    source = tmp_path / "fixture-codex-home" / "auth.json"
    source.parent.mkdir(parents=True)
    source.write_text("fixture-not-a-real-token\n", encoding="utf-8")
    monkeypatch.setattr(hlr, "_codex_auth_source", lambda: source)


@pytest.fixture
def mock_ready_sandbox(monkeypatch, tmp_path):
    """Let verdict/posting unit tests reach their own decision boundary.

    Real guard behavior is covered separately. This fixture avoids letting an
    unauthenticated Linux host turn those tests into authentication tests.
    """

    def _build(cls, provider, tmp_root):
        root = tmp_path / f"{provider}-unit-home"
        root.mkdir(parents=True, exist_ok=True)
        return hlr.HarnessSandbox(
            provider=provider,
            root=root,
            env_extra={"CODEX_HOME": str(root)} if provider == "codex" else {},
            command_wrapper=["sandbox-exec", "-f", str(root / "profile.sb")]
            if provider != "claude"
            else [],
            credential_read_denied=True,
            user_config_write_denied=True,
            git_stash_denied=True,
            authentication_ready=True,
            unavailable_guards=(),
        )

    monkeypatch.setattr(hlr.HarnessSandbox, "build", classmethod(_build))


@pytest.fixture
def target() -> hlr.LensTarget:
    return hlr.LensTarget(
        repo="markmhendrickson/ateles",
        pr=1234,
        head=SAMPLE_HEAD,
        lens="pm",
        agent="pavo",
    )


# ── Headroom refusal ------------------------------------------------------------


def test_check_headroom_passes_when_nonzero(monkeypatch):
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", json.dumps({"codex": 0.4}))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", "/nonexistent/path.json")
    assert hlr.check_headroom("codex") == 0.4


def test_check_headroom_refuses_at_exact_zero(monkeypatch):
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", json.dumps({"codex": 0.0}))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", "/nonexistent/path.json")
    with pytest.raises(hlr.HeadroomExhausted) as exc:
        hlr.check_headroom("codex")
    assert "codex" in str(exc.value)
    assert "harness-headroom.json" in str(exc.value)


def test_run_one_refuses_before_any_worktree_when_headroom_zero(
    monkeypatch, tmp_path, target, brief_file
):
    """The refusal must happen BEFORE `git worktree add` — no side effect from
    a run that never should have started."""
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", json.dumps({"codex": 0.0}))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", "/nonexistent/path.json")

    called = False

    def _boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("worktree.create called despite zero headroom")

    monkeypatch.setattr(hlr.Worktree, "create", _boom)

    import asyncio

    with pytest.raises(hlr.HeadroomExhausted):
        asyncio.run(
            hlr.run_one(
                target, provider="codex", post=False, dry_run=False,
                repo_worktree_name="ateles", scratch_root=tmp_path,
                brief_path=brief_file, timeout=None,
            )
        )
    assert called is False


# ── Guard configuration: REAL mechanism, REAL probe, no mocking -------------------
#
# Nothing in this section monkeypatches HarnessSandbox.build, sandbox-exec, git,
# or subprocess.run. Every assertion here is against what the ACTUAL mechanism
# actually did when run for real against a fixture this test created.

import platform  # noqa: E402
import shutil  # noqa: E402

_HAS_SANDBOX_EXEC = shutil.which("sandbox-exec") is not None
_IS_DARWIN = platform.system() == "Darwin"


def test_sandbox_build_codex_uses_codex_home_isolation(tmp_path):
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    assert sandbox.env_extra["CODEX_HOME"] == str(sandbox.root)
    assert sandbox.root.is_dir()


def test_sandbox_build_codex_links_auth_without_copying_it(tmp_path, monkeypatch):
    source = tmp_path / "real-codex-home" / "auth.json"
    source.parent.mkdir()
    source.write_text("fixture-not-a-real-token", encoding="utf-8")
    monkeypatch.setattr(hlr, "_codex_auth_source", lambda: source)

    sandbox = hlr.HarnessSandbox.build("codex", tmp_path / "sandbox")

    linked = sandbox.root / "auth.json"
    assert sandbox.authentication_ready is True
    assert linked.is_symlink()
    assert linked.resolve() == source.resolve()


@pytest.mark.skipif(
    not (_IS_DARWIN and _HAS_SANDBOX_EXEC),
    reason="sandbox-exec is macOS-only",
)
def test_sandbox_profile_denies_write_through_codex_auth_symlink(tmp_path):
    """The auth capability is readable, but cannot become a write path back
    into the operator's real Codex home. Use only a fixture target here.
    """
    real_auth = tmp_path / "fixture-user" / ".codex" / "auth.json"
    real_auth.parent.mkdir(parents=True)
    real_auth.write_text("fixture-not-a-real-token\n", encoding="utf-8")
    isolated = tmp_path / "isolated-codex-home"
    isolated.mkdir()
    linked = isolated / "auth.json"
    linked.symlink_to(real_auth)
    profile = isolated / "profile.sb"
    hlr.build_sandbox_exec_profile(profile)

    subprocess = __import__("subprocess")
    attempt = subprocess.run(
        [
            "sandbox-exec", "-f", str(profile),
            "sh", "-c", 'printf changed > "$1"', "probe", str(linked),
        ],
        capture_output=True,
        text=True,
    )

    assert attempt.returncode != 0
    assert real_auth.read_text(encoding="utf-8") == "fixture-not-a-real-token\n"


def test_sandbox_build_cursor_uses_home_isolation(tmp_path):
    sandbox = hlr.HarnessSandbox.build("cursor", tmp_path)
    assert sandbox.env_extra["HOME"] == str(sandbox.root)


def test_sandbox_build_claude_has_no_override(tmp_path):
    sandbox = hlr.HarnessSandbox.build("claude", tmp_path)
    assert sandbox.env_extra == {}
    assert sandbox.command_wrapper == []
    assert sandbox.unavailable_guards == ()
    assert sandbox.fully_guarded is True


def test_sandbox_build_puts_a_shim_dir_first_on_path_for_codex(tmp_path):
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    path_value = sandbox.env_extra["PATH"]
    shim_git = Path(path_value.split(":")[0]) / "git"
    assert shim_git.is_file()
    assert shim_git.stat().st_mode & 0o111  # executable


@pytest.mark.skipif(not shutil.which("git"), reason="git required for the real probe")
def test_git_shim_really_refuses_a_real_stash_push_in_a_scratch_repo(tmp_path):
    """The load-bearing test: builds the shim exactly as HarnessSandbox.build
    does, then issues a REAL `git stash push` through it against a scratch
    repo (never the shared clone), and asserts the real exit code — not a
    description of what the shim is supposed to do.
    """
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    scratch_repo = tmp_path / "real-probe-repo"
    scratch_repo.mkdir()
    subprocess = __import__("subprocess")
    subprocess.run(["git", "init", "-q", str(scratch_repo)], check=True)
    subprocess.run(
        ["git", "-C", str(scratch_repo), "config", "user.email", "probe@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(scratch_repo), "config", "user.name", "probe"], check=True
    )
    (scratch_repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(scratch_repo), "add", "-A"], check=True)
    # A REAL commit first: without one, a genuinely unshimmed `git stash push`
    # ALSO fails (with "You do not have the initial commit yet"), which would
    # make this test pass for the wrong reason on a shim that refuses nothing.
    subprocess.run(
        ["git", "-C", str(scratch_repo), "commit", "-q", "-m", "probe"], check=True
    )
    (scratch_repo / "f.txt").write_text("changed", encoding="utf-8")

    import os as _os

    shim_dir = sandbox.env_extra["PATH"].split(":")[0]
    env = {**_os.environ, "PATH": f"{shim_dir}:{_os.environ.get('PATH', '')}"}

    push = subprocess.run(
        ["git", "-C", str(scratch_repo), "stash", "push"],
        capture_output=True, text=True, env=env,
    )
    assert push.returncode != 0, "the real shim let a real stash push through"
    assert "refusing" in push.stderr.lower()

    # The read-only carve-out must still work — this is the same repo, same
    # shim, same PATH: if the shim denied stash outright rather than only
    # its mutating forms, this would also fail.
    listing = subprocess.run(
        ["git", "-C", str(scratch_repo), "stash", "list"],
        capture_output=True, text=True, env=env,
    )
    assert listing.returncode == 0, "the real shim also blocked the read-only carve-out"

    # And a completely unrelated git command must still work through the shim.
    status = subprocess.run(
        ["git", "-C", str(scratch_repo), "status"],
        capture_output=True, text=True, env=env,
    )
    assert status.returncode == 0


@pytest.mark.skipif(not shutil.which("git"), reason="git required for the real probe")
def test_sandbox_build_reports_git_stash_denied_true_from_the_real_probe(tmp_path):
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    assert sandbox.git_stash_denied is True
    assert sandbox.authentication_ready is True
    assert not any("git_stash_guard" in g for g in sandbox.unavailable_guards)


@pytest.mark.skipif(
    not (_IS_DARWIN and _HAS_SANDBOX_EXEC),
    reason="sandbox-exec is macOS-only; on this host the guard correctly "
    "reports unbound, covered by test_sandbox_probe_reports_unbound_without_sandbox_exec",
)
def test_sandbox_build_really_denies_reading_a_credential_fixture(tmp_path):
    """The load-bearing test for the read guard: builds the REAL sandbox-exec
    profile HarnessSandbox.build writes, then issues a REAL `cat` through it
    against a FIXTURE file (never a real credential file) whose path matches
    one of the deny globs, and asserts the real exit code.
    """
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    assert sandbox.credential_read_denied is True

    # Prove it against a SEPARATE, freshly created fixture too, not just the
    # one HarnessSandbox.build used internally for its own probe — this rules
    # out the probe having been faked to always report True.
    profile_path = sandbox.root / "profile.sb"
    fixture_dir = tmp_path / "second-fixture" / ".config" / "neotoma"
    fixture_dir.mkdir(parents=True)
    fixture = fixture_dir / ".env"
    fixture.write_text("SECOND_PROBE_NOT_REAL=1\n", encoding="utf-8")

    import subprocess as _subprocess

    result = _subprocess.run(
        ["sandbox-exec", "-f", str(profile_path), "cat", str(fixture)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0, "the real sandbox-exec profile let a real read through"

    # An unrelated file outside the deny globs must still be readable.
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("hi", encoding="utf-8")
    ok = _subprocess.run(
        ["sandbox-exec", "-f", str(profile_path), "cat", str(unrelated)],
        capture_output=True, text=True,
    )
    assert ok.returncode == 0
    assert ok.stdout.strip() == "hi"


@pytest.mark.skipif(
    not (_IS_DARWIN and _HAS_SANDBOX_EXEC),
    reason="sandbox-exec is macOS-only; see the read-guard test above for the "
    "companion coverage of the unbound-on-other-platforms path",
)
def test_sandbox_build_really_denies_writing_to_user_config_fixture(tmp_path):
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    assert sandbox.user_config_write_denied is True

    profile_path = sandbox.root / "profile.sb"
    fixture = tmp_path / "second-fixture-write" / ".claude" / "probe"

    import subprocess as _subprocess

    fixture.parent.mkdir(parents=True)
    result = _subprocess.run(
        ["sandbox-exec", "-f", str(profile_path), "touch", str(fixture)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0, "the real sandbox-exec profile let a real write through"
    assert not fixture.exists()


def test_sandbox_probe_reports_unbound_without_sandbox_exec(tmp_path, monkeypatch):
    """The other side of the platform split: when sandbox-exec is genuinely
    unavailable (this host, or a simulated absence), the probe must report
    False — never assume True because the profile file was written.
    """
    real_which = shutil.which
    monkeypatch.setattr(
        hlr.shutil, "which",
        lambda name: None if name == "sandbox-exec" else real_which(name),
    )
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    assert sandbox.credential_read_denied is False
    assert sandbox.user_config_write_denied is False
    assert sandbox.command_wrapper == []
    assert sandbox.fully_guarded is False
    assert any("credential_read_guard" in g for g in sandbox.unavailable_guards)
    assert any("user_config_write_guard" in g for g in sandbox.unavailable_guards)


def test_refuse_if_guard_required_allows_claude_always(tmp_path):
    sandbox = hlr.HarnessSandbox.build("claude", tmp_path)
    assert hlr.refuse_if_guard_required(sandbox) is None


def test_refuse_if_guard_required_refuses_codex_when_sandbox_exec_is_unavailable(
    tmp_path, monkeypatch
):
    """The exact defect PR #1308 shipped with: this must fire for real,
    driven by the sandbox's own probed state, not a hardcoded constant.
    """
    real_which = shutil.which
    monkeypatch.setattr(
        hlr.shutil, "which",
        lambda name: None if name == "sandbox-exec" else real_which(name),
    )
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    reason = hlr.refuse_if_guard_required(sandbox)
    assert reason is not None
    assert "codex" in reason
    assert "credential_read_denied=False" in reason


def test_refuse_if_guard_required_refuses_codex_when_authentication_is_missing(
    tmp_path, monkeypatch
):
    """Production must fail closed before dispatch when Codex auth is absent."""
    missing_auth = tmp_path / "missing-codex-home" / "auth.json"
    monkeypatch.setattr(hlr, "_codex_auth_source", lambda: missing_auth)
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    reason = hlr.refuse_if_guard_required(sandbox)
    assert sandbox.authentication_ready is False
    assert reason is not None
    assert "no authentication available" in reason
    assert "refusing before a model call" in reason


def test_refuse_if_guard_required_allows_codex_when_fully_probed_guarded(tmp_path):
    """The mirror case: when every real probe passes, the run may proceed.
    This is the ONLY test in the suite permitted to assert `is None` for a
    non-claude provider, and it earns that by using the REAL sandbox built
    with nothing faked."""
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    if not sandbox.ready_to_dispatch:
        pytest.skip(
            "this host cannot fully probe the guard/auth mechanism "
            f"(unavailable: {sandbox.unavailable_guards}, "
            f"authentication_ready={sandbox.authentication_ready}) — covered instead by "
            "test_sandbox_probe_reports_unbound_without_sandbox_exec and "
            "test_refuse_if_guard_required_refuses_codex_when_sandbox_exec_is_unavailable"
        )
    assert hlr.refuse_if_guard_required(sandbox) is None


def test_refuse_if_guard_required_is_driven_by_fully_guarded_not_a_constant(tmp_path):
    """Regression pin for the exact defect the review found: constructs two
    sandboxes that differ ONLY in their probed fully_guarded state and asserts
    refuse_if_guard_required's answer tracks that difference — proving the
    function reads real state rather than always returning the same answer
    regardless of what is passed in.
    """
    guarded = hlr.HarnessSandbox(
        provider="codex", root=tmp_path, env_extra={}, command_wrapper=[],
        credential_read_denied=True, user_config_write_denied=True,
        git_stash_denied=True, authentication_ready=True, unavailable_guards=(),
    )
    unguarded = hlr.HarnessSandbox(
        provider="codex", root=tmp_path, env_extra={}, command_wrapper=[],
        credential_read_denied=False, user_config_write_denied=True,
        git_stash_denied=True, authentication_ready=True,
        unavailable_guards=("credential_read_guard (…)",),
    )
    assert hlr.refuse_if_guard_required(guarded) is None
    assert hlr.refuse_if_guard_required(unguarded) is not None


# ── Dry run: no model call, exact command + prompt size --------------------------


def test_dry_run_makes_no_model_call_and_reports_command(
    monkeypatch, tmp_path, target, brief_file
):
    dispatch_called = False

    async def _boom(*a, **k):
        nonlocal dispatch_called
        dispatch_called = True
        raise AssertionError("dispatch_role.dispatch called during --dry-run")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _boom)

    created = {}

    def _fake_create(self, *, head):
        created["head"] = head
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target, provider="codex", post=False, dry_run=True,
            repo_worktree_name="ateles", scratch_root=tmp_path,
            brief_path=brief_file, timeout=None,
        )
    )

    assert dispatch_called is False
    assert report["dry_run"] is True
    assert report["no_model_call_made"] is True
    assert report["provider"] == "codex"
    assert "codex" in report["example_command"]
    assert "CODEX_HOME" in report["sandbox_env_extra"]
    assert report["prompt_chars"] > 0
    assert created["head"] == SAMPLE_HEAD
    # command_wrapper is prepended to the example command too, so the dry run
    # shows exactly what the real dispatch will run.
    if report["command_wrapper"]:
        assert report["example_command"][: len(report["command_wrapper"])] == (
            report["command_wrapper"]
        )
        assert report["fully_guarded"] is True
    else:
        # This host lacks sandbox-exec (or it failed its own probe) — the
        # dry run must say so rather than silently pretending it's guarded.
        assert report["fully_guarded"] is False


# ── Verdict validation uses the SAME reader Claude runs use -----------------------


def test_validate_verdict_accepts_well_formed_signoff():
    check = hlr.validate_verdict(SIGNED_OFF_VERDICT, lens_agent="pavo")
    assert check.ok is True
    assert check.lens_verdict == "signed_off"
    assert check.sign_off_warranted is True


def test_validate_verdict_rejects_unreadable_verdict():
    check = hlr.validate_verdict(UNREADABLE_VERDICT, lens_agent="pavo")
    assert check.ok is False
    assert check.lens_verdict is None
    assert "not readable" in check.reason


def test_validate_verdict_pre_post_lines_captured():
    check = hlr.validate_verdict(SIGNED_OFF_VERDICT, lens_agent="pavo")
    assert check.pre_post["line3"] == "**SIGNED_OFF**"
    assert check.pre_post["blocking_count"] == 0


def test_validate_verdict_refuses_rather_than_invent_a_check_if_reader_missing(
    monkeypatch,
):
    monkeypatch.setattr(hlr, "swarm_dispatch", None)
    check = hlr.validate_verdict(SIGNED_OFF_VERDICT, lens_agent="pavo")
    assert check.ok is False
    assert "cannot validate" in check.reason


# ── Posting gate: never posts an unreadable verdict, never posts under --compare --


def test_run_one_does_not_post_when_verdict_unreadable(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    async def _dispatch(role, task, **kwargs):
        return SkillResult(role, True, 0, UNREADABLE_VERDICT, "", provider="codex")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _dispatch)

    def _fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)

    posted = False

    def _boom_post(**kwargs):
        nonlocal posted
        posted = True
        raise AssertionError("post_verdict called for an unreadable verdict")

    monkeypatch.setattr(hlr, "post_verdict", _boom_post)

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target, provider="codex", post=True, dry_run=False,
            repo_worktree_name="ateles", scratch_root=tmp_path,
            brief_path=brief_file, timeout=None,
        )
    )

    assert posted is False
    assert report["ok"] is False
    assert report["posted"] is False


def test_run_one_does_not_post_without_post_flag(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    """A validated verdict with --post NOT set must still refuse to post."""

    async def _dispatch(role, task, **kwargs):
        verdict_path = Path(kwargs["cwd"]) / f"{target.lens}{target.pr}_verdict.md"
        verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
        return SkillResult(role, True, 0, SIGNED_OFF_VERDICT, "", provider="codex")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _dispatch)

    def _fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)

    monkeypatch.setattr(hlr, "current_pr_head", lambda **k: SAMPLE_HEAD)

    posted = False

    def _boom_post(**kwargs):
        nonlocal posted
        posted = True

    monkeypatch.setattr(hlr, "post_verdict", _boom_post)

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target, provider="codex", post=False, dry_run=False,
            repo_worktree_name="ateles", scratch_root=tmp_path,
            brief_path=brief_file, timeout=None,
        )
    )

    assert posted is False
    assert report["ok"] is True
    assert report["posted"] is False
    assert "not set" in report["refusal_reason"]


def test_run_one_refuses_to_post_when_head_moved(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    async def _dispatch(role, task, **kwargs):
        verdict_path = Path(kwargs["cwd"]) / f"{target.lens}{target.pr}_verdict.md"
        verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
        return SkillResult(role, True, 0, SIGNED_OFF_VERDICT, "", provider="codex")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _dispatch)

    def _fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)

    monkeypatch.setattr(hlr, "current_pr_head", lambda **k: "b" * 40)  # moved!

    posted = False
    monkeypatch.setattr(hlr, "post_verdict", lambda **k: (_ for _ in ()).throw(
        AssertionError("must not post against a stale head")
    ))

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target, provider="codex", post=True, dry_run=False,
            repo_worktree_name="ateles", scratch_root=tmp_path,
            brief_path=brief_file, timeout=None,
        )
    )

    assert report["ok"] is False
    assert "head moved" in report["refusal_reason"]
    assert posted is False


def test_run_one_refuses_to_post_under_wrong_gh_identity(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    async def _dispatch(role, task, **kwargs):
        verdict_path = Path(kwargs["cwd"]) / f"{target.lens}{target.pr}_verdict.md"
        verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
        return SkillResult(role, True, 0, SIGNED_OFF_VERDICT, "", provider="codex")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _dispatch)

    def _fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)

    monkeypatch.setattr(hlr, "current_pr_head", lambda **k: SAMPLE_HEAD)
    monkeypatch.setattr(hlr, "gh_login", lambda: "markmhendrickson")

    monkeypatch.setattr(hlr, "post_verdict", lambda **k: (_ for _ in ()).throw(
        AssertionError("must not post under the wrong gh identity")
    ))

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target, provider="codex", post=True, dry_run=False,
            repo_worktree_name="ateles", scratch_root=tmp_path,
            brief_path=brief_file, timeout=None,
        )
    )

    assert report["ok"] is False
    assert "ateles-agent" in report["refusal_reason"]


def test_run_one_posts_when_everything_checks_out(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    async def _dispatch(role, task, **kwargs):
        verdict_path = Path(kwargs["cwd"]) / f"{target.lens}{target.pr}_verdict.md"
        verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
        return SkillResult(role, True, 0, SIGNED_OFF_VERDICT, "", provider="codex")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _dispatch)

    def _fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)

    monkeypatch.setattr(hlr, "current_pr_head", lambda **k: SAMPLE_HEAD)
    monkeypatch.setattr(hlr, "gh_login", lambda: "ateles-agent")
    monkeypatch.setattr(
        hlr, "post_verdict", lambda **k: "https://github.com/o/r/pull/1#comment"
    )

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target, provider="codex", post=True, dry_run=False,
            repo_worktree_name="ateles", scratch_root=tmp_path,
            brief_path=brief_file, timeout=None,
        )
    )

    assert report["ok"] is True
    assert report["posted"] is True
    assert report["comment_url"] == "https://github.com/o/r/pull/1#comment"


# ── Compare mode never posts -----------------------------------------------------


def test_compare_mode_never_posts_even_with_post_flag(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    seen_posts = []

    async def _dispatch(role, task, **kwargs):
        verdict_path = Path(kwargs["cwd"]) / f"{target.lens}{target.pr}_verdict.md"
        verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
        return SkillResult(role, True, 0, SIGNED_OFF_VERDICT, "", provider=kwargs.get("provider"))

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _dispatch)

    def _fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)

    monkeypatch.setattr(hlr, "current_pr_head", lambda **k: SAMPLE_HEAD)
    monkeypatch.setattr(hlr, "gh_login", lambda: "ateles-agent")
    monkeypatch.setattr(
        hlr, "post_verdict", lambda **k: seen_posts.append(k) or "url"
    )

    import asyncio

    report = asyncio.run(
        hlr.run_compare(
            target, providers=["claude", "codex"], dry_run=False, post=True,
            repo_worktree_name="ateles", scratch_root=tmp_path,
            brief_path=brief_file, timeout=None,
        )
    )

    assert seen_posts == []
    assert report["compare"] is True
    assert set(report["results"].keys()) == {"claude", "codex"}
    for r in report["results"].values():
        assert r["posted"] is False


# ── CLI-level: --provider and --compare are mutually exclusive -------------------


def test_main_requires_provider_or_compare(brief_file):
    with pytest.raises(SystemExit):
        hlr.main(
            [
                "--repo", "o/r", "--pr", "1", "--head", SAMPLE_HEAD,
                "--lens", "pm", "--agent", "pavo", "--brief", str(brief_file),
            ]
        )


def test_main_rejects_both_provider_and_compare(brief_file):
    with pytest.raises(SystemExit):
        hlr.main(
            [
                "--repo", "o/r", "--pr", "1", "--head", SAMPLE_HEAD,
                "--lens", "pm", "--agent", "pavo", "--brief", str(brief_file),
                "--provider", "codex", "--compare", "claude,codex",
            ]
        )


def test_main_requires_brief_flag():
    with pytest.raises(SystemExit):
        hlr.main(
            [
                "--repo", "o/r", "--pr", "1", "--head", SAMPLE_HEAD,
                "--lens", "pm", "--agent", "pavo", "--provider", "codex",
            ]
        )


# ── --agent resolved from review_panel.LENSES when omitted -----------------------
#
# The coordinator's ask: the runner accepts the derived-panel lens list
# (review_panel.select_panel, the same registry approve_pr_as_app.py's
# derive_required_lenses reads) as its input, one lens per invocation (this
# script's own scope is one lens on one head). Rather than trust a caller to
# type BOTH the lens label and its owning agent consistently, --agent becomes
# optional and is resolved from the same registry select_panel itself reads.


def test_main_resolves_agent_from_lens_when_agent_omitted(
    monkeypatch, tmp_path, brief_file
):
    """--lens pm with no --agent must resolve to pavo (review_panel.LENSES's
    own mapping), not require the caller to also type --agent pavo."""
    seen_agent = {}

    async def _capture_dispatch(role, task, **kwargs):
        seen_agent["role"] = role
        # A controlled failure (not a raise): resolution is what's under
        # test, not run_one's own error handling.
        return SkillResult(role, False, 1, "", "", error="stop before real dispatch")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _capture_dispatch)

    def _fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)

    rc = hlr.main(
        [
            "--repo", "markmhendrickson/ateles", "--pr", "1", "--head", SAMPLE_HEAD,
            "--lens", "pm", "--provider", "codex", "--brief", str(brief_file),
        ]
    )

    assert seen_agent["role"] == "pavo"
    assert rc == 1  # the forced RuntimeError above surfaces as a failure exit


def test_main_refuses_when_agent_omitted_for_an_unknown_lens(brief_file):
    """A lens outside review_panel.LENSES has no agent to resolve — --agent
    stays mandatory for it, with a clear reason rather than a KeyError."""
    with pytest.raises(SystemExit):
        hlr.main(
            [
                "--repo", "o/r", "--pr", "1", "--head", SAMPLE_HEAD,
                "--lens", "not-a-real-lens", "--provider", "codex",
                "--brief", str(brief_file),
            ]
        )


def test_lens_by_name_agent_mapping_matches_registry_for_pm_and_arch():
    """Pin the two mappings this PR's own examples rely on, so a future
    registry change that silently reassigns an agent is caught here rather
    than in a stale docstring/PR-body example."""
    assert hlr.lens_by_name("pm").agent == "pavo"
    assert hlr.lens_by_name("arch").agent == "waxwing"


# ── Worktree lifecycle: guards reach the real dispatch boundary -----------------


def test_run_one_passes_sandbox_env_extra_to_dispatch(
    monkeypatch, tmp_path, target, brief_file
):
    seen = {}

    # This is a plumbing test, not another platform probe. Supply a sandbox
    # whose guards are already proven so the test remains meaningful on Linux,
    # where the separate integration tests correctly show sandbox-exec as
    # unavailable and the real runner refuses before dispatch.
    guarded = hlr.HarnessSandbox(
        provider="codex",
        root=tmp_path / "codex-home",
        env_extra={"CODEX_HOME": str(tmp_path / "codex-home")},
        command_wrapper=["sandbox-exec", "-f", str(tmp_path / "profile.sb")],
        credential_read_denied=True,
        user_config_write_denied=True,
        git_stash_denied=True,
        authentication_ready=True,
        unavailable_guards=(),
    )
    monkeypatch.setattr(
        hlr.HarnessSandbox,
        "build",
        classmethod(lambda cls, provider, tmp_root: guarded),
    )

    async def _dispatch(role, task, **kwargs):
        seen.update(kwargs)
        verdict_path = Path(kwargs["cwd"]) / f"{target.lens}{target.pr}_verdict.md"
        verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
        return SkillResult(role, True, 0, SIGNED_OFF_VERDICT, "", provider="codex")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _dispatch)

    def _fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)

    import asyncio

    asyncio.run(
        hlr.run_one(
            target, provider="codex", post=False, dry_run=False,
            repo_worktree_name="ateles", scratch_root=tmp_path,
            brief_path=brief_file, timeout=None,
        )
    )

    assert "CODEX_HOME" in seen["env_extra"]
    assert seen["command_wrapper"]
    assert seen["command_wrapper"][0] == "sandbox-exec"
    assert seen["seated_reviewer"] is False
    assert seen["provider"] == "codex"


def test_run_one_passes_task_entity_id_to_dispatch_for_neotoma_monitoring(
    monkeypatch, tmp_path, brief_file
):
    """The coordinator's ask: a session must be able to monitor a dispatch
    from Neotoma alone. dispatch_role.dispatch()->run_skill() already writes
    task_entity_id onto every harness_event row it emits (start, completion,
    failure) — this test proves harness_lens_runner actually SUPPLIES one
    when the caller names a task, rather than always leaving it empty.
    """
    target_with_task = hlr.LensTarget(
        repo="markmhendrickson/ateles", pr=1234, head=SAMPLE_HEAD,
        lens="pm", agent="pavo", task_entity_id="ent_898998f41372ce24369fb365",
    )

    guarded = hlr.HarnessSandbox(
        provider="codex", root=tmp_path / "codex-home",
        env_extra={"CODEX_HOME": str(tmp_path / "codex-home")},
        command_wrapper=["sandbox-exec", "-f", str(tmp_path / "profile.sb")],
        credential_read_denied=True, user_config_write_denied=True,
        git_stash_denied=True, authentication_ready=True, unavailable_guards=(),
    )
    monkeypatch.setattr(
        hlr.HarnessSandbox, "build", classmethod(lambda cls, provider, tmp_root: guarded)
    )

    seen = {}

    async def _dispatch(role, task, **kwargs):
        seen.update(kwargs)
        verdict_path = Path(kwargs["cwd"]) / "pm1234_verdict.md"
        verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
        return SkillResult(role, True, 0, SIGNED_OFF_VERDICT, "", provider="codex")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _dispatch)

    def _fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)

    import asyncio

    asyncio.run(
        hlr.run_one(
            target_with_task, provider="codex", post=False, dry_run=False,
            repo_worktree_name="ateles", scratch_root=tmp_path,
            brief_path=brief_file, timeout=None,
        )
    )

    assert seen["task_entity_id"] == "ent_898998f41372ce24369fb365"


def test_lens_target_task_entity_id_defaults_to_empty_string():
    """A one-off comparison run need not name a task — the default must stay
    an empty string (not None), matching dispatch_role.dispatch's own default
    and skill_runner's idempotency-key string formatting."""
    target = hlr.LensTarget(
        repo="o/r", pr=1, head=SAMPLE_HEAD, lens="pm", agent="pavo"
    )
    assert target.task_entity_id == ""
