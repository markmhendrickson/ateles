"""Real effect admission and causal mutations, without provider inference."""
import pytest

from guard_probe_policy_harness import load, run_scenario, runner

pytestmark = pytest.mark.skipif(
    runner.trusted_sandbox_exec_path() is None,
    reason="actual effect eval requires the trusted macOS sandbox",
)


@pytest.mark.parametrize("scenario", load(), ids=lambda s: s["id"])
def test_actual_preflight(scenario, tmp_path):
    result = run_scenario(scenario, tmp_path)
    assert result["ok"], result


@pytest.mark.parametrize(
    "scenario_id,mutation",
    [("intact", "remove_ref_deny"),
     ("injected-command", "bypass_plan_validation"),
     ("removed-deny", "force_probe_success")],
)
def test_planted_control_is_detected(scenario_id, mutation, tmp_path):
    scenario = next(s for s in load() if s["id"] == scenario_id)
    result = run_scenario(scenario, tmp_path, mutation=mutation)
    assert not result["ok"], result
