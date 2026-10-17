"""Lens prompt-source eval (ateles#1337): where a review lens gets its prompt.

Collected by the existing ``pytest execution/evals/`` step in
``.github/workflows/ateles-tests.yml`` (the swarm's eval lane for agent-facing
behaviour). Offline: no model is called and no network is reached. Each
scenario in ``scenarios.json`` runs the real ``harness_lens_runner`` CLI against
a real git target repository, with the real ``AgentLoader`` reading a stand-in
Neotoma — see ``lens_prompt_source_harness``.

Run: pytest execution/evals/lens_prompt_source/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lens_prompt_source_harness as harness  # noqa: E402

DATA = harness.load()
SCENARIOS = DATA["scenarios"]
IDS = [s["id"] for s in SCENARIOS]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_scenario(scenario, monkeypatch, tmp_path):
    _obs, failures = harness.run(scenario, DATA, monkeypatch, tmp_path)
    assert failures == [], scenario["why"]


def test_scenario_ids_are_unique_and_documented():
    assert len(set(IDS)) == len(IDS)
    assert all(s.get("why") and s.get("expect") for s in SCENARIOS)


def test_the_eval_covers_every_required_behaviour():
    required = {
        "sibling_repo_without_lens_files_reviews_with_canonical_prompt",
        "ateles_repo_reviews_with_the_same_command_shape",
        "planted_prompt_in_target_cannot_override_canonical",
        "definition_absent_refuses_despite_target_prompt",
        "neotoma_unreachable_refuses_despite_target_prompt",
        "empty_prompt_definition_refuses",
        "definition_absent_refuses_in_dry_run",
    }
    assert required <= set(IDS)


# An eval whose checks cannot fail is not an eval. Swap in the resolution
# origin/main used before ateles#1337 — the prompt read from the repository
# under review — and every scenario must go red: siblings refuse, planted
# prompts reach the child, and an unloadable definition no longer refuses.


def _target_side_definition(agent: str):
    matches = sorted(Path.home().glob(f"repos/*/docs/agents/{agent}.md"))
    if not matches:
        raise FileNotFoundError(
            f"docs/agents/{agent}.md does not exist — unknown lens agent {agent!r}"
        )
    text = matches[0].read_text(encoding="utf-8")
    return harness.agent_loader.AgentDefinition(
        entity_id="ent_target", name=agent, prompt_markdown=text
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_checks_go_red_with_target_side_prompt_resolution(
    scenario, monkeypatch, tmp_path
):
    monkeypatch.setattr(harness.hlr, "read_agent_definition", _target_side_definition)
    _obs, failures = harness.run(scenario, DATA, monkeypatch, tmp_path)
    assert failures, f"{scenario['id']} stayed green with target-side prompt resolution"
