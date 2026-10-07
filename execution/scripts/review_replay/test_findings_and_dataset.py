"""Findings parser and run-unit grouping."""

from __future__ import annotations

from review_replay.dataset import build_units
from review_replay.findings import parse_findings, verdict_token

VERDICT = """<!-- review:security commit=%s -->
**\U0001f916 Falco - Ateles swarm, security review**
**BLOCKED**

[BLOCKING] fail-open: `execution/scripts/secrets_keys.py:107` swallows a mismatch
- [BLOCKING] gate: `lib/gate.py` lines 40-52 accepts any readable verdict
1. **[BLOCKING]** scope: nothing names a file here
Advisory: this line mentions [BLOCKING] only mid-line and is not a finding
"""


def test_parse_findings_reads_file_line_and_category():
    found = parse_findings(VERDICT % ("a" * 40))
    assert [f.file for f in found] == [
        "execution/scripts/secrets_keys.py",
        "lib/gate.py",
        None,
    ]
    assert [f.line for f in found] == [107, 40, None]
    assert found[1].line_end == 52
    assert [f.category for f in found] == ["fail-open", "gate", "scope"]


def test_parse_findings_ignores_midline_marker_and_empty_text():
    assert parse_findings("") == []
    assert len(parse_findings(VERDICT % ("a" * 40))) == 3


def test_verdict_token_prefers_reader_then_line_three():
    assert verdict_token("x\ny\n**BLOCKED**\n", "blocked") == "blocked"
    assert verdict_token("x\ny\n**SIGNED_OFF**\n", None) == "SIGNED_OFF"
    assert verdict_token("nothing here", None) is None


def _rec(case_id, kind, lens, **extra):
    base = {
        "case_id": case_id,
        "kind": kind,
        "lens": lens,
        "repo": "ateles",
        "pr": 1,
        "head_sha": "h" * 40,
        "base_sha": "b" * 40,
    }
    base.update(extra)
    return base


def test_findings_on_one_head_and_lens_share_one_run():
    records = [
        _rec("finding-1", "unknown_blocker", "qa"),
        _rec("finding-2", "confirmed_blocker", "qa"),
        _rec("finding-3", "confirmed_blocker", "qa"),
        _rec("finding-4", "confirmed_blocker", "pm"),
    ]
    units = build_units(records)
    assert [(u.case_id, u.lens, len(u.labels)) for u in units] == [
        ("finding-2", "qa", 3),
        ("finding-4", "pm", 1),
    ]


def test_all_lens_heads_fan_out_and_probes_use_security_and_arch():
    records = [
        _rec("clean-001", "clean", "all"),
        _rec("planted-01", "planted", "security/arch", patch="planted/P01.patch"),
        _rec("injection-01", "injection", "all", patch="probes/i1.patch"),
    ]
    units = {(u.case_id, u.lens) for u in build_units(records)}
    assert {ln for c, ln in units if c == "clean-001"} == {
        "pm",
        "qa",
        "ux",
        "arch",
        "security",
    }
    assert {ln for c, ln in units if c == "planted-01"} == {"security", "arch"}
    assert {ln for c, ln in units if c == "injection-01"} == {"security", "arch"}


def test_filters_apply_and_unknown_only_heads_are_excluded_by_default():
    records = [
        _rec("finding-1", "unknown_blocker", "qa"),
        _rec("clean-001", "clean", "all"),
        _rec("n-1", "clean", "all", repo="neotoma"),
    ]
    units = build_units(records, lenses=("qa", "pm"), repos=("ateles",))
    assert {(u.case_id, u.lens) for u in units} == {
        ("clean-001", "qa"),
        ("clean-001", "pm"),
    }
