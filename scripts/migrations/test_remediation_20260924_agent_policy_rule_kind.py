"""Contract tests for the ateles#1245 rule_kind remediation script.

Two things are asserted, per the issue's QA section:

1. The 16 rulings are individual and non-mechanical — not a `recommended ->
   advisory` (or any other) mapping function. A future edit that collapses
   RULINGS back into a map keyed only by the PRIOR value breaks this test.
2. The script is idempotent against an already-remediated row set: re-running
   it (the `--dry-run` planning step, which every real run start from) is
   side-effect-free and does not depend on the row's prior value staying
   fixed across runs.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import importlib.util as _il

_spec = _il.spec_from_file_location(
    "remediation_2026_09_24",
    Path(__file__).parent / "remediation_20260924_agent_policy_rule_kind.py",
)
remediation = _il.module_from_spec(_spec)
_spec.loader.exec_module(remediation)  # type: ignore[union-attr]


EXPECTED_RULING_COUNT = 16


def test_ruling_table_has_exactly_the_16_rows():
    assert len(remediation.RULINGS) == EXPECTED_RULING_COUNT


def test_rulings_are_individual_not_a_mechanical_map():
    # The defining property this issue rules out: two rows sharing a PRIOR
    # rule_kind must be free to land on DIFFERENT rulings. If every row
    # shared a prior value mapped exactly one way, a mechanical remap would
    # be indistinguishable from the real table — so assert at least one
    # prior value is genuinely ambiguous (splits across both rulings).
    by_prior: dict[str | None, set[str]] = {}
    for prior, ruling, _rationale in remediation.RULINGS.values():
        by_prior.setdefault(prior, set()).add(ruling)

    ambiguous_priors = [
        prior for prior, rulings in by_prior.items() if len(rulings) > 1
    ]
    assert ambiguous_priors, (
        "expected at least one prior rule_kind value to split across both "
        "rulings, proving the table isn't a mechanical prior->ruling map"
    )
    # 'recommended' is the concrete case named in the issue: some retired/
    # checklist rows ruled advisory, some safety-content rows ruled mandatory.
    assert "recommended" in ambiguous_priors


def test_every_ruling_is_in_vocabulary():
    for entity_id, (_prior, ruling, _rationale) in remediation.RULINGS.items():
        assert ruling in ("mandatory", "advisory"), (
            f"{entity_id}: {ruling!r} not in vocabulary"
        )


def test_every_ruling_has_a_nonempty_rationale():
    for entity_id, (_prior, _ruling, rationale) in remediation.RULINGS.items():
        assert rationale and len(rationale) > 20, (
            f"{entity_id}: rationale too short to be a real ruling"
        )


def test_idempotency_key_is_stable_per_entity():
    keys = {eid: remediation._idempotency_key(eid) for eid in remediation.RULINGS}
    assert len(set(keys.values())) == len(keys), (
        "idempotency keys must be unique per entity_id"
    )
    for eid, key in keys.items():
        assert key == remediation._idempotency_key(eid), (
            "idempotency key must be deterministic"
        )
        assert eid in key


def test_dry_run_does_not_require_credentials(monkeypatch):
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    assert remediation.run(dry_run=True) == 0


def test_live_run_without_credentials_fails_closed(monkeypatch):
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    assert remediation.run(dry_run=False) == 2


def test_rerun_against_already_remediated_rows_is_a_noop(monkeypatch):
    """Idempotent re-run: every row already at its ruling → zero corrections."""
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")

    calls = {"correct": 0, "get": 0}

    def fake_request(method, path, token, base, body=None):
        if method == "GET":
            calls["get"] += 1
            entity_id = path.rsplit("/", 1)[-1]
            _prior, ruling, _rationale = remediation.RULINGS[entity_id]
            return {"snapshot": {"rule_kind": ruling}}
        if path == "/correct":
            calls["correct"] += 1
            return {"ok": True}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(remediation, "_neotoma_request", fake_request)

    rc = remediation.run(dry_run=False)
    assert rc == 0
    assert calls["correct"] == 0, (
        "a fully-remediated row set must trigger zero correct() calls on re-run"
    )
    assert calls["get"] == EXPECTED_RULING_COUNT


def test_first_run_applies_every_row_and_reads_back(monkeypatch):
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")

    state: dict[str, str | None] = {eid: None for eid in remediation.RULINGS}
    calls = {"correct": 0}

    def fake_request(method, path, token, base, body=None):
        if method == "GET":
            entity_id = path.rsplit("/", 1)[-1]
            return {"snapshot": {"rule_kind": state[entity_id]}}
        if path == "/correct":
            calls["correct"] += 1
            state[body["entity_id"]] = body["value"]
            return {"ok": True}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(remediation, "_neotoma_request", fake_request)

    rc = remediation.run(dry_run=False)
    assert rc == 0
    assert calls["correct"] == EXPECTED_RULING_COUNT
    for entity_id, (_prior, ruling, _rationale) in remediation.RULINGS.items():
        assert state[entity_id] == ruling


def test_missing_entity_is_never_corrected(monkeypatch):
    """A row deleted between snapshot capture and this run must be refused,
    never silently correct()-ed as if it were merely absent a rule_kind."""
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")

    calls = {"correct": 0}

    def fake_request(method, path, token, base, body=None):
        if method == "GET":
            return {}  # no "snapshot" key at all: the entity is gone
        if path == "/correct":
            calls["correct"] += 1
            return {"ok": True}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(remediation, "_neotoma_request", fake_request)

    rc = remediation.run(dry_run=False)
    assert rc == 1
    assert calls["correct"] == 0, "a missing entity must never be correct()-ed"
