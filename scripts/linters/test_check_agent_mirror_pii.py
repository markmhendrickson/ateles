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
    assert "crypto_address" in findings[0]
    assert SYNTHETIC_BTC not in findings[0]
    assert SYNTHETIC_BTC[:10] not in findings[0]


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
    assert "skipped (--allow-unverified)" in out


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


def test_semantic_hit_is_not_suppressible(tmp_path) -> None:
    """contact_field and payment_profile_field hits are NEVER suppressible —
    only a structural crypto_address hit may carry the suppression marker
    (ateles#1100 pm finding 3)."""
    text = f"pay {SYNTHETIC_SHORT_NAME} <!-- {lint.SUPPRESS}: worked example -->\n"
    findings = lint._check_semantic(
        tmp_path / "SKILL.md",
        text,
        [(SYNTHETIC_SHORT_NAME, f"contact:name:{SYNTHETIC_ENTITY}")],
    )
    assert len(findings) == 1
    assert "contact_field" in findings[0]
    assert "not suppressible" in findings[0]


def test_bare_suppression_marker_does_not_suppress(tmp_path) -> None:
    """A bare marker with no `: <reason>` at all must not suppress
    (ateles#1100 pm finding 3: reason is required)."""
    text = f"{SYNTHETIC_BTC} <!-- {lint.SUPPRESS} -->\n"
    findings = lint._check_structural(tmp_path / "SKILL.md", text)
    assert len(findings) == 1


def test_empty_reason_suppression_does_not_suppress(tmp_path) -> None:
    """`<!-- agent-mirror-payload-ok: -->` with an empty reason must not
    suppress (ateles#1100 pm finding 3)."""
    text = f"{SYNTHETIC_BTC} <!-- {lint.SUPPRESS}: -->\n"
    findings = lint._check_structural(tmp_path / "SKILL.md", text)
    assert len(findings) == 1


def test_suppression_with_nonempty_reason_still_suppresses_structural(tmp_path) -> None:
    text = f"{SYNTHETIC_BTC} <!-- {lint.SUPPRESS}: documented worked example -->\n"
    findings = lint._check_structural(tmp_path / "SKILL.md", text)
    assert findings == []


def test_content_hit_uses_failed_content_header(monkeypatch, tmp_path) -> None:
    """A crypto-address content hit prints the differentiated FAILED —
    content header, not one undifferentiated FAILED (ateles#1100 pm
    finding 2)."""
    hit_file = tmp_path / "SKILL.md"
    hit_file.write_text(f"resolve via {SYNTHETIC_BTC}\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_agent_mirror_pii.py", str(hit_file), "--allow-unverified"],
    )
    assert lint.main() == 1


def test_unverified_header_differs_from_content_header(
    monkeypatch, tmp_path, capsys
) -> None:
    clean_file = tmp_path / "SKILL.md"
    clean_file.write_text("resolve the amount from the matching payment_profile\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["check_agent_mirror_pii.py", str(clean_file)])
    rc = lint.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAILED — unverified" in out
    assert "FAILED — content" not in out


def test_help_flag_prints_usage_and_exits_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["check_agent_mirror_pii.py", "--help"])
    rc = lint.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "usage:" in out
    assert "--allow-unverified" in out


def test_short_help_flag_prints_usage_and_exits_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["check_agent_mirror_pii.py", "-h"])
    rc = lint.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "usage:" in out


def test_unknown_flag_exits_nonzero_with_usage_on_stderr(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["check_agent_mirror_pii.py", "--not-a-real-flag"])
    rc = lint.main()
    captured = capsys.readouterr()
    assert rc != 0
    assert "usage:" in captured.err
    assert "unknown flag" in captured.err.lower()
