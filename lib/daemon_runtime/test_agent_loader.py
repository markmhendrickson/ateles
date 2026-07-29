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


def test_json_array_string_shape():
    """Neotoma returns tool_allowlist as a JSON-array STRING, not a parsed list.

    A naive comma-split keeps the surrounding brackets/quotes on each token
    ('["a"', '"b"', '"c"]'), which the CLI rejects as malformed --allowedTools
    rules and fails the whole dispatch. This was a live swarm outage: the
    Bash(...:*) grammar makes the rejection fatal rather than silently ignored.
    """
    assert _tools('["a", "b", "c"]') == ["a", "b", "c"]


def test_json_array_string_preserves_parenthesized_bash_grants():
    """The exact production shape: parenthesized Bash command-scope grants must
    survive JSON parsing intact, not arrive wrapped in literal quotes."""
    raw = '["Bash", "Bash(gh pr:*)", "Bash(gh issue:*)", "Bash(git:*)", "Read"]'
    assert _tools(raw) == [
        "Bash",
        "Bash(gh pr:*)",
        "Bash(gh issue:*)",
        "Bash(git:*)",
        "Read",
    ]


def test_json_array_string_with_blanks():
    assert _tools('["a", "", "  ", "b"]') == ["a", "b"]


def test_bracketed_non_json_falls_back_to_comma_split():
    """A bracketed string that isn't valid JSON must not crash; it falls back
    to the legacy comma-split rather than raising."""
    assert _tools("[a, b, c]") == ["[a", "b", "c]"]


def test_wildcard_string():
    assert _tools("*") == ["*"]
    assert _tools("  *  ") == ["*"]


def test_empty_and_none_default_to_wildcard():
    assert _tools("") == ["*"]
    assert _tools(None) == ["*"]
    assert _tools([]) == ["*"]


def test_default_is_wildcard():
    assert AgentDefinition(name="t").tools == ["*"]
