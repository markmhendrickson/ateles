"""The bootstrap lens runner seats no lens on a head whose checks are not green.

`harness_lens_runner.run_one` shares `panel_check_gate` with the Apis review
panel. A red, pending or unreadable head is refused BEFORE any worktree,
sandbox or model call exists; a green head proceeds to the worktree step.

Run: pytest execution/scripts/test_harness_lens_runner_check_hold.py -v
"""

from __future__ import annotations

import asyncio
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
import panel_check_gate as pcg  # noqa: E402

HEAD = "a" * 40
REPO = "owner/repo"


def _outcome(name, state, passed):
    return type(
        "O", (), {"name": name, "state": state, "passed": passed, "superseded": False}
    )()


RED = pcg.describe_gate(HEAD, [_outcome("pytest", "failing (failure)", False)], False)
PENDING = pcg.describe_gate(HEAD, [_outcome("pytest", "pending (queued)", False)], False)
GREEN = pcg.describe_gate(HEAD, [_outcome("pytest", "success", True)], True)
UNREADABLE = pcg.unreadable_gate(HEAD, "HTTP 502")


class _Reached(Exception):
    """Raised by the stubbed Worktree.create: the runner got past the hold."""


@pytest.fixture
def runner(monkeypatch, tmp_path):
    state = {"gate": RED, "worktrees": 0, "bases": []}

    async def read_gate(repository, head_sha, base_ref):
        state["asked"] = (repository, head_sha, base_ref)
        return state["gate"]

    def base_ref(*, repo, pr):
        state["bases"].append((repo, pr))
        return "main"

    def create(self, *, head):
        state["worktrees"] += 1
        raise _Reached()

    monkeypatch.setattr(hlr.panel_check_gate, "read_check_gate", read_gate)
    monkeypatch.setattr(hlr, "current_pr_base_ref", base_ref)
    monkeypatch.setattr(hlr, "check_headroom", lambda provider: None)
    monkeypatch.setattr(hlr.Worktree, "create", create)
    brief = tmp_path / "brief.md"
    brief.write_text("brief\n", encoding="utf-8")
    target = hlr.LensTarget(repo=REPO, pr=7, head=HEAD, lens="pm", agent="pavo")

    def go(dry_run=False):
        return asyncio.run(
            hlr.run_one(
                target, provider="claude", post=False, dry_run=dry_run,
                repo_worktree_name="repo", scratch_root=tmp_path, brief_path=brief,
                timeout=None,
            )
        )

    return state, go


@pytest.mark.parametrize("gate", [RED, PENDING, UNREADABLE], ids=["red", "pending", "unreadable"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_a_head_that_is_not_green_is_refused_before_any_worktree(runner, gate, dry_run):
    state, go = runner
    state["gate"] = gate
    report = go(dry_run=dry_run)
    assert report["ok"] is False
    assert report["held_for_checks"] is True
    assert report["no_model_call_made"] is True and report["posted"] is False
    assert HEAD[:9] in report["reason"]
    assert "held until the head's checks are green" in report["reason"]
    assert state["worktrees"] == 0, "no worktree may exist for a held lens"
    assert state["asked"] == (REPO, HEAD, "main")


def test_a_green_head_proceeds_to_the_worktree(runner):
    state, go = runner
    state["gate"] = GREEN
    with pytest.raises(_Reached):
        go()
    assert state["worktrees"] == 1


def test_the_real_hold_names_failing_checks(runner):
    state, _ = runner
    target = hlr.LensTarget(repo=REPO, pr=7, head=HEAD, lens="pm", agent="pavo")
    state["gate"] = RED
    reason = asyncio.run(hlr.head_checks_hold(target))
    assert reason and "pytest" in reason
    state["gate"] = GREEN
    assert asyncio.run(hlr.head_checks_hold(target)) is None
