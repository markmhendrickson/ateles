"""The payment loop records each approved outcome as a payment_event.

Drives ``monedula.main()`` on the payment_approval channel with Neotoma faked
behind ``payment_approval._request`` and a fake handler. Synthetic values only;
no network, no wallet, no real journal.
"""

from __future__ import annotations

import sys
import types
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import monedula  # noqa: E402
import payment_approval  # noqa: E402
import payment_event  # noqa: E402
import consent_email  # noqa: E402
from test_payment_approval import (  # noqa: E402
    APPROVER_JKT,
    APPROVER_SUB,
    FakeNeotoma,
    env,  # noqa: F401 - pytest fixture
)
from test_session_payments import _event, _Handler, _World  # noqa: E402


def _capture_events(neo, monkeypatch, *, state: dict | None = None) -> list[dict]:
    """Route payment_event stores to a list; everything else to the fake."""
    events: list[dict] = []
    rows: dict[str, dict] = {}
    state = state if state is not None else {}
    inner = neo.request

    def request(method, path, body=None):
        if path == "/store" and any(
            e.get("entity_type") == payment_event.ENTITY_TYPE for e in body["entities"]
        ):
            if state.get("fail"):
                raise payment_approval.ApprovalError("HTTP 500: synthetic store failure")
            events.append(body)
            entity_id = f"ent_payment_{len(rows)}"
            rows[entity_id] = dict(body["entities"][0])
            return {"entity_ids": [entity_id]}
        if path in {f"/entities/{eid}" for eid in rows}:
            row = dict(rows[path.rsplit("/", 1)[1]])
            if state.get("mismatch"):
                row["status"] = "stale"
            return {"snapshot": {"snapshot": row}}
        return inner(method, path, body)

    monkeypatch.setattr(payment_approval, "_request", request)
    return events


def _capture_alerts(monkeypatch) -> list[tuple]:
    alerts: list[tuple] = []
    monkeypatch.setattr(
        monedula, "_notify", lambda *a, **k: alerts.append((a, tuple(sorted(k.items()))))
    )
    return alerts


def _approve_and_pay(w, neo) -> str:
    w.tick()
    key = neo.only["payment_key"]
    neo.resolve(key, "approved", "attended yoga")
    w.tick(minutes=15)
    return key


def test_sent_result_writes_one_payment_event_with_identity(env, monkeypatch):
    w, neo = env
    events = _capture_events(neo, monkeypatch)
    key = _approve_and_pay(w, neo)
    assert len(w.handler.execute_calls) == 1
    (body,) = events
    assert body["strict"] is True
    (entity,) = body["entities"]
    assert entity["approval_id"] == neo.ids[key]
    assert entity["session_keys"] == ["yoga:evt-1"]
    assert entity["primary_session_key"] == "yoga:evt-1"
    assert entity["payment_date"] == "2026-11-11"
    assert entity["status"] == "sent"
    assert entity["amount_eur"] == 10
    # A later tick neither pays nor records again.
    w.tick(minutes=15)
    assert len(events) == 1


def test_failed_result_writes_one_payment_event_with_status_failed(env, monkeypatch):
    w, neo = env
    events = _capture_events(neo, monkeypatch)

    def execute(match):
        w.handler.execute_calls.append(match)
        return {"status": "failed", "txid": "", "error": "synthetic free text"}

    w.handler.execute = execute
    _approve_and_pay(w, neo)
    (body,) = events
    (entity,) = body["entities"]
    assert entity["status"] == "failed"
    assert entity["session_keys"] == ["yoga:evt-1"]
    assert "error" not in entity, "free-text error is never copied"
    assert "txid" not in entity, "empty result fields are not copied"


def test_one_off_without_match_has_stable_obligation_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(monedula, "PAYMENT_EVENT_PENDING_FILE", tmp_path / "pending.json", raising=False)
    events = _capture_events(FakeNeotoma(), monkeypatch)
    handler = _Handler()
    handler.profile.calendar_keywords = []
    first = consent_email.build_pending_items([(handler, [])], "2030-01-02")[0]
    second = consent_email.build_pending_items([(handler, [])], "2030-01-02")[0]
    assert first.match is None and first.obligation == second.obligation
    assert monedula._record_payment_event(first, {"status": "sent"}, "sent")
    assert monedula._record_payment_event(second, {"status": "sent"}, "sent")
    keys = [body["entities"][0]["primary_session_key"] for body in events]
    assert keys == [f"{handler.name}:obligation-{first.obligation}"] * 2


