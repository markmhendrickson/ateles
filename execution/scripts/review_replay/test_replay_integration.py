"""Replay checkout and the replay path through the runner's ``run_one``.

The child is a stub that writes a verdict; the sandbox is a ready stand-in so
the test does not depend on the host's ``sandbox-exec``. What is real here: the
checkout, the lens task text, the verdict read, the swarm's own verdict reader
and the record the replay CLI writes.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

import harness_lens_runner as hlr
from skill_runner import SkillResult
from review_replay import review_replay as rr
from review_replay.dataset import RunUnit
from review_replay.replay_worktree import (
    ReplayWorktree,
    ReplayWorktreeError,
    ensure_commit,
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(cwd),
        },
    ).stdout.strip()


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "ateles"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "lib.py").write_text("def gate():\n    return False\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "pr")
    (repo / "lib.py").write_text(
        "def gate():\n    # reviewed change\n    return False\n"
    )
    git(repo, "commit", "-qam", "pr change")
    head = git(repo, "rev-parse", "HEAD")
    (repo / "lib.py").write_text("def gate():\n    return True  # the later fix\n")
    git(repo, "commit", "-qam", "fix: the later fix")
    git(repo, "tag", "v-future")
    git(repo, "checkout", "-q", "main")
    return SimpleNamespaceRepo(repo, base, head)


class SimpleNamespaceRepo:
    def __init__(self, path, base, head):
        self.path, self.base, self.head = path, base, head


PATCH = """diff --git a/lib.py b/lib.py
--- a/lib.py
+++ b/lib.py
@@ -1,3 +1,3 @@
 def gate():
     # reviewed change
