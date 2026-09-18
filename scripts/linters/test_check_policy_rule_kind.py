#!/usr/bin/env python3
"""Tests for check_policy_rule_kind.py.

The load-bearing test is `test_red_on_the_real_pre_ruling_record`: it holds the
`agent_policy` record EXACTLY as it stood before the 2026-09-18 retirement
writes, and asserts the check goes red on it. A test written only against the
post-fix state would ratify nothing — it would pass against a check that never
fires. See CLAUDE.md, "A test that cannot fail on the thing it watches is
decoration."
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

LINTER = Path(__file__).resolve().parent / "check_policy_rule_kind.py"

sys.path.insert(0, str(LINTER.parent))
from check_policy_rule_kind import (  # noqa: E402
    STEM_MAX_CHARS,
    OperatorPIIRefusal,
    _PII_PATTERNS,
    _screen_row_for_operator_pii,
    find_contradictions,
    find_reversed_supersedes,
    is_active,
    subject_key,
)


def row(eid, title, kind, status="active", scope="global", supersedes=""):
    return {
        "entity_id": eid,
        "canonical_name": f"agent_policy:{title}",
        "title": title,
        "scope": scope,
        "rule_kind": kind,
        "status": status,
        "supersedes": supersedes,
    }


# The record as it actually stood before the operator's ruling was implemented.
PRE_RULING = [
    row(
        "ent_7dbf",
        "Agent Background Execution Rules [strategy_governance]",
        "mandatory",
    ),
    row("ent_00fa", "Agent Background Execution Rules [governance]", "recommended"),
    row(
        "ent_c2da", "Global Agent Rules for Neotoma [strategy_governance]", "mandatory"
    ),
    row("ent_d8bc", "Global Agent Rules for Neotoma [governance]", "recommended"),
    row(
        "ent_fdc7",
        "Risk Classification for Neotoma Changes [governance]",
        "recommended",
    ),
    row(
        "ent_a3df",
        "Risk Classification for Neotoma Changes [strategy_governance]",
        "recommended",
    ),
    row(
        "ent_aa8e",
        "Human Review Checklist for Neotoma Changes [strategy_governance]",
        "recommended",
    ),
    row(
        "ent_6d91",
        "Human Review Checklist for Neotoma Changes [governance]",
        "recommended",
    ),
]


def test_red_on_the_real_pre_ruling_record():
    """THE regression proof: the check fires on the contradiction it exists for."""
    findings = find_contradictions(PRE_RULING)
    subjects = {f["subject"] for f in findings}
    assert subjects == {
        "agent background execution rules",
        "global agent rules for neotoma",
    }
    for f in findings:
        assert f["kinds"] == ["mandatory", "recommended"]


def test_green_after_mandatory_survives():
    """Retiring the `recommended` twin clears it — the operator's ruling."""
    after = []
    for r in PRE_RULING:
        r = dict(r)
        if r["entity_id"] in {"ent_00fa", "ent_d8bc"}:
            r["status"] = "retired"
        after.append(r)
    assert find_contradictions(after) == []


def test_agreeing_duplicates_are_not_flagged():
    """Two rows agreeing on rule_kind are a duplicate, not a safety question."""
    pair = [
        row("a", "Risk Classification [governance]", "recommended"),
        row("b", "Risk Classification [strategy_governance]", "recommended"),
    ]
    assert find_contradictions(pair) == []


def test_retired_rows_do_not_bind():
    pair = [
        row("a", "Some Rule [governance]", "mandatory"),
        row("b", "Some Rule [strategy_governance]", "recommended", status="retired"),
    ]
    assert find_contradictions(pair) == []


def test_different_scopes_are_not_compared():
    """A narrower scope may legitimately refine a broader one."""
    pair = [
        row("a", "Some Rule", "mandatory", scope="global"),
        row("b", "Some Rule", "recommended", scope="swarm"),
    ]
    assert find_contradictions(pair) == []


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Agent Rules [governance]", "agent rules"),
        ("Agent Rules [strategy_governance]", "agent rules"),
        ("  Agent   Rules  ", "agent rules"),
        ("Agent Rules", "agent rules"),
    ],
)
def test_subject_key_strips_bracketed_qualifier(title, expected):
    assert subject_key(row("x", title, "mandatory")) == expected


def test_subject_key_falls_back_to_canonical_name():
    r = {"canonical_name": "agent_policy:Fallback Rule [governance]", "title": None}
    assert subject_key(r) == "fallback rule"


def test_is_active_is_case_and_whitespace_tolerant():
    assert is_active({"status": " Active "})
    assert not is_active({"status": "retired"})
    assert not is_active({"status": None})


