"""Tests for the agent_policy rule_kind vocabulary guard (ateles#1245).

RED-BEFORE-GREEN EVIDENCE (principle 4): before this linter existed, an
`agent_policy` row with `rule_kind: "recommended"` (or absent) synced into the
committed snapshot cleanly — nothing failed. `test_refuses_out_of_vocabulary_value`
and `test_refuses_absent_rule_kind` are that red case, now pinned green by
`find_violations`. Reverting `check_policy_rule_kind_vocab.is_out_of_vocabulary`
to `return False` (or deleting the call in `find_violations`) makes both fail —
that is the regression they guard, not just "throws on bad input".
"""

import json
import sys
import urllib.request
from pathlib import Path
from unittest.mock import patch

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


# --- _screen_row_for_operator_pii: one positive case per pattern, a clean-row
# negative, and a case proving entity_id is deliberately exempt (qa lens,
# PR #1246 round 1: zero of the 12 pre-existing tests touched this screen).


def test_pii_screen_refuses_bitcoin_address():
    row = {"entity_id": "e1", "scope": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"}
    try:
        guard._screen_row_for_operator_pii(row)
        assert False, "expected OperatorPIIRefusal"
    except guard.OperatorPIIRefusal as exc:
        assert "bitcoin address" in str(exc)
        assert "e1" in str(exc)


def test_pii_screen_refuses_iban():
    # ES00 0000 ... is a placeholder shape (.gitleaks.toml allowlists all-zero
    # IBANs as documentation placeholders); it still matches the guard's own
    # IBAN pattern, which only checks shape, not check-digit validity.
    row = {"entity_id": "e1", "scope": "note: pay to ES00 0000 0000 0000 0000 00"}
    try:
        guard._screen_row_for_operator_pii(row)
        assert False, "expected OperatorPIIRefusal"
    except guard.OperatorPIIRefusal as exc:
        assert "IBAN" in str(exc)


def test_pii_screen_refuses_phone_number():
    # +1 555... is the standard fictional-number exchange (.gitleaks.toml
    # allowlists it as a placeholder); it still matches the guard's own phone
    # pattern, which only checks shape.
    row = {"entity_id": "e1", "scope": "call +1 555 0132 to confirm"}
    try:
        guard._screen_row_for_operator_pii(row)
        assert False, "expected OperatorPIIRefusal"
    except guard.OperatorPIIRefusal as exc:
        assert "phone number" in str(exc)


def test_pii_screen_refuses_currency_figure():
    row = {"entity_id": "e1", "scope": "budget cap $4,500 for this quarter"}
    try:
        guard._screen_row_for_operator_pii(row)
        assert False, "expected OperatorPIIRefusal"
    except guard.OperatorPIIRefusal as exc:
        assert "currency figure" in str(exc)


def test_pii_screen_passes_clean_row():
    row = {
        "entity_id": "ent_abc123",
        "rule_kind": "mandatory",
        "scope": "global",
        "status": "active",
    }
    # Must not raise.
    guard._screen_row_for_operator_pii(row)


def test_pii_screen_exempts_entity_id_field():
    # entity_id is Neotoma's own identifier, not operator PII — deliberately
    # excluded from the screen so a currency- or phone-shaped entity_id (none
    # currently exist, but the field is opaque) never blocks a legitimate
    # write. Every OTHER field with the same shape still refuses.
    row = {"entity_id": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"}
    # Must not raise: entity_id is exempt.
    guard._screen_row_for_operator_pii(row)


# --- fetch_live: proves the PII screen is wired into the write path, not
# just a standalone function (qa lens: "most missing" finding).


def test_fetch_live_screens_before_returning():
    payload = {
        "entities": [
            {
                "entity_id": "e1",
                "snapshot": {
                    "rule_kind": "mandatory",
                    "scope": "call +1 555 0132 to confirm",
                    "status": "active",
                },
            }
        ]
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode()

    with (
        patch.object(urllib.request, "urlopen", return_value=_FakeResponse()),
        patch.dict(
            "os.environ",
            {
                "NEOTOMA_BEARER_TOKEN": "test-token",
                "NEOTOMA_BASE_URL": "https://neotoma.example",
            },
        ),
    ):
        try:
            guard.fetch_live()
            assert False, "expected OperatorPIIRefusal"
        except guard.OperatorPIIRefusal as exc:
            assert "phone number" in str(exc)
            assert "e1" in str(exc)


def test_fetch_live_returns_narrowed_clean_rows():
    payload = {
        "entities": [
            {
                "entity_id": "e1",
                "snapshot": {
                    "rule_kind": "mandatory",
                    "scope": "global",
                    "status": "active",
                    "title": "internal-only free text, never returned",
                },
            }
        ]
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode()

    with (
        patch.object(urllib.request, "urlopen", return_value=_FakeResponse()),
        patch.dict(
            "os.environ",
            {
                "NEOTOMA_BEARER_TOKEN": "test-token",
                "NEOTOMA_BASE_URL": "https://neotoma.example",
            },
        ),
    ):
        rows = guard.fetch_live()

    assert rows == [
        {
            "entity_id": "e1",
            "rule_kind": "mandatory",
            "scope": "global",
            "status": "active",
        }
    ]
