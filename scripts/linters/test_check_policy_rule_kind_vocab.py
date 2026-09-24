"""Tests for the agent_policy rule_kind vocabulary guard (ateles#1245).

RED-BEFORE-GREEN EVIDENCE (principle 4): before this linter existed, an
`agent_policy` row with `rule_kind: "recommended"` (or absent) synced into the
committed snapshot cleanly — nothing failed. `test_refuses_out_of_vocabulary_value`
and `test_refuses_absent_rule_kind` are that red case, now pinned green by
`find_violations`. Reverting `check_policy_rule_kind_vocab.is_out_of_vocabulary`
to `return False` (or deleting the call in `find_violations`) makes both fail —
that is the regression they guard, not just "throws on bad input".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import check_policy_rule_kind_vocab as guard  # noqa: E402


def _write_snapshot(tmp_path, policies):
    p = tmp_path / "snapshot.json"
    p.write_text('{"policies": ' + __import__("json").dumps(policies) + "}")
    return p


def test_accepts_mandatory(tmp_path):
    p = _write_snapshot(tmp_path, [{"entity_id": "e1", "rule_kind": "mandatory"}])
    rows = guard.load_snapshot(p)
    assert guard.find_violations(rows) == []


def test_accepts_advisory(tmp_path):
    p = _write_snapshot(tmp_path, [{"entity_id": "e1", "rule_kind": "advisory"}])
    rows = guard.load_snapshot(p)
    assert guard.find_violations(rows) == []


def test_refuses_out_of_vocabulary_value(tmp_path):
    p = _write_snapshot(tmp_path, [{"entity_id": "e1", "rule_kind": "recommended"}])
    rows = guard.load_snapshot(p)
    violations = guard.find_violations(rows)
    assert len(violations) == 1
    msg = guard.refusal_message(violations[0]["rule_kind"])
    assert '"recommended"' in msg
    assert "{mandatory, advisory}" in msg
    assert "safety meaning" in msg
    assert 'Fix: set rule_kind to "mandatory" or "advisory"' in msg


def test_refuses_operating_discipline_value(tmp_path):
    p = _write_snapshot(
        tmp_path, [{"entity_id": "e1", "rule_kind": "operating_discipline"}]
    )
    rows = guard.load_snapshot(p)
    violations = guard.find_violations(rows)
    assert len(violations) == 1
    assert violations[0]["rule_kind"] == "operating_discipline"


def test_refuses_absent_rule_kind(tmp_path):
    p = _write_snapshot(tmp_path, [{"entity_id": "e1", "rule_kind": None}])
    rows = guard.load_snapshot(p)
    violations = guard.find_violations(rows)
    assert len(violations) == 1
    msg = guard.refusal_message(violations[0]["rule_kind"])
    assert "(absent)" in msg
    assert '"None"' not in msg
    assert "null" not in msg
    assert "undefined" not in msg


def test_refuses_missing_rule_kind_field(tmp_path):
    # No key at all, not even a null value.
    p = _write_snapshot(tmp_path, [{"entity_id": "e1"}])
    rows = guard.load_snapshot(p)
    violations = guard.find_violations(rows)
    assert len(violations) == 1
    assert "(absent)" in guard.refusal_message(violations[0].get("rule_kind"))


def test_refuses_empty_string_distinctly_from_absent(tmp_path):
    p = _write_snapshot(tmp_path, [{"entity_id": "e1", "rule_kind": ""}])
    rows = guard.load_snapshot(p)
    violations = guard.find_violations(rows)
    assert len(violations) == 1
    # Empty string IS displayed as (absent) per rejected_display's contract
    # (falsy string), which is deliberate: an empty string carries no more
    # information than an absent field for a caller reading the message.
    assert guard.rejected_display("") == "(absent)"


def test_case_sensitive_vocabulary(tmp_path):
    p = _write_snapshot(tmp_path, [{"entity_id": "e1", "rule_kind": "MANDATORY"}])
    rows = guard.load_snapshot(p)
    violations = guard.find_violations(rows)
    assert len(violations) == 1
    assert '"MANDATORY"' in guard.refusal_message(violations[0]["rule_kind"])


def test_main_exits_nonzero_on_violation(tmp_path, capsys):
    p = _write_snapshot(tmp_path, [{"entity_id": "e1", "rule_kind": "allow"}])
    rc = guard.main(["--snapshot", str(p)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "e1" in captured.err


def test_main_exits_zero_when_all_in_vocabulary(tmp_path):
    p = _write_snapshot(
        tmp_path,
        [
            {"entity_id": "e1", "rule_kind": "mandatory"},
            {"entity_id": "e2", "rule_kind": "advisory"},
        ],
    )
    rc = guard.main(["--snapshot", str(p)])
    assert rc == 0


def test_missing_snapshot_exits_2(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    try:
        guard.load_snapshot(missing)
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert exc.code == 2