def test_committed_snapshot_is_clean():
    """The real committed snapshot must pass — this is the CI-visible check."""
    proc = subprocess.run([sys.executable, str(LINTER)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_exits_1_on_a_contradicting_snapshot(tmp_path):
    """End-to-end: the CLI, not just the function, goes red."""
    snap = tmp_path / "s.json"
    snap.write_text(json.dumps({"policies": PRE_RULING}))
    proc = subprocess.run(
        [sys.executable, str(LINTER), "--snapshot", str(snap)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "rule_kind contradiction" in proc.stdout


# --- reversed `supersedes` (the structural signal) ---------------------------

# The edge direction as the FIRST implementation of this ruling actually wrote
# it: `supersedes` on the retired row pointing at the survivor. data_model.md
# defines the edge as source-replaces-target, so this asserts the retired
# `recommended` rule replaced the `mandatory` one that beat it.
INVERTED_EDGES = [
    row("ent_7dbf", "Agent Background Execution Rules [sg]", "mandatory"),
    row(
        "ent_00fa",
        "Agent Background Execution Rules [governance]",
        "recommended",
        status="retired",
        supersedes="ent_7dbf",
    ),
    row("ent_aa8e", "Human Review Checklist [sg]", "recommended"),
    row(
        "ent_6d91",
        "Human Review Checklist [governance]",
        "recommended",
        status="retired",
        supersedes="ent_aa8e",
    ),
]


def test_red_on_the_reversed_edge_actually_written():
    """THE second regression proof: the direction error that really happened."""
    findings = find_reversed_supersedes(INVERTED_EDGES)
    assert {f["source"] for f in findings} == {"ent_00fa", "ent_6d91"}
    for f in findings:
        assert f["source_status"] == "retired"
        assert f["target_status"] == "active"


def test_reversed_edge_caught_even_when_rule_kinds_agree():
    """The structural signal's advantage over the lexical one.

    The Human Review pair are BOTH `recommended`, so `find_contradictions` says
    nothing about them — correctly. The reversed edge is still a defect, and
    only the structural check sees it.
    """
    pair = INVERTED_EDGES[2:]
    assert find_contradictions(pair) == []
    assert len(find_reversed_supersedes(pair)) == 1


def test_correct_edge_direction_is_green():
    """Survivor -> retired is the defined direction and must not be flagged."""
    correct = [
        row("ent_7dbf", "Rule [sg]", "mandatory", supersedes="ent_00fa"),
        row("ent_00fa", "Rule [governance]", "recommended", status="retired"),
    ]
    assert find_reversed_supersedes(correct) == []


def test_edge_between_two_retired_rows_is_not_flagged():
    """Only a retired row pointing at a LIVE one inverts a ruling."""
    both = [
        row("a", "Rule [sg]", "mandatory", status="retired"),
        row("b", "Rule [governance]", "recommended", status="retired", supersedes="a"),
    ]
    assert find_reversed_supersedes(both) == []


def test_dangling_supersedes_target_is_not_flagged():
    """An id not in the snapshot is unresolvable, not a proven inversion."""
    rows = [row("b", "Rule", "recommended", status="retired", supersedes="ent_gone")]
    assert find_reversed_supersedes(rows) == []


# --- operator-PII gate on --write --------------------------------------------


# PII fixtures are ASSEMBLED AT RUNTIME, never written as literals.
#
# The repo's own gitleaks rules (`pii-iban`, `pii-phone-intl` in .gitleaks.toml)
# correctly flag an IBAN- or phone-shaped string anywhere in the tree, including
# in a test proving a screen catches one. Two controls doing their jobs,
# colliding. A `.gitleaks.toml` allowlist would resolve it by punching a
# path-scoped hole in one of the few things standing between this public repo
# and a real leak — and holes get widened later.
#
# So the screen still receives a fully realistic input, and the repository never
# holds a matching string. No fragment below matches on its own: the IBAN check
# digits and the phone country code are only ever adjacent in memory.
#
# The joined values MUST still trip the screen — a fixture refactored into
# something the screen no longer matches would gut the test while CI stayed
# green, which is the exact defect class this PR exists to close.
# `test_runtime_pii_fixtures_really_match_the_screen` below asserts that
# directly, per shape, so the refactor cannot silently defeat it.


def _fake_iban() -> str:
    """A syntactically valid, deliberately fictional IBAN, built from parts."""
    return "".join(["ES", "91", " 2100", " 0418", " 4502", " 0005", " 1332"])


def _fake_intl_phone() -> str:
    """An international-format number, built from parts. Not a real line."""
    return "+" + "34" + " " + "612" + " " + "345" + " " + "678"


def _fake_btc_bech32() -> str:
    return "bc1q" + "ar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


def _fake_btc_legacy() -> str:
    return "1A1zP1eP5" + "QGefi2DMPTfTL5SLmv7DivfNa"


def pii_fixtures() -> list[tuple[str, str]]:
    """(label, value) for each shape the screen must refuse."""
    return [
        ("bech32 BTC", f"pay {_fake_btc_bech32()} monthly"),
        ("legacy BTC", f"send to {_fake_btc_legacy()}"),
        ("IBAN", f"transfer to {_fake_iban()}"),
        ("ISO currency", "fee is 45 EUR per session"),
        ("symbol currency", "charge €1,250.00 quarterly"),
        ("intl phone", f"call {_fake_intl_phone()} to confirm"),
    ]


@pytest.mark.parametrize("label,value", pii_fixtures(), ids=lambda v: str(v)[:24])
def test_write_refuses_on_operator_pii(label, value):
    """Fail CLOSED: --write must refuse, never warn, on a PII shape."""
    with pytest.raises(OperatorPIIRefusal) as exc:
        _screen_row_for_operator_pii(
            {"entity_id": "ent_x", "title_stem": value}, {"entity_id": "ent_x"}, {}
        )
    # The refusal must name the offending entity so it is actionable.
    assert "ent_x" in str(exc.value)


def test_runtime_pii_fixtures_really_match_the_screen():
    """Guard the refactor itself: every assembled fixture must still match.

    Without this, someone "simplifying" a fixture into a string the screen no
    longer recognises would leave `test_write_refuses_on_operator_pii` passing
    for the wrong reason — it would be asserting that a non-PII string raises,
    which it never would, so the test would fail loudly... unless the fixture
    still matched some OTHER pattern. This pins each shape to a real match, so a
    fixture cannot drift onto the wrong rule and still look green.
    """
    for label, value in pii_fixtures():
        matched = [name for name, pat in _PII_PATTERNS if pat.search(value)]
        assert matched, f"{label!r} no longer matches any PII pattern: {value!r}"


def test_no_pii_shaped_literal_is_committed_in_this_file():
    """This test file must not itself contain what the screen looks for.

    The point of assembling fixtures at runtime is that the repository never
    holds a matching string. This asserts that directly against this file's own
    source, so a future literal-valued fixture fails here rather than in the
    repo's gitleaks run — where it would surface as a PII finding on a public
    repo instead of a unit-test failure.

    Scoped to the two shapes that actually collided with .gitleaks.toml
    (`pii-iban`, `pii-phone-intl`); the currency fixtures are not PII-shaped to
    gitleaks and stay readable as literals.
    """
    source = Path(__file__).read_text()
    iban = re.compile(r"\b[A-Z]{2}[0-9]{2}(?:[ ]?[A-Z0-9]{4}){3,7}\b")
    intl_phone = re.compile(r"\+[0-9](?:[ .-]?[0-9]){8,14}\b")
    assert not iban.search(source), "an IBAN-shaped literal is committed here"
    assert not intl_phone.search(source), "a phone-shaped literal is committed here"


@pytest.mark.parametrize(
    "value",
    [
        "agent background execution rules",
        "global agent rules for neotoma",
        "risk classification for neotoma changes",
        "agent prompts are always public and pii-free",
        # Regression: an ISO date inside ordinary policy prose tripped the first
        # phone pattern and refused a clean row. A date carries no + or 00
        # prefix, so the anchored pattern must let it through.
        "tooling_acquisition|allow|operator directive, 2026-06-24. autonomous ok",
        "store agent rules in neotoma, sync to harness — never memory files",
    ],
)
def test_write_does_not_refuse_on_benign_policy_text(value):
    _screen_row_for_operator_pii(
        {"entity_id": "ent_ok", "title_stem": value}, {"entity_id": "ent_ok"}, {}
    )


def test_stem_is_bounded_for_minimization():
    """A row whose canonical_name is its whole body must not land in full.

    This is what keeps a public snapshot from carrying ~3,300 characters of
    policy prose copied out of the record.
    """
    long_body = "x" * 4000
    stem = subject_key({"title": None, "canonical_name": f"agent_policy:{long_body}"})
    assert len(stem) <= STEM_MAX_CHARS


def test_committed_snapshot_carries_no_body_fields():
    """The snapshot stores only what the two checks compare."""
    snap = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "governance"
            / "agent_policy_rule_kind.json"
        ).read_text()
    )
    allowed = {
        "entity_id",
        "title_stem",
        "scope",
        "rule_kind",
        "status",
        "supersedes",
    }
    for r in snap["policies"]:
        assert set(r) <= allowed, f"unexpected field in {r.get('entity_id')}"
        assert len(r.get("title_stem") or "") <= STEM_MAX_CHARS


def test_cli_exits_1_on_a_reversed_edge_snapshot(tmp_path):
    """End-to-end: the CLI goes red on a reversed edge alone."""
    snap = tmp_path / "s.json"
    snap.write_text(json.dumps({"policies": INVERTED_EDGES}))
    proc = subprocess.run(
        [sys.executable, str(LINTER), "--snapshot", str(snap)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "reversed `supersedes`" in proc.stdout


def test_cli_exits_2_when_snapshot_is_missing(tmp_path):
    """A check that did not run is not a check that passed."""
    proc = subprocess.run(
        [sys.executable, str(LINTER), "--snapshot", str(tmp_path / "nope.json")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
