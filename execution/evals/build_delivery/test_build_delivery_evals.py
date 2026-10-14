"""Build-delivery eval: a Cicada build that is meant to open a PR can deliver it,
whichever path launched it (ateles#1448 canary).

Collected by the existing ``pytest execution/evals/`` step in
``.github/workflows/ateles-tests.yml``. Offline: no model, no network. Each
scenario in ``scenarios.json`` drives the real launch code and the real
``run_skill`` to a stub ``codex`` that does a real ``git commit`` and
``git push`` to a local bare remote and a stub ``gh pr create``; the assertions
are on that world (the commit on the remote's branch, the PR on record).

Run: pytest execution/evals/build_delivery/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_delivery_harness as harness  # noqa: E402

DATA = harness.load()
SCENARIOS = DATA["scenarios"]
IDS = [s["id"] for s in SCENARIOS]
BY_ID = {s["id"]: s for s in SCENARIOS}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_scenario(scenario, tmp_path):
    obs = harness.run_scenario(scenario, tmp_path)
    assert harness.check(scenario, obs) == [], scenario["why"]


def test_scenario_ids_are_unique_and_documented():
    assert len(set(IDS)) == len(IDS)
    assert all(s.get("why") and s.get("expect") and s.get("path") for s in SCENARIOS)


def test_the_eval_covers_every_required_behaviour():
    required = {
        # both launch paths deliver
        "handoff_delivers_from_its_worktree",
        "task_delivers_from_its_worktree",
        "task_with_an_explicit_repo_field_delivers",
        # no child when provisioning fails, on both paths
        "handoff_launches_no_child_when_the_clone_is_the_wrong_repository",
        "task_launches_no_child_when_the_clone_is_the_wrong_repository",
        "handoff_launches_no_child_when_there_is_no_clone",
        "task_launches_no_child_when_there_is_no_clone",
        # a dispatch that does not deliver is unchanged
        "non_delivery_task_runs_unchanged",
        # the duplicate, and its controls
        "tracking_task_mid_build_adds_no_launch_and_no_status_write",
        "independent_task_mid_build_is_deferred_then_run",
        "control_task_for_a_different_issue_is_not_held",
        "control_gate_override_is_not_held",
        "claim_is_released_and_deferred_task_runs_after_a_failed_build",
    }
    assert required <= set(IDS)


# An eval whose checks cannot fail is not an eval. Each delivery grant is removed
# in turn and the SAME scenario must go red, at the effect that grant protects.
DELIVERING = [
    "handoff_delivers_from_its_worktree",
    "task_delivers_from_its_worktree",
]


@pytest.mark.parametrize("scenario_id", DELIVERING)
@pytest.mark.parametrize("mutation", ["no_grants", "no_network", "no_git_roots"])
def test_checks_go_red_when_a_delivery_grant_is_removed(scenario_id, mutation, tmp_path):
    scenario = BY_ID[scenario_id]
    obs = harness.run_scenario(scenario, tmp_path, mutation=mutation)
    problems = harness.check(scenario, obs)

    assert problems, f"{mutation} did not break {scenario_id}: the eval cannot fail"
    # And it fails at the EFFECT, not merely in the arguments.
    assert any("commit is on the remote" in p for p in problems), problems
    assert obs["prs"] == [], "a PR was recorded without the grant"
    assert obs["remote_branches"] == {}, "something reached the remote without the grant"


def test_without_a_worktree_cwd_the_child_cannot_commit(tmp_path):
    obs = harness.run_scenario(BY_ID["task_delivers_from_its_worktree"], tmp_path, mutation="no_grants")
    assert obs["commits"] == [] and obs["prs"] == []


def test_without_network_the_commit_exists_locally_but_never_reaches_the_remote(tmp_path):
    obs = harness.run_scenario(BY_ID["task_delivers_from_its_worktree"], tmp_path, mutation="no_network")
    assert obs["commits"], "the child should have committed locally"
    assert obs["remote_branches"] == {} and obs["prs"] == []


def test_without_the_git_roots_the_child_cannot_commit(tmp_path):
    obs = harness.run_scenario(BY_ID["task_delivers_from_its_worktree"], tmp_path, mutation="no_git_roots")
    assert obs["commits"] == [] and obs["prs"] == []


def test_a_wrong_clone_gets_no_fetch_worktree_or_child(tmp_path):
    """The refusal precedes everything: the wrong clone is left exactly as found."""
    scenario = BY_ID["task_launches_no_child_when_the_clone_is_the_wrong_repository"]
    obs = harness.run_scenario(scenario, tmp_path)
    world = obs["world"]
    assert obs["launches"] == [] and obs["worktree_dirs"] == []
    assert harness._git(world.clone, "worktree", "list").count("\n") == 0
