"""Contract tests for the shared action-capability normalizer.

These are intentionally about action classes, not policy decisions.  The raw
tool id remains available for bounded audit while injection and enforcement
consume one normalized vocabulary across current and legacy harness aliases.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from action_capabilities import classify_tool_call  # noqa: E402


@pytest.mark.parametrize(
    ("tool_name", "expected_canonical"),
    [
        ("Bash", "shell"),
        ("exec_command", "shell"),
        ("functions.exec_command", "shell"),
    ],
)
def test_shell_aliases_share_one_capability(tool_name, expected_canonical):
    result = classify_tool_call(tool_name, {"command": "env", "cmd": "env"})
    assert result.raw_tool_id == tool_name
    assert result.canonical_tool_id == expected_canonical
    assert "credential_read_candidate" in result.action_classes


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__neotoma__store",
        "mcp__mcpsrv_neotoma__store",
        "mcp__neotoma__store_structured",
        "mcp__mcpsrv_neotoma__correct",
        "mcp__neotoma__create_relationship",
        "mcp__neotoma__delete_entity",
    ],
)
def test_current_and_legacy_neotoma_mutations_share_one_class(tool_name):
    result = classify_tool_call(
        tool_name,
        {"entities": [{"entity_type": "task"}], "entity_type": "task"},
    )
    assert result.raw_tool_id == tool_name
    assert "neotoma_mutation" in result.action_classes


def test_rendered_page_publication_is_a_publication_effect():
    result = classify_tool_call(
        "mcp__neotoma__publish_rendered_page",
        {
            "entity_id": "ent_synthetic_page",
            "idempotency_key": "publish-synthetic-page",
        },
    )
    assert result.raw_tool_id == "mcp__neotoma__publish_rendered_page"
    assert "publication" in result.action_classes
    assert "rendered_page_publication" in result.action_classes


@pytest.mark.parametrize(
    "tool_name",
    ["mcp__cua_repl__js", "computer"],
)
def test_browser_ui_tools_are_classified_as_effect_capable(tool_name):
    result = classify_tool_call(tool_name, {"code": "await tab.click({x: 1, y: 2})"})
    assert result.raw_tool_id == tool_name
    assert "browser_ui_effect" in result.action_classes


@pytest.mark.parametrize("event", ["Stop", "SubagentStop"])
def test_termination_events_share_one_class(event):
    result = classify_tool_call("", {}, hook_event_name=event)
    assert result.raw_tool_id == ""
    assert result.canonical_tool_id == "termination"
    assert "termination" in result.action_classes


def test_grant_and_policy_write_classes_are_preserved_for_injection():
    result = classify_tool_call(
        "mcp__neotoma__store",
        {
            "entities": [
                {"entity_type": "agent_grant"},
                {"entity_type": "agent_policy"},
            ]
        },
    )
    assert result.entity_types == ("agent_grant", "agent_policy")
    assert "grant_write" in result.action_classes
    assert "policy_write" in result.action_classes


def test_harness_config_and_advisory_existing_categories_survive_extraction():
    config = classify_tool_call(
        "exec_command", {"cmd": "printf '{}' > .claude/settings.json"}
    )
    advisory = classify_tool_call(
        "Bash", {"command": "gh api /repos/acme/widget/security-advisories"}
    )
    assert "harness_config" in config.action_classes
    assert "advisory" in advisory.action_classes