def test_no_identity_is_a_record_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(monedula, "PAYMENT_EVENT_PENDING_FILE", tmp_path / "pending.json", raising=False)
    calls = []
    monkeypatch.setattr(payment_approval, "_request", lambda *a, **k: calls.append(a))
    alerts = _capture_alerts(monkeypatch)
    item = types.SimpleNamespace(
        handler_name="synthetic_handler", label="Synthetic", amount_eur=10,
        payment_date="2030-01-02", match=None, obligation="", approval_id="",
    )
    assert monedula._record_payment_event(item, {"status": "sent"}, "sent") is False
    assert calls == []
    assert len(json.loads((tmp_path / "pending.json").read_text())["entries"]) == 1
    assert sum("could not be recorded" in repr(a) for a in alerts) == 1


def _world(m, tmp_path):
    """The ``env`` fixture's setup, callable twice in one test."""
    from lib.daemon_runtime import gating

    w = _World(m, tmp_path, _Handler())
    w.handler.profile.payment_type = "wise"
    w.handler.profile.wise_reference = "ref"
    w.handler.profile.wise_recipient_name = "Test Payee"
    m.setenv("MONEDULA_CONSENT_CHANNEL", "payment_approval")
    m.setenv("MONEDULA_PAYMENT_APPROVER_SUB", APPROVER_SUB)
    m.setenv("MONEDULA_PAYMENT_APPROVER_JKT", APPROVER_JKT)
    neo = FakeNeotoma()
    m.setattr(payment_approval, "_request", neo.request)
    m.setattr(
        gating, "_fetch_entity_observations", lambda eid, **k: list(neo.obs.get(eid, []))
    )
    w.calendars["primary"] = [_event("evt-1")]
    m.setattr(monedula, "PAYMENT_EVENT_PENDING_FILE", tmp_path / "pending.json", raising=False)
    return w, neo


def _run(monkeypatch, tmp_path, *, fail: bool):
    """One approve-and-pay in a fresh world; the store fails when ``fail``."""
    tmp_path.mkdir()
    with monkeypatch.context() as m:
        w, neo = _world(m, tmp_path)
        _capture_events(neo, m, state={"fail": fail})
        alerts = _capture_alerts(m)
        _approve_and_pay(w, neo)
        obligations = {
            k: {f: v for f, v in e.items() if not f.endswith("_at")}
            for k, e in w.journal["obligations"].items()
        }
        return len(w.handler.execute_calls), obligations, alerts


def test_store_error_leaves_journal_and_alert_identical(monkeypatch, tmp_path):
    baseline = _run(monkeypatch, tmp_path / "ok", fail=False)
    failing = _run(monkeypatch, tmp_path / "fail", fail=True)
    executed, obligations, alerts = failing
    assert executed == 1
    (entry,) = obligations.values()
    assert entry["state"] == "done" and entry["outcome_status"] == "sent"
    assert any("payment_outcome" in repr(a) for a in alerts)
    assert obligations == baseline[1]
    assert [a for a in alerts if "payment_outcome" in repr(a)] == [
        a for a in baseline[2] if "payment_outcome" in repr(a)
    ]
    assert (tmp_path / "fail" / "pending.json").exists()


def _two_approved(w, neo):
    w.calendars["primary"] = [_event("evt-1"), _event("evt-2")]
    w.tick()
    for key in list(neo.rows):
        neo.resolve(key, "approved", "attended yoga")


def test_readback_mismatch_halts_next_approved_payment(monkeypatch, tmp_path):
    w, neo = _world(monkeypatch, tmp_path)
    _capture_events(neo, monkeypatch, state={"mismatch": True})
    alerts = _capture_alerts(monkeypatch)
    _two_approved(w, neo)
    assert w.tick(minutes=15) is False
    assert len(w.handler.execute_calls) == 1
    assert sum("payment outcome could not be recorded" in repr(a) for a in alerts) == 1
    assert len(json.loads((tmp_path / "pending.json").read_text())["entries"]) == 1
    assert w.tick(minutes=15) is False
    assert len(w.handler.execute_calls) == 1


