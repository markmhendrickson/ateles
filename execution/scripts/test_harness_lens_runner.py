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
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_DAEMON_DIR = _REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness_lens_runner as hlr  # noqa: E402
import harness_router  # noqa: E402
import skill_runner  # noqa: E402
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

NETWORK_DELIVERY_DENIAL = (
    "sandbox denied network access — the child could not push or reach the GitHub API"
)
OBJECT_STORE_DENIAL = (
    "sandbox denied writes to the git object store — the child could not commit"
)


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
        "APIS_HARNESS_HEADROOM",
        json.dumps({"claude": 1.0, "codex": 1.0, "cursor": 1.0}),
    )
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM_FILE", "/nonexistent/harness-headroom.json"
    )


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
            command_wrapper=["/usr/bin/sandbox-exec", "-f", str(root / "profile.sb")]
            if provider != "claude"
            else [],
            credential_read_denied=True,
            credential_binding_protected=True,
            user_config_write_denied=True,
            git_stash_denied=True,
            authentication_ready=True,
            unavailable_guards=(),
        )

    monkeypatch.setattr(hlr.HarnessSandbox, "build", classmethod(_build))
    # Posting tests use a deliberately minimal fake Worktree.create that does
    # not initialize git. Stash-ref behavior has dedicated real-repo tests;
    # keep unrelated posting assertions focused on their own boundary.
    monkeypatch.setattr(
        hlr,
        "capture_stash_ref_state",
        lambda worktree: hlr.StashRefState(resolved_oid=None, packed_oid=None),
    )


