"""Tests for AgentDefinition.tools parsing.

Regression coverage for the tool_allowlist shape mismatch: agent_definition
entities store tool_allowlist as a JSON array, but the loader historically only
handled a comma-separated string (.split(",")), which mangled array values.
The .tools property must accept array, comma-string, and wildcard shapes.
"""

from agent_loader import AgentDefinition


def _tools(value):
    return AgentDefinition(name="t", tool_allowlist=value).tools


def test_array_shape_canonical_storage():
    assert _tools(["a", "b", "c"]) == ["a", "b", "c"]


def test_array_with_whitespace_and_blanks():
    assert _tools([" a ", "", "  ", "b"]) == ["a", "b"]


def test_comma_string_legacy_shape():
    assert _tools("a, b ,c") == ["a", "b", "c"]


def test_wildcard_string():
    assert _tools("*") == ["*"]
    assert _tools("  *  ") == ["*"]


def test_empty_and_none_default_to_wildcard():
    assert _tools("") == ["*"]
    assert _tools(None) == ["*"]
    assert _tools([]) == ["*"]


def test_default_is_wildcard():
    assert AgentDefinition(name="t").tools == ["*"]