def test_store_error_retries_record_only_then_resumes(monkeypatch, tmp_path):
    w, neo = _world(monkeypatch, tmp_path)
    state = {"fail": True}
    events = _capture_events(neo, monkeypatch, state=state)
    alerts = _capture_alerts(monkeypatch)
    _two_approved(w, neo)
    assert w.tick(minutes=15) is False
    assert len(w.handler.execute_calls) == 1
    assert w.tick(minutes=15) is False
    assert w.tick(minutes=15) is False
    assert len(w.handler.execute_calls) == 1
    assert sum("payment outcome could not be recorded" in repr(a) for a in alerts) == 1
    state["fail"] = False
    w.tick(minutes=15)
    assert len(w.handler.execute_calls) == 2
    assert len(events) == 2
    assert json.loads((tmp_path / "pending.json").read_text())["entries"] == []
    assert sum("halt cleared" in repr(a) for a in alerts) == 1


def test_each_halt_announces_its_own_recovery(monkeypatch, tmp_path):
    """Dedupe keys stay open until released, as the real notifier's do: a
    second halt-and-recover cycle still sends one halt alert and one cleared
    alert."""
    import payment_event

    monkeypatch.setattr(monedula, "PAYMENT_EVENT_PENDING_FILE", tmp_path / "pending.json", raising=False)
    open_keys: set[str] = set()
    sent: list[str] = []

    def notify(msg, *a, dedupe_key=None, **k):
        if dedupe_key and dedupe_key in open_keys:
            return
        if dedupe_key:
            open_keys.add(dedupe_key)
        sent.append(msg)

    monkeypatch.setattr(monedula, "_notify", notify)
    monkeypatch.setattr(monedula, "_clear_notify_dedupe", open_keys.discard)
    state = {"fail": True}

    def store(fields, **identity):
        if state["fail"]:
            raise payment_event.PaymentEventError("synthetic store failure")
        return {"entity_ids": ["ent_synthetic"]}

    monkeypatch.setattr(payment_event, "store", store)
    entry = {"fields": {"status": "sent"}, "identity": {"payment_date": "2026-01-02", "approval_id": "ent_a", "session_keys": []}}
    for _cycle in range(2):
        state["fail"] = True
        monedula._save_pending_payment_events({"entries": [entry], "halted": False})
        assert monedula._retry_pending_payment_events() is False
        assert monedula._retry_pending_payment_events() is False
        state["fail"] = False
        assert monedula._retry_pending_payment_events() is True
    assert sum("could not be recorded" in m for m in sent) == 2
    assert sum("halt cleared" in m for m in sent) == 2


def test_crash_before_journal_outcome_leaves_record_queued(monkeypatch, tmp_path):
    """The record is queued before the journal marks the obligation done, so a
    crash between the two leaves it for the next tick, which records it and
    never pays again."""
    import payment_journal

    w, neo = _world(monkeypatch, tmp_path)
    events = _capture_events(neo, monkeypatch, state={})
    _capture_alerts(monkeypatch)
    real_outcome = payment_journal.record_outcome
    crash = {"on": True}

    def outcome(*a, **k):
        if crash["on"]:
            raise SystemExit("synthetic crash after the queue write")
        return real_outcome(*a, **k)

    monkeypatch.setattr(payment_journal, "record_outcome", outcome)
    w.tick()
    key = neo.only["payment_key"]
    neo.resolve(key, "approved", "attended yoga")
    try:
        w.tick(minutes=15)
    except SystemExit:
        pass
    assert len(w.handler.execute_calls) == 1
    assert events == []
    assert len(json.loads((tmp_path / "pending.json").read_text())["entries"]) == 1
    crash["on"] = False
    w.tick(minutes=15)
    assert len(w.handler.execute_calls) == 1
    assert len(events) == 1
    assert json.loads((tmp_path / "pending.json").read_text())["entries"] == []


def test_record_failure_still_settles_the_paid_item(monkeypatch, tmp_path):
    w, neo = _world(monkeypatch, tmp_path)
    _capture_events(neo, monkeypatch, state={"fail": True})
    _capture_alerts(monkeypatch)
    settled: list[str] = []
    monkeypatch.setattr(
        monedula, "_resolve_item_session", lambda item, state: settled.append(state)
    )
    _two_approved(w, neo)
    assert w.tick(minutes=15) is False
    assert len(w.handler.execute_calls) == 1
    assert settled.count("settled") == 1
