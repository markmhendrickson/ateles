"""Strict lens admission is separate from generic compatibility defaults."""

import copy
import hashlib
import json

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
            "version": "1",
            "surface": "entity_collection",
            "mode": {"actual": "structured"},
            "applied_scope": {
                "owner": "authenticated",
                "entity_types": ["agent_definition"],
                "include_merged": False,
                "include_deleted": False,
                "include_snapshots": True,
                "pagination": {"kind": "keyset", "limit": 100, "offset": 0},
                "predicates": [
                    {
                        "field": "name",
                        "op": "eq",
                        "value": {
                            "kind": "sha256",
                            "value": hashlib.sha256(
                                json.dumps("pavo").encode()
                            ).hexdigest(),
                        },
                    }
                ],
            },
            "request_options": {
                "applied": [
                    "entity_type",
                    "snapshot_filters",
                    "include_snapshots",
                    "limit",
                ],
                "normalized": [],
                "ignored": [],
            },
            "coverage": {
                "kind": "predicate_population",
                "state": "complete",
                "reasons": [],
                "page_exhausted": True,
                "scope_exhausted": True,
                "returned_count": 1,
                "total": {"relation": "exact", "value": 1, "unit": "entities"},
                "continuation": {"kind": "none"},
            },
        },
    }


def test_mutating_parsed_tool_array_cannot_rewrite_source_authority(
    loader, monkeypatch
):
    data = response()
    monkeypatch.setattr(loader, "_neotoma", lambda *args: data)
    definition = loader.load_for_lens()
    definition.tool_allowlist.append("*")
    assert definition.raw["snapshot"]["tool_allowlist"] == ["Read"]
    with pytest.raises(ValueError, match="absent or changed"):
        validate_lens_definition(definition, "pavo")


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


@pytest.mark.parametrize(
    "case",
    [
        "wrong_row_type",
        "missing_row_type",
        "extra_predicate",
        "wrong_value",
        "missing_value",
        "wrong_value_kind",
        "merged_scope",
        "deleted_scope",
        "missing_snapshots",
        "nonzero_offset",
        "ignored_scope_option",
    ],
)
def test_name_lookup_refuses_unbound_scope_and_row_type(monkeypatch, case):
    monkeypatch.delenv("PAVO_AGENT_DEFINITION_ID", raising=False)
    data = search_response()
    scope = data["read_contract"]["applied_scope"]
    if case == "wrong_row_type":
        data["entities"][0]["entity_type"] = "task"
    elif case == "missing_row_type":
        del data["entities"][0]["entity_type"]
    elif case == "extra_predicate":
        scope["predicates"].append(
            {
                "field": "status",
                "op": "eq",
                "value": {"kind": "sha256", "value": "other"},
            }
        )
    elif case == "wrong_value":
        scope["predicates"][0]["value"]["value"] = hashlib.sha256(
            json.dumps("other").encode()
        ).hexdigest()
    elif case == "missing_value":
        del scope["predicates"][0]["value"]
    elif case == "wrong_value_kind":
        scope["predicates"][0]["value"]["kind"] = "literal"
    elif case == "merged_scope":
        scope["include_merged"] = True
    elif case == "deleted_scope":
        scope["include_deleted"] = True
    elif case == "missing_snapshots":
        scope["include_snapshots"] = False
    elif case == "nonzero_offset":
        scope["pagination"]["offset"] = 100
    elif case == "ignored_scope_option":
        data["read_contract"]["request_options"]["ignored"] = [
            {"name": "snapshot_filters", "reason": "unsupported_on_surface"}
        ]
    loader = AgentLoader("pavo")
    monkeypatch.setattr(loader, "_neotoma", lambda *args: data)
    assert loader.load_for_lens().is_stub
