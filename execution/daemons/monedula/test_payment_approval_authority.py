# ruff: noqa: F401,F811  (the shared ``env`` fixture is imported, not redefined)
"""Who may approve a payment, per-item isolation, lookup shape, cutover guard.

Neotoma is faked (see ``test_payment_approval.FakeNeotoma``); synthetic data only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import consent_email  # noqa: E402
import payment_approval  # noqa: E402
import payment_journal  # noqa: E402
from test_payment_approval import (  # noqa: E402
    APPROVER_JKT,
    APPROVER_SUB,
    OTHER_SUB,
    env,
)
from test_session_payments import _event  # noqa: E402


def _approve(neo, answer="attended yoga", **who):
    neo.resolve(neo.only["payment_key"], "approved", answer, **who)


# ── Approval authority (red against the snapshot-trusting judge) ────────────


def test_a_well_formed_approval_written_by_the_wrong_principal_is_not_paid(env):
    """Red against the old code: judge() read snapshot.status/operator_answer
    and paid whatever any Neotoma writer put there."""
    w, neo = env
    w.tick()
    _approve(neo, sub=OTHER_SUB)
    w.tick(minutes=15)
    assert w.handler.execute_calls == []


def test_the_configured_approver_is_paid(env):
    w, neo = env
    w.tick()
    _approve(neo)
    w.tick(minutes=15)
    assert len(w.handler.execute_calls) == 1


def test_wrong_key_thumbprint_is_not_paid(env):
    w, neo = env
    w.tick()
    _approve(neo, jkt="B" * 43)
    w.tick(minutes=15)
    assert w.handler.execute_calls == []


def test_untrusted_attribution_tier_is_not_paid(env):
    w, neo = env
    w.tick()
    _approve(neo, tier="anonymous")
    w.tick(minutes=15)
    assert w.handler.execute_calls == []


@pytest.mark.parametrize("missing", ["MONEDULA_PAYMENT_APPROVER_SUB", "MONEDULA_PAYMENT_APPROVER_JKT"])
def test_unconfigured_approver_pays_nothing(env, monkeypatch, missing):
    w, neo = env
    w.tick()
    _approve(neo)
    monkeypatch.delenv(missing)
    w.tick(minutes=15)
    assert w.handler.execute_calls == []


def test_a_malformed_approver_thumbprint_pays_nothing(env, monkeypatch):
    w, neo = env
    w.tick()
    _approve(neo)
    monkeypatch.setenv("MONEDULA_PAYMENT_APPROVER_JKT", "short")
    w.tick(minutes=15)
    assert w.handler.execute_calls == []


def test_status_by_the_approver_but_answer_by_someone_else_is_not_paid(env):
    w, neo = env
    w.tick()
    key = neo.only["payment_key"]
    neo.rows[key]["operator_answer"] = "attended yoga"
    neo._observe(key, {"operator_answer": "attended yoga"}, OTHER_SUB, APPROVER_JKT)
    neo.rows[key]["status"] = "approved"
    neo._observe(key, {"status": "approved"}, APPROVER_SUB, APPROVER_JKT)
    w.tick(minutes=15)
    assert w.handler.execute_calls == []


def test_a_later_write_by_another_principal_withdraws_the_approval(env):
    w, neo = env
    w.tick()
    _approve(neo)
    key = neo.only["payment_key"]
    neo.rows[key]["operator_answer"] = "attended yoga please"
    neo._observe(key, {"operator_answer": "attended yoga please"}, OTHER_SUB, APPROVER_JKT)
    w.tick(minutes=15)
    assert w.handler.execute_calls == []


def test_a_snapshot_with_no_provenance_is_not_paid(env):
    w, neo = env
    w.tick()
    _approve(neo)
    neo.prov[neo.only["payment_key"]].pop("status")
    w.tick(minutes=15)
    assert w.handler.execute_calls == []


def test_an_unauthenticated_rejection_does_not_skip_the_session(env):
    """A forged rejection must not permanently retire a payable session."""
    w, neo = env
    w.tick()
    neo.resolve(neo.only["payment_key"], "rejected", "no", sub=OTHER_SUB)
    w.tick(minutes=15)
    assert payment_journal.session_state(w.journal, "yoga:evt-1") != "skipped"


# ── Isolation: one bad record holds only its own payment ────────────────────


def test_one_unreadable_record_holds_only_that_payment(env):
    w, neo = env
    w.calendars["primary"] = [_event("evt-1"), _event("evt-2")]
    w.tick()
    k1 = consent_email.obligation_key(w.handler, "2026-11-11", "evt-1")
    k2 = consent_email.obligation_key(w.handler, "2026-11-11", "evt-2")
    assert {k1, k2} <= set(neo.rows)
    neo.resolve(k1, "approved", "attended yoga")
    neo.resolve(k2, "approved", "attended yoga")
    neo.fail_query_for = {k1}
    w.tick(minutes=15)
    assert len(w.handler.execute_calls) == 1
    assert payment_journal.obligation_state(w.journal, k2) == "done"
    assert payment_journal.obligation_state(w.journal, k1) is None


# ── Lookup shape ─────────────────────────────────────────────────────────────


def test_lookup_is_filtered_server_side_on_payment_key(env):
    w, neo = env
    w.tick()
    w.tick(minutes=15)
    assert neo.queries
    for q in neo.queries:
        assert q["snapshot_filters"] == {
            "payment_key": {"op": "eq", "value": neo.only["payment_key"]}
        }
        assert q["limit"] <= 10


def test_two_entities_for_one_key_is_an_error_not_a_pick(monkeypatch):
    row = {"entity_id": "ent_a", "snapshot": {"payment_key": "k"}}

    def request(method, path, body=None):
        return {"entities": [row, dict(row, entity_id="ent_b")]}

    monkeypatch.setattr(payment_approval, "_request", request)
    with pytest.raises(payment_approval.ApprovalError):
        payment_approval.find_entity("k")


# ── Cutover: a session paid under the pre-window obligation key ─────────────


@pytest.mark.parametrize("state", ["done", "intent"])
def test_session_paid_under_the_old_obligation_key_is_never_raised_again(env, state):
    """The old code keyed a single match on (run yesterday, empty match id).
    A session paid just before this change went live exists only under that
    key. Red against the old code: a fresh payment_approval was raised and a
    truthful "attended" paid it twice."""
    w, neo = env
    old_key = consent_email.obligation_key(w.handler, "2026-11-11", "")
    jpath = consent_email.journal_path()
    payment_journal.record_intent(jpath, old_key, "old-token", generation=1)
    if state == "done":
        payment_journal.record_outcome(jpath, old_key, "sent")
    w.tick()
    w.tick(minutes=15)
    assert neo.writes == 0, "a payment_approval was raised for an already-paid session"
    assert w.handler.execute_calls == []
    assert payment_journal.session_state(w.journal, "yoga:evt-1") == "settled"


def test_multi_match_legacy_index_key_is_also_recognised(env):
    w, neo = env
    w.calendars["primary"] = [_event("evt-1"), _event("evt-2")]
    old_key = consent_email.obligation_key(w.handler, "2026-11-11", "1")
    jpath = consent_email.journal_path()
    payment_journal.record_intent(jpath, old_key, "old-token", generation=1)
    payment_journal.record_outcome(jpath, old_key, "sent")
    w.tick()
    # the session that was match index 1 is settled; the other is still raised
    assert payment_journal.session_state(w.journal, "yoga:evt-2") == "settled"
    assert len(neo.rows) == 1


# ── Operator visibility: no silent wait ──────────────────────────────────────


@pytest.fixture
def alerts(monkeypatch):
    import monedula

    calls: list[dict] = []

    def record(message, priority="info", dedupe_key=None, email_eligible=True):
        calls.append({"message": message, "priority": priority, "key": dedupe_key})

    monkeypatch.setattr(monedula, "_notify", record)
    return calls


def _kind(alerts, kind):
    return [a for a in alerts if f"_{kind}:" in (a["key"] or "")]


def test_a_raised_record_notifies_the_operator_once(env, alerts):
    """Red against caf09b51: raising a record told the operator nothing."""
    w, neo = env
    w.tick()
    w.tick(minutes=15)
    raised = _kind(alerts, "raised")
    assert len(raised) == 1
    first = raised[0]
    assert first["priority"] == "blocker"
    assert first["key"].endswith(neo.only["payment_key"])
    assert "yoga" in first["message"] and "2026-11-11" in first["message"]
    assert "10 EUR" in first["message"] and neo.only["payment_key"][:12] in first["message"]
    assert "coordinator" in first["message"]


def test_a_record_awaiting_past_the_threshold_alerts_as_stale(env, alerts, monkeypatch):
    from datetime import timedelta

    w, neo = env
    w.tick()
    assert not _kind(alerts, "stale")
    real = payment_approval._now
    monkeypatch.setattr(payment_approval, "_now", lambda: real() + timedelta(hours=25))
    w.tick(minutes=15)
    stale = _kind(alerts, "stale")
    assert stale and stale[0]["priority"] == "blocker"
    assert neo.only["payment_key"] in stale[0]["key"]


def test_stale_threshold_is_configurable(env, alerts, monkeypatch):
    from datetime import timedelta

    w, neo = env
    w.tick()
    monkeypatch.setenv("MONEDULA_APPROVAL_STALE_HOURS", "2")
    real = payment_approval._now
    monkeypatch.setattr(payment_approval, "_now", lambda: real() + timedelta(hours=3))
    w.tick(minutes=15)
    assert _kind(alerts, "stale")


def test_a_held_payment_alert_names_the_payment_reason_and_is_keyed_per_payment(env, alerts):
    w, neo = env
    w.tick()
    _approve(neo, sub=OTHER_SUB)
    w.tick(minutes=15)
    held = _kind(alerts, "not_accepted")
    assert held
    msg = held[0]["message"]
    assert "not authenticated" in msg and "yoga" in msg and "[" in msg
    assert held[0]["key"].endswith(neo.only["payment_key"])


def test_a_rejection_that_skips_a_session_sends_an_info(env, alerts):
    w, neo = env
    w.tick()
    neo.resolve(neo.only["payment_key"], "rejected", "no")
    w.tick(minutes=15)
    skipped = _kind(alerts, "skipped")
    assert skipped and skipped[0]["priority"] == "info"


# ── Schema registration contract ─────────────────────────────────────────────


def test_schema_script_fields_equal_the_module_contract():
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    import register_payment_approval_schema as reg

    payload = reg.build_payload()
    declared = set(payload["schema_definition"]["fields"])
    assert declared == set(payment_approval.FIELD_SPECS)
    assert set(payment_approval.BOUND_FIELDS) <= declared
    assert {payment_approval.STATUS_FIELD, payment_approval.ANSWER_FIELD} <= declared
    assert set(payload["reducer_config"]["merge_policies"]) == declared
    assert payload["entity_type"] == payment_approval.ENTITY_TYPE
    assert not (payment_approval.FORBIDDEN_FIELD_NAMES & declared)


def test_every_field_raise_request_writes_is_declared(env):
    w, neo = env
    w.tick()
    assert set(neo.obs[neo.ids[neo.only["payment_key"]]][0]["fields"]) <= set(
        payment_approval.FIELD_SPECS
    )


def test_schema_script_is_dry_run_by_default(monkeypatch, capsys):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    import register_payment_approval_schema as reg

    def boom(*a, **k):
        raise AssertionError("dry run must not touch the network")

    monkeypatch.setattr(reg.neotoma_http, "request_json", boom)
    assert reg.main([]) == 0
    assert "DRY RUN" in capsys.readouterr().out


# ── The shared verifier: checkpoint behaviour is unchanged ───────────────────


def _checkpoint_fixture(monkeypatch, sub, tier="software"):
    from lib.daemon_runtime import gating

    obs = [
        {
            "id": "o1",
            "user_id": "u1",
            "fields": {"status": "approved"},
            "provenance": {
                "agent_sub": sub,
                "agent_thumbprint": APPROVER_JKT,
                "attribution_tier": tier,
            },
        }
    ]
    monkeypatch.setattr(gating, "_fetch_entity_observations", lambda eid, **k: obs)
    record = {"snapshot": {"status": "approved"}, "provenance": {"status": "o1"}}
    return gating, record


def test_checkpoint_resolution_result_shape_is_unchanged(monkeypatch):
    gating, record = _checkpoint_fixture(monkeypatch, APPROVER_SUB)
    got = gating.read_authenticated_checkpoint_resolution(
        "ent_cp",
        record,
        required_approver_sub=APPROVER_SUB,
        required_approver_jkt=APPROVER_JKT,
        expected_user_id="u1",
    )
    assert got == {
        "principal_sub": APPROVER_SUB,
        "observation_id": "o1",
        "attribution_tier": "software",
        "agent_thumbprint": APPROVER_JKT,
    }


def test_checkpoint_resolution_still_refuses_wrong_principal_and_bad_expectation(monkeypatch):
    gating, record = _checkpoint_fixture(monkeypatch, OTHER_SUB)
    kw = dict(
        required_approver_sub=APPROVER_SUB,
        required_approver_jkt=APPROVER_JKT,
        expected_user_id="u1",
    )
    assert gating.read_authenticated_checkpoint_resolution("e", record, **kw) is None
    gating, record = _checkpoint_fixture(monkeypatch, APPROVER_SUB)
    assert (
        gating.read_authenticated_checkpoint_resolution(
            "e", record, expected_resolution="maybe", **kw
        )
        is None
    )
    assert (
        gating.read_authenticated_checkpoint_resolution(
            "e", record, expected_resolution="rejected", **kw
        )
        is None
    )
