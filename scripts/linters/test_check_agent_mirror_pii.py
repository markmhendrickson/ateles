"""
Unit tests for check_agent_mirror_pii.py (ateles#1098 / #1100).

Follows the direct-import, pure-function pattern established by its nearest
siblings (test_validate_tool_allowlist.py, test_check_neotoma_rest_paths.py):
import the linter module directly, assert on its pure check functions, use
tmp_path fixtures for file-scope tests. No live Neotoma calls — the semantic
fetch is monkeypatched at the render_agent_docs boundary it reuses.

Constants are synthetic, never real operator payload — see
docs/audits/standing_rule_pii_audit_2026-09-18.md for why that discipline
matters here specifically.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_agent_mirror_pii as lint  # noqa: E402

SYNTHETIC_BTC = "bc1q" + ("a" * 39)
SYNTHETIC_SHORT_NAME = "Ari"  # deliberately < 4 chars to probe substring collision
SYNTHETIC_ENTITY = "ent_" + ("a" * 24)


def test_btc_address_structural_hit_is_flagged(tmp_path) -> None:
    text = f"resolve the payment via {SYNTHETIC_BTC} for this vendor\n"
    findings = lint._check_structural(tmp_path / "SKILL.md", text)
    assert len(findings) == 1
    assert "BTC address literal" in findings[0]


def test_generic_instruction_with_no_literal_passes(tmp_path) -> None:
    text = "resolve the payment amount from the matching payment_profile entity\n"
    findings = lint._check_structural(tmp_path / "SKILL.md", text)
    assert findings == []


def test_word_boundary_prevents_substring_false_positive(tmp_path) -> None:
    """A short contact name must not match as a substring inside an unrelated
    word (the defect this PR's commit message names: a name matching inside
    '...DIARIZE'). Proved red first: a bare substring pattern (the
    pre-fix approach) DOES false-positive on this fixture, confirming the
    defect is real; then the shipped _check_semantic (word-boundary) does
    NOT flag it, confirming the fix (docs/foundation/principles.md#4)."""
    text = "call RECORD_MEETING_DIARIZE to capture the session\n"

    # Red: a naive substring match (the pre-fix approach) DOES false-positive.
    naive_pattern = re.compile(re.escape(SYNTHETIC_SHORT_NAME.lower()))
    assert naive_pattern.search(text.lower()) is not None, (
        "fixture must reproduce the substring collision the word-boundary "
        "fix was written to prevent"
    )

    # Green: the shipped word-boundary check does not flag it.
    findings = lint._check_semantic(
        tmp_path / "SKILL.md",
        text,
        [(SYNTHETIC_SHORT_NAME, f"contact:name:{SYNTHETIC_ENTITY}")],
    )
    assert findings == []


def test_whole_word_match_is_still_caught(tmp_path) -> None:
    text = f"pay {SYNTHETIC_SHORT_NAME} directly via wire transfer\n"
    findings = lint._check_semantic(
        tmp_path / "SKILL.md",
        text,
        [(SYNTHETIC_SHORT_NAME, f"contact:name:{SYNTHETIC_ENTITY}")],
    )
    assert len(findings) == 1
    assert SYNTHETIC_ENTITY in findings[0]


def test_neotoma_unreachable_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    assert lint._fetch_sensitive_values() is None


def test_allow_unverified_flag_explicitly_skips(monkeypatch, tmp_path, capsys) -> None:
    # A clean planted fixture, passed explicitly as argv, isolates this test
    # from the live repo's default glob scope (which may carry unrelated
    # real findings, e.g. the pre-existing loop-start/SKILL.md case tracked
    # as ateles#1099 — not this test's concern).
    clean_file = tmp_path / "SKILL.md"
    clean_file.write_text("resolve the amount from the matching payment_profile\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_agent_mirror_pii.py", str(clean_file), "--allow-unverified"],
    )
    rc = lint.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIPPED (--allow-unverified)" in out


def test_neotoma_unreachable_without_allow_unverified_exits_nonzero(
    monkeypatch, tmp_path
) -> None:
    clean_file = tmp_path / "SKILL.md"
    clean_file.write_text("resolve the amount from the matching payment_profile\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["check_agent_mirror_pii.py", str(clean_file)])
    assert lint.main() == 1


def test_suppression_marker_honored(tmp_path) -> None:
    text = f"{SYNTHETIC_BTC} <!-- {lint.SUPPRESS}: worked example, not real -->\n"
    findings = lint._check_structural(tmp_path / "SKILL.md", text)
    assert findings == []


def test_suppression_marker_honored_for_semantic_hit(tmp_path) -> None:
    text = f"pay {SYNTHETIC_SHORT_NAME} <!-- {lint.SUPPRESS}: worked example -->\n"
    findings = lint._check_semantic(
        tmp_path / "SKILL.md",
        text,
        [(SYNTHETIC_SHORT_NAME, f"contact:name:{SYNTHETIC_ENTITY}")],
    )
    assert findings == []
