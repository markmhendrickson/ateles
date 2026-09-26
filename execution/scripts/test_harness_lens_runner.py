"""Tests for harness_lens_runner.py — the runner that sends one lens review
of one PR head to a chosen harness provider (claude/codex/cursor), reusing
dispatch_role.py / harness_router rather than paralleling them.

No live codex/cursor-agent process is ever started here: every dispatch call
site is monkeypatched at `dispatch_role.dispatch`, and every `gh`/`git`
subprocess call is monkeypatched at `subprocess.run`. Per the task's own hard
rule, this suite makes no model call.
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


# ── Guard configuration in the rendered command ----------------------------------


def test_sandbox_build_codex_uses_codex_home_isolation(tmp_path):
    sandbox = hlr.HarnessSandbox.build("codex", tmp_path)
    assert sandbox.env_extra == {"CODEX_HOME": str(sandbox.root)}
    assert sandbox.root.is_dir()
    assert any("git_stash_guard" in g for g in sandbox.unavailable_guards)


def test_sandbox_build_cursor_uses_home_isolation(tmp_path):
    sandbox = hlr.HarnessSandbox.build("cursor", tmp_path)
    assert sandbox.env_extra == {"HOME": str(sandbox.root)}
    assert any("credential_read_guard" in g for g in sandbox.unavailable_guards)


def test_sandbox_build_claude_has_no_override(tmp_path):
    sandbox = hlr.HarnessSandbox.build("claude", tmp_path)
    assert sandbox.env_extra == {}
    assert sandbox.unavailable_guards == ()


def test_refuse_if_guard_required_blocks_codex_without_isolation():
    reason = hlr.refuse_if_guard_required("codex", needs_full_guards=True)
    assert reason is not None
    assert "hook mechanism" in reason


def test_refuse_if_guard_required_allows_claude_always():
    assert hlr.refuse_if_guard_required("claude", needs_full_guards=True) is None


def test_refuse_if_guard_required_allows_codex_when_sandboxed():
    assert hlr.refuse_if_guard_required("codex", needs_full_guards=False) is None


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
    assert "codex" in report["example_command"][0]
    assert "CODEX_HOME" in report["sandbox_env_extra"]
    assert report["prompt_chars"] > 0
    assert created["head"] == SAMPLE_HEAD


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
    monkeypatch, tmp_path, target, brief_file
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
    monkeypatch, tmp_path, target, brief_file
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

    def _fake_run(cmd, **kwargs):
        class _R:
            stdout = json.dumps({"headRefOid": SAMPLE_HEAD})
            stderr = ""
            returncode = 0
        return _R()

    monkeypatch.setattr(hlr.subprocess, "run", _fake_run)

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
    monkeypatch, tmp_path, target, brief_file
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

    def _fake_run(cmd, **kwargs):
        class _R:
            stdout = json.dumps({"headRefOid": "b" * 40})  # moved!
            stderr = ""
            returncode = 0
        return _R()

    monkeypatch.setattr(hlr.subprocess, "run", _fake_run)

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
    monkeypatch, tmp_path, target, brief_file
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

    def _fake_run(cmd, **kwargs):
        class _R:
            stdout = json.dumps({"headRefOid": SAMPLE_HEAD})
            stderr = ""
            returncode = 0
        return _R()

    monkeypatch.setattr(hlr.subprocess, "run", _fake_run)
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
    monkeypatch, tmp_path, target, brief_file
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

    def _fake_run(cmd, **kwargs):
        class _R:
            stdout = json.dumps({"headRefOid": SAMPLE_HEAD})
            stderr = ""
            returncode = 0
        return _R()

    monkeypatch.setattr(hlr.subprocess, "run", _fake_run)
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
    monkeypatch, tmp_path, target, brief_file
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

    def _fake_run(cmd, **kwargs):
        class _R:
            stdout = json.dumps({"headRefOid": SAMPLE_HEAD})
            stderr = ""
            returncode = 0
        return _R()

    monkeypatch.setattr(hlr.subprocess, "run", _fake_run)
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


# ── Worktree lifecycle: env_extra is threaded through dispatch_role.dispatch -----


def test_run_one_passes_sandbox_env_extra_to_dispatch(
    monkeypatch, tmp_path, target, brief_file
):
    seen = {}

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
    assert seen["seated_reviewer"] is False
    assert seen["provider"] == "codex"
