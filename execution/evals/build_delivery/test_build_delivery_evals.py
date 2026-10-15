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
        # a deferral survives a restart, with controls
        "restart_redispatches_a_deferred_task_through_the_gates",
        "restart_does_not_run_a_deferred_task_the_build_completed",
        "restart_duplicate_delivery_does_not_double_dispatch",
        "restart_surfaces_a_deferred_task_it_cannot_re_read",
        # recovery is live and complete, and a deferral is proven by read-back
        "restart_sweep_reaches_every_marked_task_past_broken_records",
        "restart_recovers_a_deferral_whose_first_write_dropped_a_field",
        "deferral_that_never_persists_is_warned_not_acknowledged",
        "queued_deferred_task_is_re_validated_when_its_slot_comes",
        "unrecordable_deferral_whose_marker_cannot_be_cleared_is_announced_truthfully_and_dispatched_once",
        "unrecordable_deferral_whose_context_cannot_be_cleared_is_never_dispatched",
        "partially_written_deferral_without_its_reason_is_never_dispatched",
        "intact_marker_is_dispatched_once_when_the_build_ends_before_the_notice",
        "intact_marker_is_dispatched_once_when_the_build_ends_during_the_notice",
        "intact_marker_is_dispatched_once_when_the_build_ends_after_the_notice",
        # the rollback never overwrites a task that moved on, and says what is true
        "rollback_never_overwrites_a_gate_hold_arriving_before_the_clears",
        "rollback_never_overwrites_a_gate_hold_arriving_between_the_clears",
        "rollback_never_overwrites_a_gate_hold_arriving_after_the_clears",
        "rollback_reports_a_running_status_as_observed_and_asks_for_a_check",
        "rollback_reports_a_terminal_status_with_a_marker_left_as_not_settled",
        "rollback_reports_a_cancelled_status_as_observed_not_as_a_story",
        "rollback_reports_an_unmapped_status_as_unknown",
        "rollback_clear_is_conditional_so_a_hold_landing_after_the_read_survives",
        "deferral_chain_stops_when_a_write_returns_no_version_and_a_hold_lands_after_write_1",
        "deferral_chain_stops_when_a_write_returns_no_version_and_a_hold_lands_after_write_2",
        # the worktree itself is verified, on both paths
        "handoff_launches_no_child_when_the_worktree_remote_is_wrong_case_1",
        "handoff_launches_no_child_when_the_worktree_remote_is_wrong_case_2",
        "task_launches_no_child_when_the_worktree_remote_is_wrong_case_1",
        "task_launches_no_child_when_the_worktree_remote_is_wrong_case_2",
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


# The restart guarantee has two halves, and each must be load-bearing: the record
# on the task, and the sweep that reads it.
RESTART = [i for i in IDS if i.startswith("restart_redispatches")]


@pytest.mark.parametrize("mutation", ["no_durable_record", "no_startup_sweep"])
def test_the_restart_scenario_goes_red_without_either_half(mutation, tmp_path):
    scenario = BY_ID["restart_redispatches_a_deferred_task_through_the_gates"]
    obs = harness.run_scenario(scenario, tmp_path, mutation=mutation)
    problems = harness.check(scenario, obs)
    assert problems, f"{mutation} did not break the restart scenario"
    assert obs["spawns"] == [], "the task was dispatched without its half of the mechanism"


def test_the_durable_record_is_what_the_mid_build_scenario_checks(tmp_path):
    scenario = BY_ID["independent_task_mid_build_is_deferred_then_run"]
    obs = harness.run_scenario(scenario, tmp_path, mutation="no_durable_record")
    assert any("marked deferred on the task" in p for p in harness.check(scenario, obs))


def test_the_scale_scenario_goes_red_without_a_real_continuation(tmp_path):
    scenario = BY_ID["restart_sweep_reaches_every_marked_task_past_broken_records"]
    obs = harness.run_scenario(scenario, tmp_path, mutation="no_continuation")
    problems = harness.check(scenario, obs)
    # One sweep alone no longer finishes the job: later sweeps had to do it.
    assert any("repeated sweeps" in p for p in problems), problems


