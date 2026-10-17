"""Strict lens admission is separate from generic compatibility defaults."""

import copy

import pytest

from lib.daemon_runtime.agent_loader import AgentLoader, validate_lens_definition


def response():
    return {
        "entity_id": "ent_fixture",
        "entity_type": "agent_definition",
        "snapshot": {
            "name": "pavo",
            "prompt_markdown": "Canonical role",
            "status": "active",
            "tool_allowlist": ["Read"],
        },
    }


@pytest.fixture
def loader(monkeypatch):
    monkeypatch.setenv("PAVO_AGENT_DEFINITION_ID", "ent_fixture")
    return AgentLoader("pavo")


@pytest.mark.parametrize(
    "field,value",
    [
        ("entity_id", "ent_wrong"),
        ("entity_type", "task"),
        ("name", None),
        ("name", "other"),
        ("prompt_markdown", None),
        ("prompt_markdown", "  "),
        ("status", None),
        ("status", "retired"),
        ("status", "unknown"),
        ("status", []),
        ("tool_allowlist", None),
        ("tool_allowlist", []),
        ("tool_allowlist", ""),
        ("tool_allowlist", "[]"),
        ("tool_allowlist", "[bad"),
        ("tool_allowlist", [1]),
        ("tool_allowlist", [""]),
        ("tool_allowlist", {}),
        ("tool_allowlist", " , "),
    ],
)
def test_explicit_role_refuses_bad_authority(loader, monkeypatch, field, value):
    data = response()
    (data if field in ("entity_id", "entity_type") else data["snapshot"])[field] = value
    monkeypatch.setattr(loader, "_neotoma", lambda *a: data)
    assert loader.load_for_lens().is_stub


@pytest.mark.parametrize(
    "missing", ["name", "prompt_markdown", "status", "tool_allowlist"]
)
def test_explicit_role_refuses_missing_authority(loader, monkeypatch, missing):
    data = response()
    del data["snapshot"][missing]
    monkeypatch.setattr(loader, "_neotoma", lambda *a: data)
    assert loader.load_for_lens().is_stub


@pytest.mark.parametrize(
    "tools", [["Read"], "Read", "*", ["*"], '["Bash(git status:*)", "Read"]']
)
@pytest.mark.parametrize("status", ["active", "planned"])
def test_explicit_role_preserves_declared_tools_and_status(
    loader, monkeypatch, tools, status
):
    data = response()
    data["snapshot"].update(status=status, tool_allowlist=tools)
    monkeypatch.setattr(loader, "_neotoma", lambda *a: data)
    definition = loader.load_for_lens()
    assert not definition.is_stub
    assert definition.tool_allowlist == tools
    assert definition.status == status
    validate_lens_definition(definition, "pavo")
    definition.tool_allowlist = ["*"] if tools != ["*"] else ["Read"]
    with pytest.raises(ValueError, match="absent or changed"):
        validate_lens_definition(definition, "pavo")


def search_response():
    row = response()
    return {
        "entities": [row],
        "total": 1,
        "read_contract": {
            "mode": {"actual": "structured"},
            "applied_scope": {
                "entity_types": ["agent_definition"],
                "predicates": [{"field": "name", "op": "eq"}],
            },
            "coverage": {
                "state": "complete",
                "scope_exhausted": True,
                "returned_count": 1,
                "total": {"relation": "exact", "value": 1},
                "continuation": {"kind": "none"},
            },
        },
    }


@pytest.mark.parametrize(
    "case",
    [
        "complete",
        "duplicate",
        "unknown",
        "continued",
        "missing_scope",
        "wrong_scope",
        "candidate_count",
        "missing_total",
        "boolean_returned",
        "boolean_total",
    ],
)
def test_name_lookup_requires_unique_exhaustive_scope(monkeypatch, case):
    monkeypatch.delenv("PAVO_AGENT_DEFINITION_ID", raising=False)
    data = search_response()
    if case == "duplicate":
        data["entities"].append(copy.deepcopy(data["entities"][0]))
        data["total"] = data["read_contract"]["coverage"]["returned_count"] = 2
        data["read_contract"]["coverage"]["total"]["value"] = 2
    elif case == "unknown":
        data["read_contract"]["coverage"]["state"] = "unknown"
    elif case == "continued":
        data["next_cursor"] = "next"
    elif case == "missing_scope":
        del data["read_contract"]["coverage"]["scope_exhausted"]
    elif case == "wrong_scope":
        data["read_contract"]["applied_scope"]["predicates"] = []
    elif case == "candidate_count":
        data["read_contract"]["coverage"]["total"]["relation"] = "candidate_count"
    elif case == "missing_total":
        del data["total"]
    elif case == "boolean_returned":
        data["read_contract"]["coverage"]["returned_count"] = True
    elif case == "boolean_total":
        data["read_contract"]["coverage"]["total"]["value"] = True
    loader = AgentLoader("pavo")
    seen = []

    def request(method, url, body):
        seen.append(body)
        return data

    monkeypatch.setattr(loader, "_neotoma", request)
    result = loader.load_for_lens()
    assert result.is_stub is (case != "complete")
    assert seen[0]["snapshot_filters"] == {"name": {"op": "eq", "value": "pavo"}}
