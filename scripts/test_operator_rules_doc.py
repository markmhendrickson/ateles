"""Effect tests for the decisions bullet and docs/operator_rules.md."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLAUDE = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
DOC = (REPO / "docs" / "operator_rules.md").read_text(encoding="utf-8")


def _decisions_bullet() -> str:
    for line in CLAUDE.splitlines():
        if line.startswith("- **Put every decision that needs Mark"):
            return line
    raise AssertionError("decisions bullet missing")


def test_decisions_bullet_uses_the_questions_tool():
    bullet = _decisions_bullet()
    assert "End every turn with the decisions that need Mark" not in bullet
    for phrase in (
        "options",
        "what each implies",
        "settled",
        "name alone",
        "until answered",
        "every open decision",
        "recommendation",
        "unchanged",
        "[decisions-unposed]",
        "Do not substitute a numbered list",
    ):
        assert phrase in bullet, phrase
    assert "do not call the tool" in bullet
    assert "do not add a trailer" in bullet
    assert "there are no decisions" in bullet
    assert "Give a full URL for every pull request" not in bullet
    assert "Dispatch a subagent on every pulled email" not in CLAUDE


def test_operator_rules_doc_shape():
    lines = [line for line in DOC.splitlines() if line.strip()]
    assert lines[0].startswith("# ")
    assert lines[1] == "## Scope"
    for token in (
        "[rules-unbound]",
        "[rules-incomplete]",
        "[decisions-unposed]",
        "[skill-sync]",
        "skill_rule_missing",
    ):
        assert token in DOC
    assert "do not paste rule text into prompt_markdown or CLAUDE.md" in DOC
    assert "Zero decisions produce no questions-tool call." in DOC
    assert "Give a full URL for every pull request that needs the operator's approval" not in DOC
    assert "Dispatch a subagent on every pulled email" not in DOC
    assert not any(line.startswith(tuple(f"{n}. " for n in range(10))) for line in DOC.splitlines())