-    return False
+    return True
"""


def test_replay_checkout_hides_history_after_the_head(source_repo, tmp_path):
    wt = ReplayWorktree(
        source=source_repo.path, path=tmp_path / "wt", base_sha=source_repo.base, pr=1
    )
    wt.create(head=source_repo.head)
    try:
        assert git(wt.path, "rev-parse", "HEAD") == source_repo.head == wt.head_sha
        log = git(wt.path, "log", "--all", "--format=%s")
        assert "the later fix" not in log and "pr change" in log
        assert git(wt.path, "tag") == ""
        assert (
            git(wt.path, "rev-parse", "origin/main")
            == source_repo.base
            == git(wt.path, "rev-parse", "main")
        )
        assert git(wt.path, "diff", "--stat", "origin/main...HEAD").count("lib.py") == 1
        assert (
            subprocess.run(
                ["git", "-C", str(wt.path), "fetch", "origin"], capture_output=True
            ).returncode
            != 0
        )
        assert (
            git(wt.path, "status", "--porcelain") == ""
        )  # description file is excluded
        assert (wt.path / "PR_DESCRIPTION.md").read_text().startswith("(no description")
    finally:
        wt.remove()
    assert not (tmp_path / "wt").exists()


def test_patch_is_committed_on_top_so_the_pr_diff_includes_it(source_repo, tmp_path):
    patch = tmp_path / "p.patch"
    patch.write_text(PATCH)
    wt = ReplayWorktree(
        source=source_repo.path,
        path=tmp_path / "wt",
        base_sha=source_repo.base,
        patches=[patch],
    )
    wt.create(head=source_repo.head)
    try:
        assert (
            wt.head_sha != source_repo.head
            and git(wt.path, "rev-parse", "HEAD") == wt.head_sha
        )
        assert git(wt.path, "rev-parse", "HEAD~1") == source_repo.head
        assert "return True" in (wt.path / "lib.py").read_text()
        assert "return True" in git(wt.path, "diff", "origin/main...HEAD")
    finally:
        wt.remove()


def test_patch_that_does_not_apply_is_an_error_not_a_silent_unpatched_run(
    source_repo, tmp_path
):
    bad = tmp_path / "bad.patch"
    bad.write_text(PATCH.replace("return False\n+", "return Nothing\n+"))
    wt = ReplayWorktree(
        source=source_repo.path,
        path=tmp_path / "wt",
        base_sha=source_repo.base,
        patches=[bad],
    )
    with pytest.raises(ReplayWorktreeError, match="does not apply"):
        wt.create(head=source_repo.head)
    wt.remove()


def test_missing_commit_is_not_fetched_when_fetching_is_not_allowed(source_repo):
    with pytest.raises(ReplayWorktreeError, match="fetching is not allowed"):
        ensure_commit(source_repo.path, "f" * 40, 5, may_fetch=False)


# -- run_one in replay mode ----------------------------------------------------------------


def _ready_sandbox(provider, tmp_root, **kwargs):
    root = tmp_root / "home"
    root.mkdir(parents=True, exist_ok=True)
    return hlr.HarnessSandbox(
        provider=hlr.parse_provider(provider)[0],
        root=root,
        env_extra={"ATELES_LOCAL_REVIEW_HOME": str(root)},
        command_wrapper=["/usr/bin/sandbox-exec", "-f", str(root / "profile.sb")],
        credential_read_denied=True,
        credential_binding_protected=True,
        user_config_write_denied=True,
        git_stash_denied=True,
        authentication_ready=True,
        unavailable_guards=(),
        review_write_confined=True,
    )


@pytest.fixture
def replay_env(monkeypatch, source_repo, tmp_path):
    monkeypatch.setattr(
        hlr.HarnessSandbox,
        "build",
        classmethod(
            lambda cls, provider, tmp_root, **kw: _ready_sandbox(provider, tmp_root)
        ),
    )
    monkeypatch.setattr(hlr, "capture_stash_ref_state", lambda wt: None)
    monkeypatch.setattr(
        hlr, "verify_stash_ref_unchanged_after_dispatch", lambda wt, before: None
    )
    # Anything that could touch GitHub or publish fails the test.
    for name in ("gh_login", "current_pr_head", "post_verdict"):
        monkeypatch.setattr(
            hlr,
            name,
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("replay must not reach GitHub")
            ),
        )
    brief = tmp_path / "brief.md"
    brief.write_text("# brief\n")
    results = tmp_path / "out" / "results.jsonl"
    cfg = rr.ReplayConfig(
        set_dir=tmp_path,
        brief=brief,
        results=results,
        timeout_seconds=60,
        source_repo_root=source_repo.path,
        codex_effort=None,
        ollama_thinking=False,
        ollama_context_tokens=None,
        openrouter_key_ref="op://x",
    )
    unit = RunUnit(
        case_id="finding-1",
        kind="confirmed_blocker",
        repo="ateles",
        pr=1,
        head_sha=source_repo.head,
        base_sha=source_repo.base,
        lens="security",
        labels=[],
    )
    return cfg, unit


def _stub_child(verdict_body: str, **metrics_out):
    async def run(launch, metrics):
        metrics.update(metrics_out)
        launch.verdict_path.write_text(
            verdict_body.replace(
                "HEAD", launch.task_text.split("TARGET HEAD: ")[1].split()[0]
            )
        )
        return SkillResult(launch.agent, True, 0, "", "")

    return run


def test_replay_one_records_a_valid_verdict_and_never_posts(replay_env, monkeypatch):
    cfg, unit = replay_env
    body = (
        "<!-- review:security commit=HEAD -->\n**\U0001f916 Falco — Ateles swarm, security review**\n**BLOCKED**\n\n"
        "[BLOCKING] fail-open: `lib.py:2` gate returns the wrong value\n"
    )
    monkeypatch.setattr(
        rr,
        "run_candidate_child",
        _stub_child(
            body, wall_seconds=12.5, turns=4, tool_errors=1, tokens_in=10, cost_usd=0.02
        ),
    )
    rec = asyncio.run(
        rr.replay_one(
            unit,
            provider="openrouter:vendor/m",
            candidate="cand",
            run_index=0,
            cfg=cfg,
            run_cost_limit=None,
        )
    )
    assert rec.get("incomplete") is None and rec["verdict_valid"] is True
    assert rec["verdict_token"].lower() == "blocked" and rec["blocking_count"] == 1
    assert rec["findings"][0]["file"] == "lib.py" and rec["findings"][0]["line"] == 2
    assert (
        rec["wall_seconds"],
        rec["turns"],
        rec["tool_errors"],
        rec["tokens_in"],
        rec["cost_usd"],
    ) == (12.5, 4, 1, 10, 0.02)
    assert rec["replay_head_sha"] == unit.head_sha
    assert (
        (cfg.results.parent / (cfg.results.name + ".verdicts") / rec["verdict_file"])
        .read_text()
        .startswith("<!-- review:security")
    )


def test_a_verdict_the_swarm_reader_rejects_is_recorded_invalid(
    replay_env, monkeypatch
):
    cfg, unit = replay_env
    monkeypatch.setattr(
        rr, "run_candidate_child", _stub_child("looks fine to me, ship it\n")
    )
    rec = asyncio.run(
        rr.replay_one(
            unit,
            provider="ollama:m",
            candidate="cand",
            run_index=0,
            cfg=cfg,
            run_cost_limit=None,
        )
    )
    assert (
        rec["verdict_valid"] is False
        and rec["completed"] is False
        and rec["blocking_count"] == 0
    )
    assert "marker" in rec["validity_reason"]


def test_a_launch_failure_is_incomplete_so_resume_retries_it(replay_env, monkeypatch):
    cfg, unit = replay_env

    async def boom(launch, metrics):
        metrics["launch_error"] = "the password manager did not return the key"
        return SkillResult(launch.agent, False, None, "", "", error="x")

    monkeypatch.setattr(rr, "run_candidate_child", boom)
    rec = asyncio.run(
        rr.replay_one(
            unit,
            provider="openrouter:m",
            candidate="cand",
            run_index=0,
            cfg=cfg,
            run_cost_limit=None,
        )
    )
    assert rec["incomplete"] is True and "password manager" in rec["incomplete_reason"]
    rr.append_line(cfg.results, rec)
    assert rr.load_done(cfg.results) == set()


def test_candidate_kinds_cannot_be_dispatched_outside_replay_and_replay_cannot_post(
    tmp_path,
):
    target = hlr.LensTarget(repo="o/r", pr=1, head="a" * 40, lens="pm", agent="pavo")
    common = dict(
        dry_run=False,
        repo_worktree_name="r",
        scratch_root=tmp_path,
        brief_path=tmp_path / "b",
        timeout=1,
    )
    with pytest.raises(ValueError, match="only through"):
        asyncio.run(hlr.run_one(target, provider="ollama:m", post=False, **common))
    hooks = hlr.ReplayHooks(worktree_factory=None, child_runner=None, on_verdict=None)
    with pytest.raises(ValueError, match="never posts"):
        asyncio.run(
            hlr.run_one(target, provider="claude", post=True, replay=hooks, **common)
        )


def test_results_line_carries_every_field_the_scorer_reads(replay_env, monkeypatch):
    cfg, unit = replay_env
    full = dict(
        wall_seconds=1.0,
        turns=2,
        tool_calls=3,
        tool_errors=0,
        tokens_in=4,
        tokens_out=5,
        tokens_cache_read=6,
        tokens_cache_write=7,
        cost_usd=0.01,
        cost_source="provider_usage_cost",
        timeout=False,
        exit_status=0,
    )
    monkeypatch.setattr(rr, "run_candidate_child", _stub_child("nope\n", **full))
    rec = asyncio.run(
        rr.replay_one(
            unit,
            provider="claude",
            candidate="cand",
            run_index=2,
            cfg=cfg,
            run_cost_limit=None,
        )
    )
    rr.append_line(cfg.results, rec)
    back = json.loads(cfg.results.read_text().splitlines()[0])
    for key in (
        "case_id",
        "candidate",
        "run_index",
        "lens",
        "verdict_token",
        "findings",
        "verdict_valid",
        "tokens_in",
        "tokens_out",
        "tokens_cache_read",
        "cost_usd",
        "wall_seconds",
        "turns",
        "tool_errors",
        "timeout",
        "exit_status",
    ):
        assert key in back, key
    assert back["run_index"] == 2


def test_cli_accepts_num_ctx_and_passes_it_to_the_launch(monkeypatch, tmp_path):
    (tmp_path / "dataset.json").write_text("[]")
    seen = {}

    async def fake_run_all(units, args, cfg, guard):
        seen["num_ctx"] = cfg.ollama_num_ctx
        return 0

    monkeypatch.setattr(rr, "run_all", fake_run_all)
    args = [
        "--candidate",
        "ollama:m",
        "--set-dir",
        str(tmp_path),
        "--brief",
        str(tmp_path / "b.md"),
    ]
    args += ["--results", str(tmp_path / "r.jsonl"), "--num-ctx", "131072"]
    assert rr.main(args) == 0 and seen["num_ctx"] == 131072
