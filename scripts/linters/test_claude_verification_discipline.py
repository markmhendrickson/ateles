"""Effect-level read-back for CLAUDE.md Verification discipline (ateles#731).

Asserts the shipped session-only section — not that a PR exists. Cross-surface
parity is N/A: the authorized binding surface is CLAUDE.md only; dispatch
surfaces remain ateles#593.

Run with: pytest scripts/linters/test_claude_verification_discipline.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

# Keyword sets per Eng/QA: tolerant of minor rewording, not exact titles.
RULE_KEYWORD_SETS: list[tuple[str, tuple[str, ...]]] = [
    ("mechanism-that-binds", ("mechanism that does not bind", "not a control")),
    ("read-back", ("write that reports success", "read it back")),
    ("validate-instrument", ("validate the instrument", "believing the measurement")),
    ("tests-that-can-fail", ("test that cannot fail", "decoration")),
    ("fail-closed", ("fail closed", "safety meaning")),
    ("extend-generalizing", ("extend the mechanism", "already generalizes")),
]

HONESTY_CUE = re.compile(
    r"\b(?:Enforced|Partial(?:ly)?|Manual|Nothing|not mechanizable|"
    r"not mechanically enforceable|Enforcement)\b",
    re.IGNORECASE,
)

# Hard non-goal: claiming #593 acceptance criteria are satisfied by this prose.
CLAIM_593_SATISFIED = re.compile(
    r"593.{0,40}(satisfied|complete|closed|met)",
    re.IGNORECASE | re.DOTALL,
)


def _claude_text() -> str:
    return CLAUDE_MD.read_text(encoding="utf-8")


def _h2_offsets(text: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(1)) for m in re.finditer(r"(?m)^## (.+)$", text)]


def _verification_section(text: str) -> tuple[int, str]:
    """Return (byte offset of H2, section body through next H2 or EOF)."""
    matches = list(
        re.finditer(
            r"(?m)^(## Verification discipline[^\n]*)\n(.*?)(?=^## |\Z)",
            text,
            re.DOTALL,
        )
    )
    assert matches, "CLAUDE.md missing Verification discipline H2"
    m = matches[0]
    return m.start(1), m.group(1) + "\n" + m.group(2)


def _first_prose_paragraph(section: str) -> str:
    """First non-blank paragraph after the H2 line."""
    lines = section.splitlines()
    assert lines and lines[0].startswith("## ")
    buf: list[str] = []
    started = False
    for line in lines[1:]:
        if not line.strip():
            if started:
                break
            continue
        if line.startswith("#"):
            break
        started = True
        buf.append(line)
    assert buf, "Verification section has no lead paragraph"
    return "\n".join(buf)


def _rule_blocks(section: str) -> list[str]:
    """Split section into top-level bullet blocks starting with '- **'."""
    body = section.split("\n", 1)[1]
    parts = re.split(r"(?m)^(?=- \*\*)", body)
    return [p for p in parts if p.startswith("- **")]


def test_verification_discipline_h2_present_and_labeled_session_only():
    text = _claude_text()
    _offset, heading_and_body = _verification_section(text)
    heading = heading_and_body.splitlines()[0]
    assert "Verification discipline" in heading
    assert "session-only" in heading or "session-only" in heading_and_body.splitlines()[1]


def test_lead_sentence_states_audience_limit():
    _offset, section = _verification_section(_claude_text())
    lead = _first_prose_paragraph(section)
    assert re.search(r"do(?:es)? not bind dispatched", lead, re.IGNORECASE)
    assert re.search(
        r"(#593|D1|D2|D3|ateles\.prompt_markdown)",
        lead,
        re.IGNORECASE,
    )


def test_authorization_cites_731_not_593():
    _offset, section = _verification_section(_claude_text())
    lead = _first_prose_paragraph(section)
    assert re.search(r"#731", lead)
    assert CLAIM_593_SATISFIED.search(section) is None


def test_six_rules_or_explicit_deferral_with_honesty_cues():
    _offset, section = _verification_section(_claude_text())
    lowered = section.lower()
    blocks = _rule_blocks(section)
    for name, keywords in RULE_KEYWORD_SETS:
        present = all(k.lower() in lowered for k in keywords)
        deferred = bool(
            re.search(
                rf"(?i)deferr\w*.{{0,80}}{re.escape(name.split('-')[0])}",
                section,
            )
        )
        assert present or deferred, f"missing rule {name} (and no deferral)"
        if present:
            # Match the rule bullet that contains the first keyword.
            needle = keywords[0].lower()
            matching = [b for b in blocks if needle in b.lower()]
            assert matching, f"no bullet block for {name}"
            assert HONESTY_CUE.search(matching[0]), (
                f"rule {name} missing enforcement-honesty cue"
            )


def test_e1_skip_tests_form_under_standing_or_commit_hygiene():
    text = _claude_text()
    assert "SKIP_TESTS=1" in text
    assert "SKIP_TESTS_REASON=" in text


def test_verification_follows_standing_constraints():
    """#731 places Verification after Standing; #711 places Session before Standing."""
    text = _claude_text()
    offsets = _h2_offsets(text)
    titles = [t for _, t in offsets]
    assert any(t.startswith("Standing constraints") for t in titles)
    assert any("Verification discipline" in t for t in titles)

    standing = next(o for o, t in offsets if t.startswith("Standing constraints"))
    verification = next(o for o, t in offsets if "Verification discipline" in t)
    assert standing < verification, "Verification must follow Standing constraints"

    session = next((o for o, t in offsets if "Session conduct" in t), None)
    if session is not None:
        # #711 Session conduct → Standing; #731 Verification after Standing.
        assert session < standing < verification, (
            "Expected Session conduct → Standing constraints → Verification discipline"
        )


