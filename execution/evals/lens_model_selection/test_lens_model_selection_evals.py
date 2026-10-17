"""Agent-invoked lens CLI eval; collected by the existing execution/evals CI lane."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lens_model_selection_harness as harness  # noqa: E402

SCENARIOS = harness.load()['scenarios']
BY_ID = {scenario['id']: scenario for scenario in SCENARIOS}


@pytest.mark.parametrize('scenario', SCENARIOS, ids=lambda s: s['id'])
def test_scenario(scenario, tmp_path):
    obs = harness.run_scenario(scenario, tmp_path)
    assert harness.check(scenario, obs) == [], (scenario['why'], obs)


def test_scenarios_are_unique_and_cover_the_required_contract():
    assert len(BY_ID) == len(SCENARIOS)
    assert all(s['why'] and s['expect'] for s in SCENARIOS)
    assert {
        'omitted_selectors_preserve_default', 'explicit_sol_reaches_guarded_child',
        'explicit_terra_overrides_class', 'action_class_resolves_each_comparison_provider',
        'dry_run_default', 'dry_run_class', 'dry_run_explicit',
        'invalid_empty_live', 'invalid_empty_dry', 'invalid_empty_compare',
        'guard_refusal_still_prevents_child',
        'reader_unavailable_codex', 'reader_unavailable_cursor',
        'reader_unavailable_compare', 'reader_unavailable_dry_run',
        'reader_available_exact_head_positive',
    } <= BY_ID.keys()


@pytest.mark.parametrize('scenario_id,mutation', [
    ('invalid_empty_live', 'no_validation'),
    ('dispatch_cli_invalid_empty', 'no_validation'),
    ('dispatch_invalid_empty', 'no_validation'),
    ('run_skill_invalid_empty', 'no_validation'),
    ('run_once_invalid_empty', 'no_validation'),
    ('provider_command_invalid_empty', 'no_validation'),
    ('review_prompt_invalid_control', 'no_validation'),
    ('invalid_empty_dry', 'no_validation'),
    ('invalid_empty_compare', 'no_validation'),
    ('explicit_terra_overrides_class', 'drop_model'),
    ('action_class_resolves_each_comparison_provider', 'drop_action_class'),
])
def test_eval_checks_detect_removed_contract(scenario_id, mutation, tmp_path):
    scenario = BY_ID[scenario_id]
    obs = harness.run_scenario(scenario, tmp_path, mutation=mutation)
    assert harness.check(scenario, obs), 'the eval cannot detect this mutation'


@pytest.mark.parametrize('scenario_id', ['dispatch_cli_omitted', 'dispatch_cli_explicit'])
def test_direct_cli_uses_fixture_checkout_without_ambient_skills(monkeypatch, tmp_path, scenario_id):
    """CI has no developer checkout at the dispatch module's import-time path."""
    monkeypatch.setattr(harness.runner.dispatch_role, 'ATELES_REPO', tmp_path / 'absent-ambient-checkout')
    scenario = BY_ID[scenario_id]
    obs = harness.run_scenario(scenario, tmp_path)
    assert harness.check(scenario, obs) == [], obs


def test_effort_eval_detects_removed_forwarding(tmp_path):
    scenario = BY_ID['explicit_effort_inert_cli']
    obs = harness.run_scenario(scenario, tmp_path, mutation='drop_reasoning_effort')
    assert 'actual provider reasoning effort argv differs' in harness.check(scenario, obs)
