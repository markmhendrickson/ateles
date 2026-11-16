"""The payment loop records each approved outcome as a payment_event.

Drives ``monedula.main()`` on the payment_approval channel with Neotoma faked
behind ``payment_approval._request`` and a fake handler. Synthetic values only;
no network, no wallet, no real journal.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import monedula  # noqa: E402
import payment_approval  # noqa: E402
import payment_event  # noqa: E402
from test_payment_approval import (  # noqa: E402
    APPROVER_JKT,
    APPROVER_SUB,
    FakeNeotoma,
    env,  # noqa: F401 - pytest fixture
)
from test_session_payments import _event, _Handler, _World  # noqa: E402


def _capture_events(neo, monkeypatch, *, fail: bool = False) -> list[dict]:
    """Route payment_event stores to a list; everything else to the fake."""
    events: list[dict] = []
    inner = neo.request

    def request(method, path, body=None):
        if path == "/store" and any(
            e.get("entity_type") == payment_event.ENTITY_TYPE for e in body["entities"]
        ):
            if fail:
                raise payment_approval.ApprovalError("HTTP 500: synthetic store failure")
            events.append(body)
            return {"ok": True}
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


def test_item_with_no_approval_id_and_no_session_key_writes_nothing(monkeypatch):
    calls: list = []
    monkeypatch.setattr(payment_approval, "_request", lambda *a, **k: calls.append(a))
    lines: list[str] = []
    fake_log = types.SimpleNamespace(
        warning=lines.append, error=lines.append, info=lines.append
    )
    monkeypatch.setattr(monedula, "log", fake_log)
    item = types.SimpleNamespace(
        handler_name="synthetic_handler",
        label="Synthetic",
        amount_eur=10,
        payment_date="2030-01-02",
        match={"summary": "no calendar event"},
        obligation="0123456789abcdef",
        approval_id="",
    )
    monedula._record_payment_event(item, {"status": "sent"}, "sent")
    assert calls == []
    assert len(lines) == 1 and "payment_event_not_recorded" in lines[0]


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
    return w, neo


def _run(monkeypatch, tmp_path, *, fail: bool):
    """One approve-and-pay in a fresh world; the store fails when ``fail``."""
    tmp_path.mkdir()
    with monkeypatch.context() as m:
        w, neo = _world(m, tmp_path)
        _capture_events(neo, m, fail=fail)
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
    assert failing == baseline