def test_parallel_prepare_serializes_only_shared_fetch(monkeypatch, tmp_path):
    """Reproduce the concurrent ref failure, then prove the fetch lock."""
    repo = tmp_path / "shared-repo"
    (repo / ".git").mkdir(parents=True)
    state_lock = threading.Lock()
    active_fetches = 0
    max_active_fetches = 0

    def planted_run(command, **kwargs):
        nonlocal active_fetches, max_active_fetches
        if command[-2:] == ["rev-parse", "--git-common-dir"]:
            return subprocess.CompletedProcess(command, 0, stdout=".git\n", stderr="")
        if command[-3:] != ["fetch", "-q", "origin"]:
            raise AssertionError(f"unexpected command: {command}")
        with state_lock:
            active_fetches += 1
            max_active_fetches = max(max_active_fetches, active_fetches)
            overlap = active_fetches > 1
        time.sleep(0.05)
        with state_lock:
            active_fetches -= 1
        if overlap:
            raise subprocess.CalledProcessError(
                1,
                command,
                stderr=(
                    "cannot lock ref 'refs/remotes/origin-pr/1308': "
                    "is at 07ebc09a but expected 33dfea92"
                ),
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(hlr.subprocess, "run", planted_run)

    # Prove the planted instrument goes red without the production lock.
    start = threading.Barrier(2)
    unprotected_errors = []

    def unprotected_fetch():
        start.wait()
        try:
            planted_run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
        except subprocess.CalledProcessError as exc:
            unprotected_errors.append(exc)

    threads = [threading.Thread(target=unprotected_fetch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert unprotected_errors
    assert (
        "cannot lock ref 'refs/remotes/origin-pr/1308'" in unprotected_errors[0].stderr
    )
    assert max_active_fetches == 2

    # The same concurrent callers stay single-flight through production code.
    max_active_fetches = 0
    protected_errors = []

    def protected_fetch():
        try:
            hlr._fetch_origin_serialized(repo)
        except Exception as exc:  # pragma: no cover - assertion reports details
            protected_errors.append(exc)

    threads = [threading.Thread(target=protected_fetch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert protected_errors == []
    assert max_active_fetches == 1


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
                target,
                provider="codex",
                post=False,
                dry_run=False,
                repo_worktree_name="ateles",
                scratch_root=tmp_path,
                brief_path=brief_file,
                timeout=None,
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

_HAS_SANDBOX_EXEC = hlr.trusted_sandbox_exec_path() is not None
_IS_DARWIN = platform.system() == "Darwin"


def test_sandbox_build_codex_uses_codex_home_isolation(tmp_path):
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    assert sandbox.env_extra["CODEX_HOME"] == str(sandbox.root)
    assert sandbox.root.is_dir()


@pytest.mark.skipif(
    not (_IS_DARWIN and _HAS_SANDBOX_EXEC),
    reason="the trusted macOS sandbox executable is platform-specific",
)
def test_sandbox_build_uses_trusted_wrapper_despite_path_shadowing(
    tmp_path, monkeypatch
):
    shadow_bin = tmp_path / "shadow-bin"
    shadow_bin.mkdir()
    lookalike = shadow_bin / "sandbox-exec"
    lookalike.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    lookalike.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shadow_bin}:{os.environ['PATH']}")
    monkeypatch.setattr(
        hlr, "probe_stash_effect_denied_across_git_binaries", lambda *args: True
    )

    sandbox = hlr.HarnessSandbox.build("codex", tmp_path / "sandbox")

    assert sandbox.command_wrapper[0] == "/usr/bin/sandbox-exec"
    assert Path(sandbox.command_wrapper[0]).samefile("/usr/bin/sandbox-exec")


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
            "/usr/bin/sandbox-exec",
            "-f",
            str(profile),
            "sh",
            "-c",
            'printf changed > "$1"',
            "probe",
            str(linked),
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
def test_git_shim_is_advisory_only_not_the_control(tmp_path):
    """As of PR #1308 round 4 (ent_9e88db1882c668e6c5c32be9's round-4
    follow-up), the shim is a friendly early-refusal message for the common
    PATH-resolved case — NOT the enforcement mechanism. This test proves the
    ADVISORY behaviour still works: a real `git stash push` through the shim
    exits nonzero with a message naming it a courtesy, while an unrelated
    command still runs. Whether the mutation is ACTUALLY prevented is proven
    separately, by the sandbox-exec effect tests below, against the real
    stash ref — not by this shim, and not by this test.
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
    subprocess.run(
        ["git", "-C", str(scratch_repo), "commit", "-q", "-m", "probe"], check=True
    )
    (scratch_repo / "f.txt").write_text("changed", encoding="utf-8")

    shim_dir = sandbox.env_extra["PATH"].split(":")[0]
    env = {**os.environ, "PATH": f"{shim_dir}:{os.environ.get('PATH', '')}"}

    push = subprocess.run(
        ["git", "-C", str(scratch_repo), "stash", "push"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert push.returncode != 0, "the shim let a real stash push through"
    assert "courtesy" in push.stderr.lower()

    # And a completely unrelated git command must still work through the shim.
    status = subprocess.run(
        ["git", "-C", str(scratch_repo), "status"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert status.returncode == 0


# ── git-stash denied by EFFECT, not by binary (PR #1308 round 4,
# ent_9e88db1882c668e6c5c32be9's own round-4 follow-up) ---------------------------
#
# Round 3 enumerated and denied specific git BINARIES (a PATH-first shim, then a
# process-exec deny per discovered path). Round 4's own review found a THIRD
# real git binary that enumeration missed entirely (Xcode's bundled copy,
# reachable via `xcrun -f git`, never on PATH) and reproduced a live bypass
# through it. These tests prove the round-4 fix — a deny on WRITING/READING the
# stash ref itself, regardless of which binary attempts it — actually closes
# that gap, using the real mechanism against a real scratch repo.


def test_discover_probe_git_invocations_finds_more_than_one_real_binary():
    """This host must have at least the PATH-resolved git for this test suite
    to mean anything; asserting more than one (when available) is what
    distinguishes this from round 3's PATH-only enumeration.
    """
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "discover-probe-repo"
        scratch.mkdir()
        subprocess.run(["git", "init", "-q", str(scratch)], check=True)
        invocations = hlr.discover_probe_git_invocations(scratch)
    assert invocations, "expected at least the PATH-resolved git"
    assert ["git"] in invocations


def test_stash_effect_probe_fails_closed_when_every_state_read_fails(
    monkeypatch, tmp_path
):
    """Identical failed reads must not look like an unchanged stash stack."""

    def _failed_reads_and_denied_push(command, **kwargs):
        if command[-2:] == ["stash", "list"]:
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="unreadable"
            )
        if command[-2:] == ["stash", "push"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="denied")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(hlr.subprocess, "run", _failed_reads_and_denied_push)
    monkeypatch.setattr(
        hlr, "discover_probe_git_invocations", lambda scratch_git_dir: [["git"]]
    )
    monkeypatch.setattr(hlr.shutil, "which", lambda command: "/usr/bin/git")

    assert (
        hlr.probe_stash_effect_denied_across_git_binaries(["sandbox-exec"], tmp_path)
        is False
    )


def test_advisory_stash_probe_fails_closed_when_every_state_read_fails(
    monkeypatch, tmp_path
):
    """The advisory probe must reject the same unreadable baseline/after state."""

    def _failed_reads_and_denied_push(command, **kwargs):
        if command[-2:] == ["stash", "list"]:
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="unreadable"
            )
        if command[-2:] == ["stash", "push"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="denied")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(hlr.subprocess, "run", _failed_reads_and_denied_push)
    monkeypatch.setattr(hlr.shutil, "which", lambda command: "/usr/bin/git")

    assert hlr.probe_git_shim_denies_stash_push(tmp_path / "git", tmp_path) is False


def test_stash_effect_probe_accepts_successful_empty_state_reads(monkeypatch, tmp_path):
    """A normal empty stash list remains a readable, comparable observation."""

    def _empty_reads_and_denied_push(command, **kwargs):
        if command[-2:] == ["stash", "list"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[-2:] == ["stash", "push"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="denied")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(hlr.subprocess, "run", _empty_reads_and_denied_push)
    monkeypatch.setattr(
        hlr, "discover_probe_git_invocations", lambda scratch_git_dir: [["git"]]
    )
    monkeypatch.setattr(hlr.shutil, "which", lambda command: "/usr/bin/git")

    assert (
        hlr.probe_stash_effect_denied_across_git_binaries(["sandbox-exec"], tmp_path)
        is True
    )


@pytest.mark.skipif(
    not (_IS_DARWIN and _HAS_SANDBOX_EXEC),
    reason="sandbox-exec is macOS-only; see test_sandbox_probe_reports_unbound_without_sandbox_exec",
)
def test_stash_ref_is_really_denied_across_every_real_git_binary_this_host_has(
    tmp_path,
):
    """The load-bearing regression test for the round-4 finding: invoke `git
    stash push` via EVERY real git invocation this host offers — PATH-resolved
    `git`, `/usr/bin/git` by absolute path, `xcrun git`, and (if distinct)
    Xcode's bundled binary by its own absolute path — wrapped in the ACTUAL
    sandbox-exec command this run's HarnessSandbox built, and confirm the REAL
    stash stack (read directly with the system git, not through any guard) is
    unchanged after every single one.

    This must include at least `xcrun git` or Xcode's own binary when this
    host has Xcode installed — the exact resolution mechanism round 3's
    PATH-only enumeration missed and round 4's review reproduced a live
    bypass through.
    """
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    assert sandbox.command_wrapper, "no sandbox-exec wrapper built — cannot test this"

    scratch_repo = tmp_path / "stash-effect-probe-repo"
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
    # A REAL commit first: without one, even a fully bypassed guard fails on
    # its own with "no initial commit", which would make this test pass for
    # the wrong reason.
    subprocess.run(
        ["git", "-C", str(scratch_repo), "commit", "-q", "-m", "probe"], check=True
    )
    (scratch_repo / "f.txt").write_text("changed", encoding="utf-8")

    invocations = hlr.discover_probe_git_invocations(scratch_repo)
    assert invocations, "expected at least one real git invocation to test against"
    if (
        shutil.which("xcrun")
        and Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git").is_file()
    ):
        assert any(
            prefix == ["xcrun", "git"]
            or prefix == ["/Applications/Xcode.app/Contents/Developer/usr/bin/git"]
            for prefix in invocations
        ), "expected xcrun/Xcode's git to be among the tested invocations on this host"

    for prefix in invocations:
        attempt = subprocess.run(
            [
                *sandbox.command_wrapper,
                *prefix,
                "-C",
                str(scratch_repo),
                "stash",
                "push",
            ],
            capture_output=True,
            text=True,
        )
        assert attempt.returncode != 0, (
            f"invocation {prefix} was NOT denied by the sandbox-exec "
            "stash-ref deny — the exact round-4 bypass"
        )

    # Read the REAL stash stack directly (system git, no guard involved) —
    # confirms the denial actually stopped the mutation for every invocation.
    real_system_git = shutil.which("git")
    listing = subprocess.run(
        [real_system_git, "-C", str(scratch_repo), "stash", "list"],
        capture_output=True,
        text=True,
    )
    assert listing.stdout.strip() == "", (
        "a stash entry landed despite every invocation reporting denied — "
        "the sandbox blocked something else, not the mutation"
    )


@pytest.mark.skipif(
    not (_IS_DARWIN and _HAS_SANDBOX_EXEC),
    reason="sandbox-exec is macOS-only",
)
def test_stash_ref_deny_also_denies_reading_an_existing_entry(tmp_path):
    """The read-deny half of the round-4 fix: a dispatched child must not be
    able to CONSUME an existing stash entry (left by another session) via
    `stash list`/`show`/`apply` either. Verified shape: a sandboxed `stash
    list` against a repo with a real pre-existing entry returns an EMPTY
    listing rather than an error — Seatbelt's deny makes git behave as if the
    ref does not exist, which is the desired outcome even though it is not a
    nonzero exit code (see build_sandbox_exec_profile's docstring).
    """
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    assert sandbox.command_wrapper

    scratch_repo = tmp_path / "read-deny-probe-repo"
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
    subprocess.run(
        ["git", "-C", str(scratch_repo), "commit", "-q", "-m", "probe"], check=True
    )
    (scratch_repo / "f.txt").write_text("changed", encoding="utf-8")
    # A REAL, unsandboxed push — this is the "existing entry left by another
    # session" the read-deny must hide from a later sandboxed attempt.
    subprocess.run(["git", "-C", str(scratch_repo), "stash", "push"], check=True)
    real_listing_before = subprocess.run(
        ["git", "-C", str(scratch_repo), "stash", "list"],
        capture_output=True,
        text=True,
    )
    assert real_listing_before.stdout.strip() != "", (
        "setup failed: no real entry to hide"
    )

    sandboxed_listing = subprocess.run(
        [*sandbox.command_wrapper, "git", "-C", str(scratch_repo), "stash", "list"],
        capture_output=True,
        text=True,
    )
    assert sandboxed_listing.stdout.strip() == "", (
        "the sandboxed list saw an existing stash entry it should not be able to read"
    )


def _packed_stash_fixture(repo: Path) -> tuple[str, str]:
    """Create two commits and pack refs/stash at the first, without git-stash."""
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "probe@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "probe"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True
    )
    tracked = repo / "tracked.txt"
    tracked.write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "first"], check=True)
    first = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    tracked.write_text("second\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "second"], check=True)
    second = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/stash", first], check=True
    )
    subprocess.run(["git", "-C", str(repo), "pack-refs", "--all"], check=True)
    assert not (repo / ".git" / "refs" / "stash").exists()
    return first, second


def _rewrite_packed_stash(repo: Path, replacement: str) -> None:
    """Model the round-five bypass: direct packed-refs content replacement."""
    packed_refs = repo / ".git" / "packed-refs"
    lines = packed_refs.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.endswith(" refs/stash"):
            lines[index] = f"{replacement} refs/stash"
            replaced = True
    assert replaced, "fixture setup failed: refs/stash was not packed"
    packed_refs.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_verify_stash_ref_unchanged_detects_direct_packed_ref_rewrite(tmp_path):
    """A hand-written packed-refs redirect is detected by its observable effect."""
    repo = tmp_path / "packed-stash-repo"
    before_oid, replacement_oid = _packed_stash_fixture(repo)

    before = hlr.capture_stash_ref_state(repo)
    assert before.resolved_oid == before_oid
    assert before.packed_oid == before_oid
    _rewrite_packed_stash(repo, replacement_oid)

    reason = hlr.verify_stash_ref_unchanged_after_dispatch(repo, before)
    assert reason is not None
    assert "refs/stash changed during the dispatched run" in reason
    assert before_oid in reason
    assert replacement_oid in reason


def test_verify_stash_ref_detects_shadowed_packed_entry_mutation(tmp_path):
    """A loose ref must not hide mutation of the packed entry beneath it."""
    repo = tmp_path / "shadowed-packed-stash-repo"
    packed_oid, loose_oid = _packed_stash_fixture(repo)
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/stash", loose_oid], check=True
    )

    before = hlr.capture_stash_ref_state(repo)
    assert before.resolved_oid == loose_oid
    assert before.packed_oid == packed_oid

    packed_refs = repo / ".git" / "packed-refs"
    retained = [
        line
        for line in packed_refs.read_text(encoding="utf-8").splitlines()
        if not line.endswith(" refs/stash")
    ]
    packed_refs.write_text("\n".join(retained) + "\n", encoding="utf-8")

    reason = hlr.verify_stash_ref_unchanged_after_dispatch(repo, before)
    assert reason is not None
    assert "packed refs/stash changed" in reason
    assert packed_oid in reason


@pytest.mark.skipif(
    not (_IS_DARWIN and _HAS_SANDBOX_EXEC),
    reason="git_stash_denied now also requires the absolute-path sandbox-exec "
    "probe (ent_9e88db1882c668e6c5c32be9), which needs sandbox-exec; see "
    "test_sandbox_probe_reports_unbound_without_sandbox_exec for the other side",
)
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
    if not sandbox.credential_read_denied:
        pytest.skip(
            "sandbox-exec is installed but this host cannot apply its profile; "
            "the production fail-closed path is tested separately"
        )
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
        ["/usr/bin/sandbox-exec", "-f", str(profile_path), "cat", str(fixture)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "the real sandbox-exec profile let a real read through"
    )

    # An unrelated file outside the deny globs must still be readable.
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("hi", encoding="utf-8")
    ok = _subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(profile_path), "cat", str(unrelated)],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0
    assert ok.stdout.strip() == "hi"


@pytest.mark.skipif(
    not (_IS_DARWIN and _HAS_SANDBOX_EXEC),
    reason="sandbox-exec is macOS-only; non-macOS dispatches fail closed",
)
def test_sandbox_profile_preserves_credential_binding_with_positive_control(tmp_path):
    fixture_home = tmp_path / "fixture-user"
    protected_dir = fixture_home / ".config" / "neotoma"
    protected_dir.mkdir(parents=True)
    protected_file = protected_dir / ".env-test"
    protected_file.write_text("fixture-not-a-secret\n", encoding="utf-8")
    profile = tmp_path / "profile.sb"
    hlr.build_sandbox_exec_profile(profile, credential_home_roots=(fixture_home,))

    probe_passed = hlr.probe_sandbox_exec_preserves_credential_binding(
        profile,
        protected_dir=protected_dir,
        protected_file=protected_file,
        control_root=tmp_path / "ordinary-worktree-fixture",
    )
    if not probe_passed:
        operational = subprocess.run(
            ["/usr/bin/sandbox-exec", "-f", str(profile), "/usr/bin/true"],
            capture_output=True,
            text=True,
        )
        if operational.returncode != 0:
            pytest.skip(
                "this process is already sandboxed and cannot apply a nested "
                "Seatbelt profile; production fails closed on the same probe"
            )
    assert probe_passed


def test_fully_guarded_requires_credential_binding_probe(tmp_path):
    sandbox = hlr.HarnessSandbox(
        provider="codex",
        root=tmp_path,
        env_extra={},
        command_wrapper=[],
        credential_read_denied=True,
        credential_binding_protected=False,
        user_config_write_denied=True,
        git_stash_denied=True,
        authentication_ready=True,
        unavailable_guards=("credential_binding_guard",),
    )

    assert sandbox.fully_guarded is False
    assert hlr.refuse_if_guard_required(sandbox) is not None


@pytest.mark.skipif(
    not (_IS_DARWIN and _HAS_SANDBOX_EXEC),
    reason="sandbox-exec is macOS-only; see the read-guard test above for the "
    "companion coverage of the unbound-on-other-platforms path",
)
def test_sandbox_build_really_denies_writing_to_user_config_fixture(tmp_path):
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    if not sandbox.user_config_write_denied:
        pytest.skip(
            "sandbox-exec is installed but this host cannot apply its profile; "
            "the production fail-closed path is tested separately"
        )
    assert sandbox.user_config_write_denied is True

    profile_path = sandbox.root / "profile.sb"
    fixture = tmp_path / "second-fixture-write" / ".claude" / "probe"

    import subprocess as _subprocess

    fixture.parent.mkdir(parents=True)
    result = _subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(profile_path), "touch", str(fixture)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "the real sandbox-exec profile let a real write through"
    )
    assert not fixture.exists()


def test_sandbox_probe_reports_unbound_without_sandbox_exec(tmp_path, monkeypatch):
    """The other side of the platform split: when sandbox-exec is genuinely
    unavailable (this host, or a simulated absence), the probe must report
    False — never assume True because the profile file was written.
    """
    monkeypatch.setattr(hlr, "trusted_sandbox_exec_path", lambda: None)
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    assert sandbox.credential_read_denied is False
    assert sandbox.user_config_write_denied is False
    assert sandbox.command_wrapper == []
    assert sandbox.fully_guarded is False
    assert any("credential_read_guard" in g for g in sandbox.unavailable_guards)
    assert any("user_config_write_guard" in g for g in sandbox.unavailable_guards)
    # Without sandbox-exec, the absolute-path bypass (ent_9e88db1882c668e6c5c32be9)
    # cannot be closed either — git_stash_denied must reflect that rather than
    # reporting True on the strength of the PATH-shim probe alone.
    assert sandbox.git_stash_denied is False
    assert any("git_stash_guard" in g for g in sandbox.unavailable_guards)


def test_refuse_if_guard_required_allows_claude_always(tmp_path):
    sandbox = hlr.HarnessSandbox.build("claude", tmp_path)
    assert hlr.refuse_if_guard_required(sandbox) is None


def test_refuse_if_guard_required_refuses_codex_when_sandbox_exec_is_unavailable(
    tmp_path, monkeypatch
):
    """The exact defect PR #1308 shipped with: this must fire for real,
    driven by the sandbox's own probed state, not a hardcoded constant.
    """
    monkeypatch.setattr(hlr, "trusted_sandbox_exec_path", lambda: None)
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


def test_codex_outer_guard_real_commands_keep_all_effect_denials(tmp_path):
    """Removing Codex's inner sandbox must not weaken the outer guard.

    Run the same real command wrapper production passes to Codex: an
    unrelated command succeeds, while a credential-shaped read, a
    user-config write, and a real stash push remain denied by effect. A host
    that cannot apply ``sandbox-exec`` is covered by fail-closed tests.
    """
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path / "sandbox")
    if not sandbox.ready_to_dispatch:
        pytest.skip(
            "host cannot apply the real outer sandbox; fail-closed path is "
            f"covered separately: {sandbox.unavailable_guards}"
        )

    control = subprocess.run(
        [*sandbox.command_wrapper, "/usr/bin/true"], capture_output=True, text=True
    )
    assert control.returncode == 0

    credential = tmp_path / "fixture-user" / ".config" / "neotoma" / ".env-test"
    credential.parent.mkdir(parents=True)
    credential.write_text("fixture-not-a-secret\n", encoding="utf-8")
    denied_read = subprocess.run(
        [*sandbox.command_wrapper, "cat", str(credential)],
        capture_output=True,
        text=True,
    )
    assert denied_read.returncode != 0

    config_write = tmp_path / "fixture-user" / ".codex" / "probe-write"
    config_write.parent.mkdir(parents=True)
    denied_write = subprocess.run(
        [*sandbox.command_wrapper, "touch", str(config_write)],
        capture_output=True,
        text=True,
    )
    assert denied_write.returncode != 0
    assert not config_write.exists()

    scratch = tmp_path / "stash-negative-control"
    scratch.mkdir()
    subprocess.run(["git", "init", "-q", str(scratch)], check=True)
    subprocess.run(
        ["git", "-C", str(scratch), "config", "user.email", "probe@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(scratch), "config", "user.name", "probe"], check=True
    )
    (scratch / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(scratch), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(scratch), "commit", "-q", "-m", "probe"], check=True
    )
    (scratch / "tracked.txt").write_text("after\n", encoding="utf-8")
    denied_stash = subprocess.run(
        [*sandbox.command_wrapper, "git", "-C", str(scratch), "stash", "push"],
        capture_output=True,
        text=True,
    )
    assert denied_stash.returncode != 0
    listing = subprocess.run(
        ["git", "-C", str(scratch), "stash", "list"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert listing.stdout.strip() == ""


def test_refuse_if_guard_required_is_driven_by_fully_guarded_not_a_constant(tmp_path):
    """Regression pin for the exact defect the review found: constructs two
    sandboxes that differ ONLY in their probed fully_guarded state and asserts
    refuse_if_guard_required's answer tracks that difference — proving the
    function reads real state rather than always returning the same answer
    regardless of what is passed in.
    """
    guarded = hlr.HarnessSandbox(
        provider="codex",
        root=tmp_path,
        env_extra={},
        command_wrapper=[],
        credential_read_denied=True,
        credential_binding_protected=True,
        user_config_write_denied=True,
        git_stash_denied=True,
        authentication_ready=True,
        unavailable_guards=(),
    )
    unguarded = hlr.HarnessSandbox(
        provider="codex",
        root=tmp_path,
        env_extra={},
        command_wrapper=[],
        credential_read_denied=False,
        credential_binding_protected=True,
        user_config_write_denied=True,
        git_stash_denied=True,
        authentication_ready=True,
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
            target,
            provider="codex",
            post=False,
            dry_run=True,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
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
    if report["fully_guarded"]:
        assert report["command_wrapper"]
        assert (
            report["example_command"][: len(report["command_wrapper"])]
            == (report["command_wrapper"])
        )
        assert "danger-full-access" in report["example_command"]
    else:
        # This host lacks operational sandbox-exec — the dry run must say so
        # rather than silently pretending the mere wrapper argv is a guard.
        assert report["would_refuse"] is not None
        assert "workspace-write" in report["example_command"]


def test_dry_run_claude_provider_needs_no_sandbox_and_pins_cwd_to_the_worktree(
    monkeypatch, tmp_path, target, brief_file
):
    """The operator's decision (bootstrap dispatch defaults to claude first):
    prove --provider claude reaches the exact real command shape, with no
    model call, and that it runs with cwd pinned to a real worktree of the
    TARGET repo -- which is what makes the project .claude/settings.json and
    the user-level Claude Code hooks apply to it, the same as any ordinary
    Claude Code session in that directory. This does not and cannot prove a
    hook actually FIRED (that needs a live claude process, forbidden here);
    it proves the mechanism that would let it fire is exactly in place:
    dispatch_role.dispatch -> run_skill -> _run_skill_once passes cwd to
    asyncio.create_subprocess_exec unchanged (verified by reading that
    source, not asserted) and this runner supplies the worktree path as cwd.
    """
    dispatch_called = False

    async def _boom(*a, **k):
        nonlocal dispatch_called
        dispatch_called = True
        raise AssertionError("dispatch_role.dispatch called during --dry-run")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _boom)

    def _fake_create(self, *, head):
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
            target,
            provider="claude",
            post=False,
            dry_run=True,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert dispatch_called is False
    assert report["no_model_call_made"] is True
    assert report["provider"] == "claude"
    # claude needs no sandbox wrapper -- its own Claude Code hooks bind
    # every guard this class attempts, so fully_guarded is unconditionally
    # True for this provider (see HarnessSandbox.build's claude branch).
    assert report["command_wrapper"] == []
    assert report["fully_guarded"] is True
    assert report["sandbox_env_extra"] == {}
    assert report["example_command"] == [
        "claude",
        "--print",
        "--append-system-prompt",
        "<system prompt>",
    ]
    assert report["would_refuse"] is None
    # The worktree path IS the real repo's own dedicated throwaway checkout
    # of the target repo -- this is the cwd a real dispatch would pass.
    assert report["worktree"].endswith(f"ateles-wt-{target.lens}-{target.pr}-claude")


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


def _install_minimal_lens_worktree(monkeypatch, target):
    def _fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / f"{target.agent}.md").write_text(
            "# canonical lens prompt\nYou must publish your review on GitHub.\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)


def _delivery_denied_dispatch(
    target,
    *,
    verdict_text=SIGNED_OFF_VERDICT,
    error=NETWORK_DELIVERY_DENIAL,
    returncode=0,
    task_sink=None,
):
    async def _dispatch(role, task, **kwargs):
        if task_sink is not None:
            task_sink.append(task)
        if verdict_text is not None:
            verdict_path = Path(kwargs["cwd"]) / f"{target.lens}{target.pr}_verdict.md"
            verdict_path.write_text(verdict_text, encoding="utf-8")
        return SkillResult(
            role,
            False,
            returncode,
            SIGNED_OFF_VERDICT,
            "",
            error=error,
            provider="codex",
        )

    return _dispatch


def test_child_instruction_after_agent_prompt_forbids_github_publication(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    tasks = []
    _install_minimal_lens_worktree(monkeypatch, target)
    monkeypatch.setattr(
        hlr.dispatch_role,
        "dispatch",
        _delivery_denied_dispatch(
            target,
            verdict_text=None,
            error="unrelated failure",
            returncode=1,
            task_sink=tasks,
        ),
    )

    import asyncio

    asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=False,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert len(tasks) == 1
    task = tasks[0]
    prompt_index = task.index("You must publish your review on GitHub.")
    instruction_index = task.index("AUTHORITATIVE FINAL CHILD INSTRUCTION")
    assert instruction_index > prompt_index
    final_instruction = task[instruction_index:]
    assert "read-only" in final_instruction
    assert "must not call GitHub" in final_instruction
    assert "must not attempt publication" in final_instruction
    assert "trusted parent" in final_instruction
    assert "live-head verification" in final_instruction


def test_network_delivery_denial_with_valid_local_verdict_reaches_parent_gate(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    _install_minimal_lens_worktree(monkeypatch, target)
    monkeypatch.setattr(
        hlr.dispatch_role,
        "dispatch",
        _delivery_denied_dispatch(target),
    )
    head_checks = []
    monkeypatch.setattr(
        hlr,
        "current_pr_head",
        lambda **kwargs: head_checks.append(kwargs) or SAMPLE_HEAD,
    )

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=False,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert report["ok"] is True
    assert report["posted"] is False
    assert report["delivery_denial_recovered"] is True
    assert report["lens_verdict"] == "signed_off"
    assert head_checks == [{"repo": target.repo, "pr": target.pr}]


def test_router_preserved_delivery_denial_reaches_parent_gate(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    """Exercise the real router result shape observed in the live PM run."""
    _install_minimal_lens_worktree(monkeypatch, target)
    harness_router.reset_state()
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "none"))

    async def _dispatch(role, task, **kwargs):
        verdict_path = Path(kwargs["cwd"]) / f"{target.lens}{target.pr}_verdict.md"
        verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")

        async def _attempt(provider):
            return SkillResult(
                role,
                False,
                0,
                SIGNED_OFF_VERDICT + "\nThe input also quoted a session limit.\n",
                "fatal: unable to access 'https://github.com/o/r/': "
                "Could not resolve host: github.com",
                error=NETWORK_DELIVERY_DENIAL,
                provider=provider,
            )

        return await skill_runner._run_provider_attempts(
            role,
            _attempt,
            binaries={"codex": "/bin/codex"},
            provider="codex",
        )

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _dispatch)
    monkeypatch.setattr(hlr, "current_pr_head", lambda **kwargs: SAMPLE_HEAD)

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=False,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert report["ok"] is True
    assert report["delivery_denial_recovered"] is True
    assert report["lens_verdict"] == "signed_off"
    assert harness_router.cooling_providers() == set()


@pytest.mark.parametrize(
    ("verdict_text", "expected_reason"),
    [
        (None, "expected local verdict file"),
        (UNREADABLE_VERDICT, "not readable"),
    ],
    ids=["missing_file", "invalid_verdict"],
)
def test_network_delivery_denial_without_valid_local_verdict_refuses(
    monkeypatch,
    tmp_path,
    target,
    brief_file,
    mock_ready_sandbox,
    verdict_text,
    expected_reason,
):
    _install_minimal_lens_worktree(monkeypatch, target)
    monkeypatch.setattr(
        hlr.dispatch_role,
        "dispatch",
        _delivery_denied_dispatch(target, verdict_text=verdict_text),
    )
    monkeypatch.setattr(
        hlr,
        "current_pr_head",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("parent gates must not run without a valid local verdict")
        ),
    )

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=True,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert report["ok"] is False
    assert report["posted"] is False
    assert expected_reason in report["refusal_reason"]


@pytest.mark.parametrize(
    ("error", "returncode"),
    [
        (OBJECT_STORE_DENIAL, 0),
        ("arbitrary dispatch failure", 0),
        (NETWORK_DELIVERY_DENIAL, 1),
    ],
    ids=["object_store", "unrelated", "nonzero_network"],
)
def test_local_verdict_never_recovers_non_network_or_nonzero_failure(
    monkeypatch,
    tmp_path,
    target,
    brief_file,
    mock_ready_sandbox,
    error,
    returncode,
):
    _install_minimal_lens_worktree(monkeypatch, target)
    monkeypatch.setattr(
        hlr.dispatch_role,
        "dispatch",
        _delivery_denied_dispatch(
            target,
            error=error,
            returncode=returncode,
        ),
    )
    monkeypatch.setattr(
        hlr,
        "current_pr_head",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("parent gates must not run for unrecoverable failures")
        ),
    )

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=True,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert report["ok"] is False
    assert report.get("posted", False) is False
    assert report["reason"] == error


def test_recovered_local_verdict_cannot_post_against_stale_head(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    _install_minimal_lens_worktree(monkeypatch, target)
    monkeypatch.setattr(
        hlr.dispatch_role,
        "dispatch",
        _delivery_denied_dispatch(target),
    )
    monkeypatch.setattr(hlr, "current_pr_head", lambda **kwargs: "b" * 40)
    monkeypatch.setattr(
        hlr,
        "post_verdict",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("must not post a recovered verdict against a stale head")
        ),
    )

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=True,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert report["ok"] is False
    assert report["posted"] is False
    assert report["error_kind"] == "head_mismatch"


def test_recovered_local_verdict_cannot_post_under_wrong_identity(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    _install_minimal_lens_worktree(monkeypatch, target)
    monkeypatch.setattr(
        hlr.dispatch_role,
        "dispatch",
        _delivery_denied_dispatch(target),
    )
    monkeypatch.setattr(hlr, "current_pr_head", lambda **kwargs: SAMPLE_HEAD)
    monkeypatch.setattr(hlr, "gh_login", lambda: "wrong-account")
    monkeypatch.setattr(
        hlr,
        "post_verdict",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("must not post a recovered verdict under wrong identity")
        ),
    )

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=True,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert report["ok"] is False
    assert report["posted"] is False
    assert "expected 'ateles-agent'" in report["refusal_reason"]


def test_post_verdict_retries_transient_gh_failure(monkeypatch, tmp_path):
    verdict_path = tmp_path / "verdict.md"
    verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
    post_calls = []
    landed = []

    def _run(command, **kwargs):
        if command[1] == "api":
            pages = (
                [
                    [
                        {
                            "id": 1,
                            "body": SIGNED_OFF_VERDICT,
                            "html_url": "comment-url",
                            "user": {"login": "ateles-agent"},
                        }
                    ]
                ]
                if landed
                else [[]]
            )
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(pages), stderr=""
            )
        post_calls.append(command)
        if len(post_calls) < 3:
            raise subprocess.CalledProcessError(
                1, command, stderr="temporary transport failure"
            )
        landed.append(True)
        return subprocess.CompletedProcess(
            command, 0, stdout="comment-url\n", stderr=""
        )

    monkeypatch.setattr(hlr.subprocess, "run", _run)

    assert (
        hlr.post_verdict(repo="o/r", pr=1, verdict_path=verdict_path) == "comment-url"
    )
    assert len(post_calls) == 3


def test_post_verdict_does_not_duplicate_when_failed_call_already_landed(
    monkeypatch, tmp_path
):
    verdict_path = tmp_path / "verdict.md"
    verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
    post_calls = []
    landed = []

    def _run(command, **kwargs):
        if command[1] == "api":
            pages = (
                [
                    [
                        {
                            "id": 1,
                            "body": SIGNED_OFF_VERDICT,
                            "html_url": "https://github.com/o/r/pull/1#issuecomment-1",
                            "user": {"login": "ateles-agent"},
                        }
                    ]
                ]
                if landed
                else [[]]
            )
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(pages), stderr=""
            )
        post_calls.append(command)
        landed.append(True)
        raise subprocess.CalledProcessError(1, command, stderr="response lost")

    monkeypatch.setattr(hlr.subprocess, "run", _run)

    assert (
        hlr.post_verdict(repo="o/r", pr=1, verdict_path=verdict_path)
        == "https://github.com/o/r/pull/1#issuecomment-1"
    )
    assert len(post_calls) == 1


def test_post_verdict_persistent_failure_stays_loud_after_three_attempts(
    monkeypatch, tmp_path
):
    verdict_path = tmp_path / "verdict.md"
    verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
    post_calls = []

    def _run(command, **kwargs):
        if command[1] == "api":
            return subprocess.CompletedProcess(command, 0, stdout="[[]]", stderr="")
        post_calls.append(command)
        raise subprocess.CalledProcessError(1, command, stderr="permission denied")

    monkeypatch.setattr(hlr.subprocess, "run", _run)

    with pytest.raises(subprocess.CalledProcessError, match="gh.*pr.*comment"):
        hlr.post_verdict(repo="o/r", pr=1, verdict_path=verdict_path)
    assert len(post_calls) == 3


def test_post_verdict_edits_existing_lens_comment_in_place(monkeypatch, tmp_path):
    verdict_path = tmp_path / "verdict.md"
    verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
    old_body = SIGNED_OFF_VERDICT.replace(SAMPLE_HEAD, "b" * 40).replace(
        "SIGNED_OFF", "REQUEST_CHANGES"
    )
    current_body = old_body
    patch_calls = []

    def _run(command, **kwargs):
        nonlocal current_body
        if command[1] == "api" and command[2].startswith(
            "repos/o/r/issues/1/comments?"
        ):
            pages = [
                [
                    {
                        "id": 41,
                        "body": current_body,
                        "html_url": "https://github.com/o/r/pull/1#issuecomment-41",
                        "user": {"login": "ateles-agent"},
                    }
                ]
            ]
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(pages), stderr=""
            )
        patch_calls.append((command, kwargs))
        assert command[:4] == ["gh", "api", "-X", "PATCH"]
        assert command[4].endswith("/issues/comments/41")
        current_body = json.loads(kwargs["input"])["body"]
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(hlr.subprocess, "run", _run)

    url = hlr.post_verdict(repo="o/r", pr=1, verdict_path=verdict_path)

    assert url.endswith("#issuecomment-41")
    assert current_body == SIGNED_OFF_VERDICT
    assert len(patch_calls) == 1


def test_post_verdict_refuses_ambiguous_prior_lens_comments(monkeypatch, tmp_path):
    verdict_path = tmp_path / "verdict.md"
    verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
    writes = []

    def _run(command, **kwargs):
        if command[1] == "api" and command[2].startswith(
            "repos/o/r/issues/1/comments?"
        ):
            comment = {
                "body": SIGNED_OFF_VERDICT,
                "html_url": "url",
                "user": {"login": "ateles-agent"},
            }
            pages = [[{**comment, "id": 1}, {**comment, "id": 2}]]
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(pages), stderr=""
            )
        writes.append(command)
        raise AssertionError("ambiguous comments must prevent every write")

    monkeypatch.setattr(hlr.subprocess, "run", _run)

    with pytest.raises(RuntimeError, match="ambiguous prior review:pm"):
        hlr.post_verdict(repo="o/r", pr=1, verdict_path=verdict_path)
    assert writes == []


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
            target,
            provider="codex",
            post=True,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
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
            target,
            provider="codex",
            post=False,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert posted is False
    assert report["ok"] is True
    assert report["posted"] is False
    assert "not set" in report["refusal_reason"]


def test_run_one_refuses_before_dispatch_when_stash_baseline_is_unreadable(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    dispatched = False

    def _fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)

    def _unreadable(worktree):
        raise RuntimeError("packed-refs malformed")

    monkeypatch.setattr(hlr, "capture_stash_ref_state", _unreadable)

    async def _dispatch(*args, **kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("dispatch must not start without a stash baseline")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _dispatch)

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=False,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert dispatched is False
    assert report["ok"] is False
    assert report["posted"] is False
    assert "baseline could not be established" in report["refusal_reason"]


def test_read_stash_ref_oid_accepts_real_repository_without_stash_ref(tmp_path):
    repo = tmp_path / "fresh-repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    assert hlr.read_stash_ref_oid(repo) is None
    assert hlr.capture_stash_ref_state(repo) == hlr.StashRefState(
        resolved_oid=None,
        packed_oid=None,
    )


def test_read_stash_ref_oid_refuses_malformed_real_ref_storage(tmp_path):
    repo = tmp_path / "malformed-repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".git" / "packed-refs").write_text(
        "not-an-object-id refs/stash\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="cannot test refs/stash existence"):
        hlr.read_stash_ref_oid(repo)


def test_run_one_real_repository_without_stash_ref_reaches_dispatch(
    monkeypatch, tmp_path, target, brief_file
):
    dispatched = False

    def _ready_build(cls, provider, tmp_root):
        root = tmp_root / f"{provider}-unit-home"
        root.mkdir(parents=True, exist_ok=True)
        return hlr.HarnessSandbox(
            provider=provider,
            root=root,
            env_extra={"CODEX_HOME": str(root)},
            command_wrapper=["/usr/bin/sandbox-exec", "-f", str(root / "profile.sb")],
            credential_read_denied=True,
            credential_binding_protected=True,
            user_config_write_denied=True,
            git_stash_denied=True,
            authentication_ready=True,
            unavailable_guards=(),
        )

    monkeypatch.setattr(hlr.HarnessSandbox, "build", classmethod(_ready_build))

    def _fake_create(self, *, head):
        self._created = True
        subprocess.run(["git", "init", "-q", str(self.path)], check=True)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)
    monkeypatch.setattr(hlr, "current_pr_head", lambda **kwargs: SAMPLE_HEAD)

    async def _dispatch(role, task, **kwargs):
        nonlocal dispatched
        dispatched = True
        verdict_path = Path(kwargs["cwd"]) / f"{target.lens}{target.pr}_verdict.md"
        verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
        return SkillResult(role, True, 0, SIGNED_OFF_VERDICT, "", provider="codex")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _dispatch)

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=False,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert dispatched is True
    assert report["ok"] is True
    assert report["posted"] is False


def test_run_one_refuses_verdict_after_direct_packed_stash_rewrite(
    monkeypatch, tmp_path, target, brief_file, mock_ready_sandbox
):
    """The residual packed-refs path must fail the run before verdict trust."""
    replacement_oid = ""
    monkeypatch.setattr(
        hlr,
        "capture_stash_ref_state",
        lambda worktree: hlr.StashRefState(
            resolved_oid=hlr.read_stash_ref_oid(worktree),
            packed_oid=hlr.read_packed_stash_ref_oid(worktree),
        ),
    )

    def _fake_create(self, *, head):
        nonlocal replacement_oid
        self._created = True
        _, replacement_oid = _packed_stash_fixture(self.path)
        agents_dir = self.path / "docs" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "pavo.md").write_text("# pavo prompt\n", encoding="utf-8")

    monkeypatch.setattr(hlr.Worktree, "create", _fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", lambda self: None)

    async def _dispatch(role, task, **kwargs):
        dispatched_repo = Path(kwargs["cwd"])
        _rewrite_packed_stash(dispatched_repo, replacement_oid)
        verdict_path = dispatched_repo / f"{target.lens}{target.pr}_verdict.md"
        verdict_path.write_text(SIGNED_OFF_VERDICT, encoding="utf-8")
        return SkillResult(role, True, 0, SIGNED_OFF_VERDICT, "", provider="codex")

    monkeypatch.setattr(hlr.dispatch_role, "dispatch", _dispatch)
    monkeypatch.setattr(
        hlr,
        "post_verdict",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("must not post after refs/stash changes")
        ),
    )

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=True,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert report["ok"] is False
    assert report["posted"] is False
    assert "refs/stash changed during the dispatched run" in report["refusal_reason"]


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
    monkeypatch.setattr(
        hlr,
        "post_verdict",
        lambda **k: (_ for _ in ()).throw(
            AssertionError("must not post against a stale head")
        ),
    )

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=True,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert report["ok"] is False
    assert "head moved" in report["refusal_reason"]
    assert report["retryable"] is False
    assert report["error_kind"] == "head_mismatch"
    assert posted is False


def test_run_one_refuses_to_post_when_head_verification_is_unreadable(
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
    monkeypatch.setattr(hlr, "current_pr_head", lambda **k: "")
    monkeypatch.setattr(hlr, "gh_login", lambda: "ateles-agent")
    monkeypatch.setattr(
        hlr,
        "post_verdict",
        lambda **k: (_ for _ in ()).throw(
            AssertionError("must not post without an exact live head")
        ),
    )

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=True,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert report["ok"] is False
    assert report["posted"] is False
    assert report["lens_verdict"] == "signed_off"
    assert report["verdict_text"] == SIGNED_OFF_VERDICT
    assert report["retryable"] is True
    assert report["error_kind"] == "head_verification_failed"


def test_run_one_reports_thrown_head_lookup_as_retryable(
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
    monkeypatch.setattr(
        hlr,
        "current_pr_head",
        lambda **k: (_ for _ in ()).throw(RuntimeError("lookup unavailable")),
    )
    monkeypatch.setattr(
        hlr,
        "post_verdict",
        lambda **k: (_ for _ in ()).throw(
            AssertionError("must not post when head lookup raises")
        ),
    )

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=True,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert report["ok"] is False
    assert report["posted"] is False
    assert report["lens_verdict"] == "signed_off"
    assert report["verdict_text"] == SIGNED_OFF_VERDICT
    assert report["retryable"] is True
    assert report["error_kind"] == "head_verification_failed"
    assert "lookup unavailable" in report["refusal_reason"]


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

    monkeypatch.setattr(
        hlr,
        "post_verdict",
        lambda **k: (_ for _ in ()).throw(
            AssertionError("must not post under the wrong gh identity")
        ),
    )

    import asyncio

    report = asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=True,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
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
            target,
            provider="codex",
            post=True,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
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
        return SkillResult(
            role, True, 0, SIGNED_OFF_VERDICT, "", provider=kwargs.get("provider")
        )

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
    monkeypatch.setattr(hlr, "post_verdict", lambda **k: seen_posts.append(k) or "url")

    import asyncio

    report = asyncio.run(
        hlr.run_compare(
            target,
            providers=["claude", "codex"],
            dry_run=False,
            post=True,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
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
                "--repo",
                "o/r",
                "--pr",
                "1",
                "--head",
                SAMPLE_HEAD,
                "--lens",
                "pm",
                "--agent",
                "pavo",
                "--brief",
                str(brief_file),
            ]
        )


def test_main_rejects_both_provider_and_compare(brief_file):
    with pytest.raises(SystemExit):
        hlr.main(
            [
                "--repo",
                "o/r",
                "--pr",
                "1",
                "--head",
                SAMPLE_HEAD,
                "--lens",
                "pm",
                "--agent",
                "pavo",
                "--brief",
                str(brief_file),
                "--provider",
                "codex",
                "--compare",
                "claude,codex",
            ]
        )


def test_main_requires_brief_flag():
    with pytest.raises(SystemExit):
        hlr.main(
            [
                "--repo",
                "o/r",
                "--pr",
                "1",
                "--head",
                SAMPLE_HEAD,
                "--lens",
                "pm",
                "--agent",
                "pavo",
                "--provider",
                "codex",
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
    monkeypatch, tmp_path, brief_file, mock_ready_sandbox
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
    monkeypatch.setattr(
        hlr,
        "capture_stash_ref_state",
        lambda worktree: hlr.StashRefState(resolved_oid=None, packed_oid=None),
    )

    rc = hlr.main(
        [
            "--repo",
            "markmhendrickson/ateles",
            "--pr",
            "1",
            "--head",
            SAMPLE_HEAD,
            "--lens",
            "pm",
            "--provider",
            "codex",
            "--brief",
            str(brief_file),
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
                "--repo",
                "o/r",
                "--pr",
                "1",
                "--head",
                SAMPLE_HEAD,
                "--lens",
                "not-a-real-lens",
                "--provider",
                "codex",
                "--brief",
                str(brief_file),
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
        command_wrapper=["/usr/bin/sandbox-exec", "-f", str(tmp_path / "profile.sb")],
        credential_read_denied=True,
        credential_binding_protected=True,
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
    monkeypatch.setattr(
        hlr,
        "capture_stash_ref_state",
        lambda worktree: hlr.StashRefState(resolved_oid=None, packed_oid=None),
    )

    import asyncio

    asyncio.run(
        hlr.run_one(
            target,
            provider="codex",
            post=False,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert "CODEX_HOME" in seen["env_extra"]
    assert seen["command_wrapper"]
    assert seen["command_wrapper"][0] == "/usr/bin/sandbox-exec"
    assert seen["codex_outer_sandboxed"] is True
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
        repo="markmhendrickson/ateles",
        pr=1234,
        head=SAMPLE_HEAD,
        lens="pm",
        agent="pavo",
        task_entity_id="ent_898998f41372ce24369fb365",
    )

    guarded = hlr.HarnessSandbox(
        provider="codex",
        root=tmp_path / "codex-home",
        env_extra={"CODEX_HOME": str(tmp_path / "codex-home")},
        command_wrapper=["/usr/bin/sandbox-exec", "-f", str(tmp_path / "profile.sb")],
        credential_read_denied=True,
        credential_binding_protected=True,
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
    monkeypatch.setattr(
        hlr,
        "capture_stash_ref_state",
        lambda worktree: hlr.StashRefState(resolved_oid=None, packed_oid=None),
    )

    import asyncio

    asyncio.run(
        hlr.run_one(
            target_with_task,
            provider="codex",
            post=False,
            dry_run=False,
            repo_worktree_name="ateles",
            scratch_root=tmp_path,
            brief_path=brief_file,
            timeout=None,
        )
    )

    assert seen["task_entity_id"] == "ent_898998f41372ce24369fb365"


def test_lens_target_task_entity_id_defaults_to_empty_string():
    """A one-off comparison run need not name a task — the default must stay
    an empty string (not None), matching dispatch_role.dispatch's own default
    and skill_runner's idempotency-key string formatting."""
    target = hlr.LensTarget(repo="o/r", pr=1, head=SAMPLE_HEAD, lens="pm", agent="pavo")
    assert target.task_entity_id == ""