def test_standing_constraints_fork_test_and_config_single_source():
    text = _claude_text()
    standing = re.search(
        r"(?ms)^## Standing constraints\n(.*?)(?=^## |\Z)",
        text,
    )
    assert standing, "Standing constraints H2 missing"
    body = standing.group(1)
    assert re.search(r"fork test", body, re.IGNORECASE)
    assert re.search(r"single source", body, re.IGNORECASE)


def test_negative_no_593_acceptance_claim_in_verification_section():
    _offset, section = _verification_section(_claude_text())
    assert CLAIM_593_SATISFIED.search(section) is None


@pytest.mark.parametrize(
    "mutator,expect_fail_substring",
    [
        (
            lambda t: t.replace("(session-only)", "(interactive)"),
            "session-only",
        ),
        (
            lambda t: t.replace("**Enforcement: Nothing** (manual).", "Apply by hand."),
            "honesty",
        ),
            (
                lambda t: t.replace(
                    "not as a #593 deliverable.",
                    "not as a #593 deliverable. #593 acceptance criteria are satisfied.",
                ),
                "593",
            ),
    ],
)
def test_red_path_mutations_fail_contract_helpers(mutator, expect_fail_substring, tmp_path):
    """Prove the assertions are load-bearing (QA red-path), not decoration.

    Mutates a temp copy and re-runs the core checks against it. The live
    CLAUDE.md is untouched; this documents that each checked property can fail.
    """
    original = _claude_text()
    mutated = mutator(original)
    path = tmp_path / "CLAUDE.md"
    path.write_text(mutated, encoding="utf-8")
    text = path.read_text(encoding="utf-8")

    failed = False
    reason = ""
    try:
        if expect_fail_substring == "session-only":
            _o, section = _verification_section(text)
            heading = section.splitlines()[0]
            assert "session-only" in heading
        elif expect_fail_substring == "honesty":
            _o, section = _verification_section(text)
            blocks = _rule_blocks(section)
            validate = [b for b in blocks if "validate the instrument" in b.lower()]
            assert validate and HONESTY_CUE.search(validate[0])
        else:
            _o, section = _verification_section(text)
            assert CLAIM_593_SATISFIED.search(section) is None
    except AssertionError as exc:
        failed = True
        reason = str(exc)

    assert failed, (
        f"expected mutation for {expect_fail_substring!r} to fail; got pass"
        + (f" ({reason})" if reason else "")
    )
