"""Existing target policy qualification; every provider response is synthetic."""
import copy
import json
from pathlib import Path

import pytest
from lib.daemon_runtime import artifact_resolver as reader
import build_delivery

REPO = "owner/repo"


@pytest.fixture(autouse=True)
def isolated_ledger(monkeypatch, tmp_path):
    path = (tmp_path / "policy-ledger.json").resolve()
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(path))
    assert Path(build_delivery._registry_path()).resolve() == path and not path.exists()


@pytest.fixture
def rule(monkeypatch):
    rule = {"id": 42, "source_type": "Repository", "source": REPO,
            "target": "branch", "enforcement": "active", "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [{"type": "non_fast_forward"}]}
    def listing(*a):
        return 0, json.dumps([{k: rule[k] for k in ("id", "source_type", "source", "target", "enforcement")}])
    monkeypatch.setattr(reader, "_gh_rulesets_view", listing)
    monkeypatch.setattr(reader, "_gh_ruleset_view", lambda *a: (0, json.dumps(rule)))
    return rule


def test_actual_visible_dynamic_default_rule_qualifies(rule):
    assert reader.read_back_default_protection(REPO, lambda *a: "inert-read-token") is None


@pytest.mark.parametrize("change", [
    {"bypass_actors": None}, {"bypass_actors": [{"actor_type": "Integration", "actor_id": 99}]},
    {"enforcement": "disabled"}, {"enforcement": "evaluate"}, {"target": "tag"},
    {"source": "foreign/repo"}, {"source_type": "Enterprise"}, {"rules": []},
    {"conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}}},
    {"conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": ["refs/heads/main"]}}},
])
def test_unqualified_or_bypassed_policy_holds(rule, change):
    rule.update(copy.deepcopy(change))
    assert reader.read_back_default_protection(REPO, lambda *a: "inert-read-token")


def test_omitted_bypass_is_not_empty(rule):
    del rule["bypass_actors"]
    assert reader.read_back_default_protection(REPO, lambda *a: "inert-read-token")


def test_inherited_rule_is_scoped_by_repository_listing_and_owner(rule):
    rule.update(source_type="Organization", source="owner")
    rule["conditions"]["repository_name"] = {"include": ["~ALL"], "exclude": []}
    assert reader.read_back_default_protection(REPO, lambda *a: "inert-read-token") is None
    rule["conditions"]["repository_name"]["exclude"] = ["repo"]
    assert reader.read_back_default_protection(REPO, lambda *a: "inert-read-token")


def test_foreign_inherited_owner_and_unknown_scope_do_not_qualify(rule):
    rule.update(source_type="Organization", source="other-owner")
    assert reader.read_back_default_protection(REPO, lambda *a: "inert-read-token")
    rule["source"] = "owner"
    rule["conditions"]["repository_id"] = {"repository_ids": [999]}
    assert reader.read_back_default_protection(REPO, lambda *a: "inert-read-token")


def test_missing_read_credential_and_unreadable_provider_hold(rule, monkeypatch):
    assert reader.read_back_default_protection(REPO, lambda *a: "")
    monkeypatch.setattr(reader, "_gh_rulesets_view", lambda *a: (403, "{}"))
    assert reader.read_back_default_protection(REPO, lambda *a: "inert-read-token")
