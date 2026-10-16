"""Binding contracts with configuration and provider collaborators isolated."""

import asyncio
import json

import pytest

import model_tiering as tiers
import producer_confidence as producer
from lib.daemon_runtime.gating import ExecutionPolicy


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_VENDOR_BINDING_FILE", str(tmp_path / "missing.json"))
    monkeypatch.delenv("APIS_VENDOR_BINDING", raising=False)
    monkeypatch.setenv("APIS_ACTION_POLICY", '{"confidence_scoring":"mid"}')
    monkeypatch.setenv("APIS_PRODUCER_SCORER", "1")


@pytest.mark.parametrize("binding", [
    [], "", {"codex": None}, {"codex": {}}, {"codex": {"mid": " "}},
    {"codex": {"mid": False}}, {"codex": {"mid": ["model"]}},
    {"codex": {"mid": "two models"}}, {"codex": {"mid": "-model"}},
    {"codex": {"mid": "valid"}, "CODEX": {"top": "other"}},
    {"codex": {"mid": "valid", "MID": "other"}},
])
def test_direct_binding_contract_refuses_invalid_selected_values(binding):
    with pytest.raises(tiers.InvalidTierPolicyError):
        tiers.model_for_tier("codex", "mid", binding=binding)


def test_direct_empty_snapshot_retains_documented_unconfigured_contract():
    assert tiers.model_for_tier("codex", "mid", binding={}) is None


def test_binding_normalization_preserves_valid_case_and_padding(monkeypatch):
    monkeypatch.setenv("APIS_VENDOR_BINDING", '{" CODEX ":{" MID ":" selected-model "}}')
    assert tiers.model_for_tier("codex", "mid") == "selected-model"


@pytest.mark.parametrize("binding", [None, {"codex": None}, {"codex": {"mid": " "}},
                                         {"codex": {"mid": "selected-model"}}])
def test_producer_entrypoint_only_scores_with_usable_selected_binding(monkeypatch, binding):
    if binding is not None:
        monkeypatch.setenv("APIS_VENDOR_BINDING", json.dumps(binding))
    calls = []

    async def runner(prompt, tier, task_entity_id):
        calls.append((prompt, tier, task_entity_id))
        return True, '{"confidence":0.9,"rationale":"fixture"}', "codex"

    attempt = asyncio.run(producer.score_unscored_task(
        {"title": "Synthetic task", "body": "Local edit"},
        action_type="local_edit", policy=ExecutionPolicy(entity_id="fixture", loaded=False),
        mechanical_value=0.8, runner=runner,
    ))
    if binding == {"codex": {"mid": "selected-model"}}:
        assert attempt.score is not None and attempt.score.model == "selected-model"
        assert len(calls) == 1
    else:
        assert attempt.score is None and calls == []