@pytest.mark.parametrize(
    "scenario_id",
    [
        "restart_recovers_a_deferral_whose_first_write_dropped_a_field",
        "deferral_that_never_persists_is_warned_not_acknowledged",
    ],
)
def test_the_read_back_scenarios_go_red_without_the_read_back(scenario_id, tmp_path):
    scenario = BY_ID[scenario_id]
    obs = harness.run_scenario(scenario, tmp_path, mutation="no_readback")
    assert harness.check(scenario, obs), "an unverified acknowledgement went unnoticed"


@pytest.mark.parametrize("path", ["handoff", "task"])
@pytest.mark.parametrize("case", [1, 2])
def test_the_worktree_scenarios_go_red_when_only_the_source_is_checked(path, case, tmp_path):
    scenario = BY_ID[f"{path}_launches_no_child_when_the_worktree_remote_is_wrong_case_{case}"]
    obs = harness.run_scenario(scenario, tmp_path, mutation="no_worktree_check")
    assert harness.check(scenario, obs)
    assert obs["token_requests"], "the token was resolved for the unchecked worktree"


def test_the_slot_wait_scenario_goes_red_without_the_re_validation(tmp_path):
    scenario = BY_ID["queued_deferred_task_is_re_validated_when_its_slot_comes"]
    obs = harness.run_scenario(scenario, tmp_path, mutation="no_revalidation")
    problems = harness.check(scenario, obs)
    assert any("stopped qualifying" in p or "status write" in p for p in problems), problems


def test_an_unrecordable_deferral_that_nobody_is_told_about_goes_red(tmp_path):
    scenario = BY_ID["deferral_that_never_persists_is_warned_not_acknowledged"]
    obs = harness.run_scenario(scenario, tmp_path, mutation="silent_unrecorded")
    assert any("not warned" in p for p in harness.check(scenario, obs))


@pytest.mark.parametrize(
    "scenario_id",
    [
        "unrecordable_deferral_whose_context_cannot_be_cleared_is_never_dispatched",
        "partially_written_deferral_without_its_reason_is_never_dispatched",
    ],
)
def test_the_half_written_marker_scenarios_go_red_if_the_reason_is_not_required(scenario_id, tmp_path):
    scenario = BY_ID[scenario_id]
    obs = harness.run_scenario(scenario, tmp_path, mutation="reason_not_required")
    assert harness.check(scenario, obs), "a marker without its reason was acted on unnoticed"
    assert obs["spawns"], "the half-written marker was dispatched"


def test_the_intact_marker_scenarios_go_red_without_the_registration(tmp_path):
    """Dropping the catch-up strands the release-before case behind its promise."""
    scenario = BY_ID["intact_marker_is_dispatched_once_when_the_build_ends_before_the_notice"]
    obs = harness.run_scenario(scenario, tmp_path, mutation="drop_intact_registration")
    problems = harness.check(scenario, obs)
    assert problems and obs["spawns"] == [], problems
    assert any("WILL re-dispatch" in m for m in obs["notifier"].sent), "the promise was made"


@pytest.mark.parametrize("when", ["before", "between"])
def test_the_gate_hold_scenarios_go_red_if_the_rollback_does_not_re_read_first(when, tmp_path):
    scenario = BY_ID[f"rollback_never_overwrites_a_gate_hold_arriving_{when}_the_clears"]
    obs = harness.run_scenario(scenario, tmp_path, mutation="skip_pre_clear_read")
    problems = harness.check(scenario, obs)
    assert any("blocked_reason after the rollback" in p or "moved on" in p for p in problems), problems


def test_the_landing_scenario_goes_red_if_the_clear_ignores_the_version(tmp_path):
    scenario = BY_ID["rollback_clear_is_conditional_so_a_hold_landing_after_the_read_survives"]
    obs = harness.run_scenario(scenario, tmp_path, mutation="no_expected_version")
    problems = harness.check(scenario, obs)
    assert any("blocked_reason after the rollback" in p or "refused" in p for p in problems), problems


@pytest.mark.parametrize("n", [1, 2])
def test_the_no_version_scenarios_go_red_if_a_fresh_version_is_adopted(n, tmp_path):
    scenario = BY_ID[f"deferral_chain_stops_when_a_write_returns_no_version_and_a_hold_lands_after_write_{n}"]
    obs = harness.run_scenario(scenario, tmp_path, mutation="fresh_version_fallback")
    problems = harness.check(scenario, obs)
    assert any("blocked_reason after the rollback" in p or "writes after the hold" in p for p in problems), problems
