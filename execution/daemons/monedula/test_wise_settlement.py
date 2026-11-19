"""Synthetic Wise transitions; every journal and marker lives under tmp_path."""

# ruff: noqa: F811  (imported fixtures are intentionally injected by pytest)

from __future__ import annotations

import sys
import json
import time
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import payment_journal as journal  # noqa: E402
import wise_reconcile  # noqa: E402
from handlers import wise_transfer as wise  # noqa: E402
from test_payment_approval import env as approval_env  # noqa: E402,F401
from test_payment_approval_authority import _approve  # noqa: E402
from test_telegram_gate import sent_messages, _install_handlers  # noqa: E402,F401
from test_session_payments import _event, world  # noqa: E402,F401


def test_pending_obligation_state_is_distinct_from_intent():
    data = {"obligations": {"synthetic": {"state": "funding_pending"}}}
    assert journal.obligation_state(data, "synthetic") == "funding_pending"


@pytest.fixture
def attempt(tmp_path):
    path = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(path)
    journal.record_intent(path, "obligation", "approval", generation=1, rail="wise",
                          handler_name="synthetic", session_key="synthetic:session")
    return path


@pytest.mark.parametrize("status", ["CREATED", "COMPLETED", "PROCESSING", "PENDING"])
def test_funding_status_is_pending_and_never_updates_task(attempt, monkeypatch, status):
    monkeypatch.setattr(wise, "_get_wise_profile_id", lambda token: 1)
    monkeypatch.setattr(wise, "_get_or_create_recipient", lambda *a, **k: 2)
    monkeypatch.setattr(wise, "_create_quote", lambda *a, **k: "quote")
    seen = []

    def create(*args):
        seen.append(("create", args[-1]))
        return 3

    def fund(*args):
        assert journal.load(attempt)["obligations"]["obligation"]["transfer_id"] == 3
        seen.append(("fund", 3))
        return {"status": status}

    monkeypatch.setattr(wise, "_create_transfer", create)
    monkeypatch.setattr(wise, "_fund_transfer", fund)
    result = wise._execute_wise_transfer(
        "synthetic-token", "XX000", "Synthetic", 1, "ref",
        journal_context=(attempt, "obligation"),
    )
    assert result["status"] == "funding_pending"
    assert journal.obligation_state(journal.load(attempt), "obligation") == "funding_pending"
    assert [event[0] for event in seen] == ["create", "fund"]
    assert journal.load(attempt)["obligations"]["obligation"].get("outcome_status") is None


@pytest.mark.parametrize("status", ["SOMETHING_NEW", None])
def test_rejected_or_missing_funding_status_holds_existing_transfer(attempt, monkeypatch, status):
    monkeypatch.setattr(wise, "_get_wise_profile_id", lambda token: 1)
    monkeypatch.setattr(wise, "_get_or_create_recipient", lambda *a, **k: 2)
    monkeypatch.setattr(wise, "_create_quote", lambda *a, **k: "quote")
    monkeypatch.setattr(wise, "_create_transfer", lambda *a: 3)
    monkeypatch.setattr(wise, "_fund_transfer", lambda *a: {"status": status})
    with pytest.raises(RuntimeError, match="funding status unexpected"):
        wise._execute_wise_transfer(
            "synthetic-token", "XX000", "Synthetic", 1, "ref",
            journal_context=(attempt, "obligation"),
        )
    entry = journal.load(attempt)["obligations"]["obligation"]
    assert entry["state"] == "intent" and entry["transfer_id"] == 3
    with pytest.raises(journal.JournalError, match="already recorded"):
        journal.record_intent(attempt, "obligation", "approval", generation=1, rail="wise")


def test_rejected_funding_names_rejection_and_holds_transfer(attempt, monkeypatch):
    monkeypatch.setattr(wise, "_get_wise_profile_id", lambda *_: 1)
    monkeypatch.setattr(wise, "_get_or_create_recipient", lambda *a, **k: 2)
    monkeypatch.setattr(wise, "_create_quote", lambda *a: "quote")
    monkeypatch.setattr(wise, "_create_transfer", lambda *a: 3)
    monkeypatch.setattr(wise, "_fund_transfer", lambda *a: {"status": "REJECTED"})
    result = wise._execute_wise_transfer(
        "synthetic-token", "XX000", "Synthetic", 1, "ref",
        journal_context=(attempt, "obligation"),
    )
    assert result["status"] == "funding_rejected"
    entry = journal.load(attempt)["obligations"]["obligation"]
    assert entry["state"] == "intent" and entry["transfer_id"] == 3


def test_failed_pending_write_keeps_intent_and_consumed_approval(attempt, monkeypatch):
    journal.record_transfer_created(attempt, "obligation", 3)
    original = journal._write

    def fail_pending(path, data):
        if data["obligations"]["obligation"]["state"] == "funding_pending":
            raise journal.JournalError("synthetic write failure")
        return original(path, data)

    monkeypatch.setattr(journal, "_write", fail_pending)
    with pytest.raises(journal.JournalError, match="synthetic write failure"):
        journal.record_funding_pending(attempt, "obligation", 3, "COMPLETED")
    back = journal.load(attempt)
    assert back["obligations"]["obligation"]["state"] == "intent"
    assert journal.token_consumed(back, "approval")


def test_pending_readback_rejects_corrupted_approval_binding(attempt, monkeypatch):
    journal.record_transfer_created(attempt, "obligation", 3)
    original = journal._write

    def corrupt(path, data):
        payload = copy.deepcopy(data)
        payload["obligations"]["obligation"]["token"] = "wrong-approval"
        original(path, payload)

    monkeypatch.setattr(journal, "_write", corrupt)
    with pytest.raises(journal.JournalError):
        journal.record_funding_pending(attempt, "obligation", 3, "COMPLETED")


def test_failed_final_write_keeps_pending_and_never_applies_paid(attempt, monkeypatch):
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_funding_pending(attempt, "obligation", 3, "COMPLETED")
    original = journal._write

    def fail_done(path, data):
        if data["obligations"]["obligation"]["state"] == "done":
            raise journal.JournalError("synthetic final write failure")
        return original(path, data)

    monkeypatch.setattr(journal, "_write", fail_done)
    with pytest.raises(journal.JournalError, match="synthetic final write failure"):
        journal.record_wise_status(
            attempt, "obligation", 3, "outgoing_payment_sent", "2026-01-01T00:00:00+00:00"
        )
    assert journal.obligation_state(journal.load(attempt), "obligation") == "funding_pending"


def test_unreadable_or_malformed_entry_holds(attempt):
    data = journal.load(attempt)
    data["obligations"]["obligation"] = "broken"
    attempt.write_text(json.dumps(data))
    with pytest.raises(journal.JournalError, match="obligation malformed"):
        journal.load(attempt)


def test_malformed_replacement_history_holds(attempt):
    data = journal.load(attempt)
    data["obligations"]["obligation"]["attempts"] = "broken"
    attempt.write_text(json.dumps(data))
    with pytest.raises(journal.JournalError, match="attempt history malformed"):
        journal.load(attempt)


def test_existing_marker_serializes_ticks(attempt):
    with journal.single_writer(attempt):
        with pytest.raises(journal.JournalError, match="writer unavailable"):
            with journal.single_writer(attempt):
                pytest.fail("a second tick acquired the same journal marker")


def test_latest_status_uses_occurred_at_even_for_rollback():
    body = {"statusHistory": [
        {"status": "processing", "occurred_at": "2026-01-02T00:00:00Z"},
        {"status": "outgoing_payment_sent", "occurred_at": "2026-01-01T00:00:00Z"},
    ]}
    assert wise_reconcile.latest_status(body)[0] == "processing"


def test_same_time_conflicting_latest_status_is_unknown():
    with pytest.raises(wise_reconcile.TransferReadError, match="ambiguous"):
        wise_reconcile.latest_status({"statusHistory": [
            {"status": "processing", "occurred_at": "2026-01-01T00:00:00Z"},
            {"status": "outgoing_payment_sent", "occurred_at": "2026-01-01T00:00:00Z"},
        ]})


@pytest.mark.parametrize("status", sorted(wise_reconcile.TRANSFER_STATUSES))
def test_only_confirmed_transfer_status_records_paid(attempt, status):
    journal.record_transfer_created(attempt, "obligation", 3)
    first = journal.record_wise_status(
        attempt, "obligation", 3, status, "2026-01-01T00:00:00+00:00"
    )
    entry = journal.load(attempt)["obligations"]["obligation"]
    assert first is (status == "outgoing_payment_sent")
    assert (entry.get("outcome_status") == "sent") is first
    expected = ("done" if first else "failed" if status in ("cancelled", "funds_refunded")
                else "funding_pending")
    assert entry["state"] == expected


def test_bounce_then_wise_self_recovers_without_new_submission(attempt):
    journal.record_transfer_created(attempt, "obligation", 3)
    assert not journal.record_wise_status(
        attempt, "obligation", 3, "bounced_back", "2026-01-01T00:01:00+00:00"
    )
    assert journal.record_wise_status(
        attempt, "obligation", 3, "outgoing_payment_sent", "2026-01-01T13:00:00+00:00"
    )
    assert not journal.record_wise_status(
        attempt, "obligation", 3, "outgoing_payment_sent", "2026-01-01T13:00:00+00:00"
    )
    assert journal.load(attempt)["obligations"]["obligation"]["outcome_status"] == "sent"


def test_later_return_preserves_paid_history_and_records_discrepancy(attempt):
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(
        attempt, "obligation", 3, "outgoing_payment_sent", "2026-01-01T00:00:00+00:00"
    )
    journal.record_wise_status(
        attempt, "obligation", 3, "funds_refunded", "2026-01-02T00:00:00+00:00"
    )
    entry = journal.load(attempt)["obligations"]["obligation"]
    assert entry["outcome_status"] == "sent"
    assert entry["discrepancy_status"] == "funds_refunded"


def test_stale_return_read_does_not_raise_a_false_discrepancy(attempt, monkeypatch):
    import monedula

    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(
        attempt, "obligation", 3, "outgoing_payment_sent", "2026-01-02T00:00:00+00:00"
    )
    journal.mark_wise_effects_applied(attempt, "obligation", 3)
    journal.schedule_wise_check(attempt, "obligation", time.time() - 1)
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_wise_get", lambda *_: {
        "status": "bounced_back", "updated": "2026-01-01T00:00:00Z"
    })
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda message, **kw: alerts.append(message))
    assert monedula._reconcile_wise_entries([])
    assert not alerts
    assert journal.load(attempt)["obligations"]["obligation"].get("discrepancy_status") is None


def test_replacement_requires_terminal_failure_and_new_approval(attempt):
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(
        attempt, "obligation", 3, "bounced_back", "2026-01-01T00:01:00+00:00"
    )
    with pytest.raises(journal.JournalError, match="terminal failure"):
        journal.record_approved_replacement_intent(
            attempt, "obligation", "new-approval", generation=2
        )
    journal.record_wise_status(
        attempt, "obligation", 3, "funds_refunded", "2026-01-02T00:00:00+00:00"
    )
    with pytest.raises(journal.JournalError, match="fresh approval"):
        journal.record_approved_replacement_intent(
            attempt, "obligation", "approval", generation=1
        )
    journal.record_approved_replacement_intent(
        attempt, "obligation", "new-approval", generation=2
    )
    entry = journal.load(attempt)["obligations"]["obligation"]
    assert entry["attempt"] == 2 and entry["state"] == "intent"
    assert entry["attempts"][0]["failure_status"] == "funds_refunded"
    assert entry["attempts"][0]["transfer_id"] == 3
    assert entry["customer_transaction_id"] != entry["attempts"][0]["customer_transaction_id"]


@pytest.mark.parametrize("body", [None, {}, {"statusHistory": []},
                                  {"statusHistory": [{"status": "processing"}]},
                                  {"statusHistory": [{"status": "processing",
                                                      "occurred_at": "2026-01-01T00:00:00Z"}],
                                   "hasMore": True}])
def test_malformed_transfer_body_holds(body):
    with pytest.raises(wise_reconcile.TransferReadError):
        wise_reconcile.read_transfer_status("synthetic", 3, lambda *_: body)


def test_transport_failure_holds():
    def fail(*_):
        raise TimeoutError("synthetic timeout")

    with pytest.raises(TimeoutError):
        wise_reconcile.read_transfer_status("synthetic", 3, fail)


def test_discrepancy_report_is_read_only_and_unmatched_without_id():
    rows = {"old": {"state": "done", "outcome_status": "sent"},
            "known": {"state": "done", "transfer_id": 3}}
    before = repr(rows)
    report = wise_reconcile.discrepancy_report(
        rows, {3: {"status": "funds_refunded", "updated": "2026-01-02T00:00:00Z"}}
    )
    assert {row["obligation"]: row["category"] for row in report} == {
        "old": "unmatched", "known": "returned"
    }
    assert repr(rows) == before


def test_report_command_does_not_edit_journal_or_marker(attempt, tmp_path, monkeypatch, capsys):
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(
        attempt, "obligation", 3, "outgoing_payment_sent", "2026-01-01T00:00:00+00:00"
    )
    marker = attempt.with_name(journal.MARKER_NAME)
    before = (attempt.read_bytes(), marker.read_bytes())
    snapshots = tmp_path / "transfer_snapshots.json"
    snapshots.write_text(json.dumps({
        "coverage": {"complete": True, "from": "2026-01-01", "to": "2026-01-31"},
        "transfers": {"3": {"status": "outgoing_payment_sent",
                            "updated": "2026-01-02T00:00:00Z"}},
    }))
    monkeypatch.setattr(sys, "argv", ["wise_reconcile.py", "report", "--journal",
                                  str(attempt), "--transfer-snapshots", str(snapshots)])
    assert wise_reconcile._report_command() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["rows"][0]["category"] == "confirmed sent"
    assert (attempt.read_bytes(), marker.read_bytes()) == before


def test_email_copy_for_pending_transfer_forbids_another_payment():
    import consent_email

    item = consent_email.PendingItem(
        handler_name="synthetic", label="Synthetic", amount_eur=1,
        payment_date="2026-01-01", is_calendar=True, token="TOKEN1",
        obligation="synthetic-obligation",
    )
    _, body = consent_email.build_request_body(
        [item], superseded=False, unknown={"synthetic-obligation"}
    )
    assert "outcome unknown; do not pay again" in body
    assert "already paid" not in body


def test_monitor_schedule_extends_to_day_thirty():
    confirmed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert wise_reconcile.next_check_at(confirmed, confirmed).date().isoformat() == "2026-01-02"
    assert wise_reconcile.next_check_at(confirmed, datetime(2026, 2, 1, tzinfo=timezone.utc)) is None


def test_reconciliation_recovers_unscheduled_confirmed_transfer(attempt, monkeypatch):
    import monedula

    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(
        attempt, "obligation", 3, "outgoing_payment_sent", datetime.now(timezone.utc).isoformat()
    )
    journal.mark_wise_effects_applied(attempt, "obligation", 3)
    monkeypatch.setattr("consent_email.STATE_FILE", attempt.parent / "consent.json")
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    reads = []
    monkeypatch.setattr(wise, "_wise_get", lambda *_: reads.append(3) or {
        "status": "outgoing_payment_sent", "updated": datetime.now(timezone.utc).isoformat()
    })
    monkeypatch.setattr(monedula, "_notify", lambda *a, **kw: None)

    assert monedula._reconcile_wise_entries([])
    due = journal.load(attempt)["obligations"]["obligation"]["next_check_at"]
    assert due > time.time()
    assert reads == []
    journal.schedule_wise_check(attempt, "obligation", time.time() - 1)
    assert monedula._reconcile_wise_entries([])
    assert reads == [3]


def test_reconciliation_confirms_and_applies_effects_once(attempt, monkeypatch):
    import monedula
    from handlers.payment_profile import PaymentProfile

    profile = PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=["synthetic"],
        payment_type="wise", amount_eur=1, wise_recipient_id=5,
        wise_recipient_name="Synthetic", neotoma_task_id="synthetic-task",
    )
    handler = wise.WiseTransferHandler(profile)
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_funding_pending(attempt, "obligation", 3, "COMPLETED")
    statuses = iter(["incoming_payment_waiting", "outgoing_payment_sent"])
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_wise_get", lambda *_: {
        "status": next(statuses), "updated": datetime.now(timezone.utc).isoformat()
    })
    effects = []
    alerts = []
    monkeypatch.setattr(wise, "_update_task", lambda p, r: effects.append(r) or True)
    monkeypatch.setattr(monedula, "_notify", lambda message, **kw: alerts.append(message))
    assert monedula._reconcile_wise_entries([handler])
    assert effects == []
    assert journal.session_state(journal.load(attempt), "synthetic:session") is None
    assert monedula._reconcile_wise_entries([handler])
    assert journal.load(attempt)["obligations"]["obligation"]["outcome_status"] == "sent"
    assert any("confirmed sent" in alert for alert in alerts)
    assert len(effects) == 1
    assert journal.session_state(journal.load(attempt), "synthetic:session") == "settled"
    assert monedula._reconcile_wise_entries([handler])
    assert len(effects) == 1


@pytest.mark.parametrize("one_off", [False, True])
def test_confirmed_read_changes_real_task_fields_only_once(attempt, monkeypatch, one_off):
    import monedula
    from fake_neotoma import FakeNeotoma
    from handlers import task_writes
    from handlers.payment_profile import PaymentProfile

    entities = {"synthetic-task": {"status": "awaiting_approval", "notes": "prior"}}
    if one_off:
        entities["synthetic-profile"] = {"status": "active"}
    fake = FakeNeotoma(entities)
    profile = PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=[] if one_off else ["synthetic"],
        payment_type="wise", amount_eur=1, neotoma_task_id="synthetic-task",
        one_off=one_off, entity_id="synthetic-profile" if one_off else "",
    )
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_funding_pending(attempt, "obligation", 3, "COMPLETED")
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_wise_get", lambda *_: {
        "status": "outgoing_payment_sent", "updated": datetime.now(timezone.utc).isoformat()
    })
    monkeypatch.setattr(task_writes, "make_client", fake.connection)
    monkeypatch.setattr(wise, "_find_next_event_due_date", lambda *_: "2026-12-01")
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None)
    assert monedula._reconcile_wise_entries([wise.WiseTransferHandler(profile)])
    assert "transfer_id=3" in fake.entities["synthetic-task"]["notes"]
    if one_off:
        assert fake.entities["synthetic-task"]["status"] == "done"
        assert fake.entities["synthetic-profile"]["status"] == "archived"
    else:
        assert fake.entities["synthetic-task"]["due_date"] == "2026-12-01"
        assert fake.entities["synthetic-task"]["status"] == "awaiting_approval"
    first_posts = len(fake.posts)
    assert monedula._reconcile_wise_entries([wise.WiseTransferHandler(profile)])
    assert len(fake.posts) == first_posts


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), RuntimeError("HTTP 401"),
                                     RuntimeError("HTTP 503")])
def test_transfer_read_failure_cannot_settle(attempt, monkeypatch, failure):
    import monedula

    journal.record_transfer_created(attempt, "obligation", 3)
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_wise_get", lambda *_: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None)
    assert not monedula._reconcile_wise_entries([])
    assert journal.obligation_state(journal.load(attempt), "obligation") == "intent"


@pytest.mark.parametrize("lost_response", [None, "fund", "create"])
def test_default_approval_pending_then_confirmed_updates_session_once(
    approval_env, monkeypatch, lost_response
):
    from handlers.payment_profile import PaymentProfile

    world, approvals = approval_env
    world.handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="YOGA", label="Synthetic", calendar_keywords=["yoga"],
        payment_type="wise", amount_eur=10, wise_reference="ref",
        wise_recipient_id=5, wise_recipient_name="Synthetic",
        neotoma_task_id="synthetic-task",
    ))
    world.handler.recipient_status_lookup = lambda token, rid: {"id": rid, "active": True}
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_get_wise_profile_id", lambda *_: 1)
    monkeypatch.setattr(wise, "_create_quote", lambda *_: "quote")
    calls = []

    def create(*args):
        calls.append("create")
        if lost_response == "create":
            raise TimeoutError("synthetic lost create response")
        return 3

    def fund(*args):
        calls.append("fund")
        if lost_response == "fund":
            raise TimeoutError("synthetic lost funding response")
        return {"status": "CREATED"}

    monkeypatch.setattr(wise, "_create_transfer", create)
    monkeypatch.setattr(wise, "_fund_transfer", fund)
    monkeypatch.setattr(wise, "_wise_get", lambda *_: {
        "status": "outgoing_payment_sent", "updated": datetime.now(timezone.utc).isoformat()
    })
    effects = []
    monkeypatch.setattr(wise, "_update_task", lambda p, r: effects.append(r) or True)
    world.tick()
    _approve(approvals)
    world.tick(minutes=15)
    key = approvals.only["payment_key"]
    assert journal.obligation_state(world.journal, key) == (
        "intent" if lost_response else "funding_pending"
    )
    assert journal.session_state(world.journal, "yoga:evt-1") == "pending"
    expected_calls = ["create"] if lost_response == "create" else ["create", "fund"]
    assert calls == expected_calls and effects == []
    world.tick(minutes=15)
    if lost_response == "create":
        assert journal.obligation_state(world.journal, key) == "intent"
        assert journal.session_state(world.journal, "yoga:evt-1") == "pending"
        assert calls == ["create"] and effects == []
        return
    assert journal.obligation_state(world.journal, key) == "done"
    assert journal.session_state(world.journal, "yoga:evt-1") == "settled"
    assert len(effects) == 1
    world.tick(minutes=15)
    assert calls == expected_calls and len(effects) == 1


def test_telegram_pending_confirmation_never_closes_session_or_resends(
    sent_messages, monkeypatch
):
    import consent_email
    import monedula
    from handlers.payment_profile import PaymentProfile

    handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=["synthetic"],
        payment_type="wise", amount_eur=1, wise_reference="ref",
        wise_recipient_id=5, wise_recipient_name="Synthetic",
        neotoma_task_id="synthetic-task",
    ))
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda text, **kw: alerts.append(text))
    handler.recipient_status_lookup = lambda token, rid: {"id": rid, "active": True}
    _install_handlers(monkeypatch, [handler])
    event = _event("evt-1", summary="synthetic",
                   end=datetime.now(timezone.utc) - timedelta(hours=1))
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [event])
    monkeypatch.setattr(monedula, "telegram_poll_approval", lambda *a, **k:
                        monedula.TelegramPollResult(kind="reply", text="attended synthetic"))
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_get_wise_profile_id", lambda *_: 1)
    monkeypatch.setattr(wise, "_create_quote", lambda *_: "quote")
    calls = []
    monkeypatch.setattr(wise, "_create_transfer", lambda *args: calls.append("create") or 3)
    monkeypatch.setattr(wise, "_fund_transfer", lambda *args: calls.append("fund") or
                        {"status": "COMPLETED"})
    monkeypatch.setattr(wise, "_wise_get", lambda *_: {
        "status": "incoming_payment_waiting", "updated": datetime.now(timezone.utc).isoformat()
    })
    assert monedula.main()
    path = consent_email.journal_path()
    assert list(journal.load(path)["obligations"].values())[0]["state"] == "funding_pending"
    assert journal.session_state(journal.load(path), "synthetic:evt-1") == "pending"
    assert any("submitted; not confirmed; do not pay again" in msg for msg in sent_messages)
    assert monedula.main() is False  # unresolved transfer remains a hold
    assert any("Monedula is checking" in text and "do not pay again" in text
               for text in alerts)
    assert not any("result was never recorded" in text for text in alerts)
    assert calls == ["create", "fund"]
    assert journal.session_state(journal.load(path), "synthetic:evt-1") == "pending"


@pytest.mark.parametrize("rail_status", ["incoming_payment_waiting", "funds_refunded", "cancelled"])
def test_oneoff_next_day_holds_same_attempt_on_default_approval(
    approval_env, monkeypatch, rail_status
):
    """A new tick date cannot mint a second consent or transfer for one invoice."""
    import consent_email
    import monedula
    from handlers.payment_profile import PaymentProfile

    world, approvals = approval_env
    world.handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="INVOICE", label="Synthetic invoice", calendar_keywords=[],
        payment_type="wise", amount_eur=10, wise_reference="ref",
        due_date="2026-01-01", one_off=True, wise_recipient_id=5,
        wise_recipient_name="Synthetic", neotoma_task_id="synthetic-task",
    ))
    world.handler.recipient_status_lookup = lambda token, rid: {"id": rid, "active": True}
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda text, **kw: alerts.append(text))
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_get_wise_profile_id", lambda *_: 1)
    monkeypatch.setattr(wise, "_create_quote", lambda *_: "quote")
    calls = []
    monkeypatch.setattr(wise, "_create_transfer", lambda *args: calls.append("create") or 3)
    monkeypatch.setattr(wise, "_fund_transfer", lambda *args: calls.append("fund") or
                        {"status": "COMPLETED"})
    monkeypatch.setattr(wise, "_wise_get", lambda *_: {
        "status": rail_status, "updated": datetime.now(timezone.utc).isoformat()
    })
    first = consent_email.build_pending_items(
        [(world.handler, world.handler.matches([]))], "2026-11-18"
    )[0]
    second = consent_email.build_pending_items(
        [(world.handler, world.handler.matches([]))], "2026-11-19"
    )[0]
    assert first.obligation == second.obligation
    world.tick()
    _approve(approvals, answer="yes invoice")
    world.tick(minutes=15)
    assert calls == ["create", "fund"]
    assert world.tick(minutes=1440) is False
    assert approvals.writes == 1
    assert calls == ["create", "fund"]
    assert any("do not pay again" in text.lower() for text in alerts)
    if rail_status == "incoming_payment_waiting":
        assert any("Monedula is checking" in text for text in alerts)
    state = journal.load(consent_email.journal_path())["obligations"][first.obligation]["state"]
    assert state == ("failed" if rail_status in {"funds_refunded", "cancelled"} else "funding_pending")


def test_old_tick_date_oneoff_key_still_holds(monkeypatch, tmp_path):
    import monedula
    from handlers.payment_profile import PaymentProfile
    from handlers.wise_transfer import WiseTransferHandler
    from consent_email import obligation_key

    handler = WiseTransferHandler(PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=[],
        payment_type="wise", amount_eur=1, due_date="2026-01-01",
    ))
    attempt = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(attempt)
    old_key = obligation_key(handler, "2026-01-02", "")
    journal.record_intent(attempt, old_key, "old", generation=2,
                          rail="wise", handler_name=handler.name)
    journal.record_transfer_created(attempt, old_key, 3)
    journal.record_funding_pending(attempt, old_key, 3, "COMPLETED")
    monkeypatch.setattr("consent_email.STATE_FILE", attempt.parent / "consent.json")
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda text, **kw: alerts.append(text))
    assert monedula._apply_session_journal(
        [(handler, [{"trigger": "due_date", "due_date": "2026-01-01"}])],
        reconcile=True,
    ) == []
    assert any("do not pay again" in text for text in alerts)


@pytest.mark.parametrize("failure", ["account", "quote", "token", "create", "rejected"])
def test_wise_submission_boundary_and_alert(attempt, monkeypatch, failure):
    import monedula
    from handlers.payment_profile import PaymentProfile

    handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=[],
        payment_type="wise", amount_eur=1, wise_recipient_id=5,
        wise_recipient_name="Synthetic",
    ))
    handler.recipient_status_lookup = lambda token, rid: {"id": rid, "active": True}
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_get_wise_profile_id", lambda *_: 1)
    monkeypatch.setattr(wise, "_get_or_create_recipient", lambda *a, **k:
                        (_ for _ in ()).throw(RuntimeError("account failed")) if failure == "account" else 5)
    monkeypatch.setattr(wise, "_create_quote", lambda *a:
                        (_ for _ in ()).throw(RuntimeError("quote failed")) if failure == "quote" else "quote")
    calls = []
    def create(*args):
        calls.append("create")
        if failure == "create":
            raise TimeoutError("unknown response")
        return 3
    monkeypatch.setattr(wise, "_create_transfer", create)
    monkeypatch.setattr(wise, "_fund_transfer", lambda *a: calls.append("fund") or
                        {"status": "REJECTED"})
    if failure == "token":
        monkeypatch.delenv("WISE_API_TOKEN")
    result = handler.execute({"_wise_journal": (attempt, "obligation")})
    entry = journal.load(attempt)["obligations"]["obligation"]
    item = type("Item", (), {"handler_name": "synthetic", "payment_date": "2026-01-01",
                              "amount_eur": 1, "obligation": "obligation"})()
    alert = monedula._outcome_alert(item, result)[1]
    if failure in {"account", "quote", "token"}:
        assert result["status"] == "not_submitted"
        assert entry["state"] == "failed" and entry["failure_status"] == "not_submitted"
        assert "not submitted" in alert and "no payment was made" in alert
        assert calls == []
    elif failure == "create":
        assert result["status"] == "funding_pending"
        assert entry["state"] == "intent" and entry.get("transfer_id") is None
        assert "submitted" in alert and "do not pay again" in alert
        assert calls == ["create"]
    else:
        assert result["status"] == "funding_rejected"
        assert entry["state"] == "intent" and entry["transfer_id"] == 3
        assert "rejected funding" in alert and calls == ["create", "fund"]
    with pytest.raises(journal.JournalError, match="already recorded"):
        journal.record_intent(attempt, "obligation", "new-approval", generation=2, rail="wise")


def test_mixed_offset_status_and_recovered_discrepancy(attempt):
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(attempt, "obligation", 3, "outgoing_payment_sent",
                               "2026-01-01T10:00:00+00:00")
    journal.record_wise_status(attempt, "obligation", 3, "funds_refunded",
                               "2026-01-01T12:00:00+00:00")
    assert journal.load(attempt)["obligations"]["obligation"]["discrepancy_status"] == "funds_refunded"
    journal.record_wise_status(attempt, "obligation", 3, "outgoing_payment_sent",
                               "2026-01-01T13:00:00+01:00")
    assert journal.load(attempt)["obligations"]["obligation"].get("discrepancy_status") is None
    assert not journal.record_wise_status(attempt, "obligation", 3, "processing",
                                          "2026-01-01T14:00:00+03:00")
    assert journal.load(attempt)["obligations"]["obligation"]["last_status"] == "outgoing_payment_sent"


def test_btc_legacy_done_non_sent_stays_done():
    assert journal.obligation_state({"obligations": {"btc": {
        "state": "done", "outcome_status": "manual_required"
    }}}, "btc") == "done"


@pytest.mark.parametrize("journal_condition", ["not_initialized", "missing", "malformed", "locked"])
def test_entrypoint_journal_failure_uses_fixed_alert_and_nonzero(
    tmp_path, monkeypatch, journal_condition
):
    import monedula
    import consent_email
    import types

    path = tmp_path / journal.JOURNAL_NAME
    if journal_condition != "not_initialized":
        journal.init_journal(path)
    if journal_condition == "missing":
        path.unlink()
    elif journal_condition == "malformed":
        path.write_text("not json")
    monkeypatch.setattr(consent_email, "STATE_FILE", tmp_path / "consent.json")
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda msg, **kw: alerts.append((msg, kw)))
    monkeypatch.setattr(monedula, "_record_notify_health", lambda: None)
    strandings_checked = []
    monkeypatch.setattr(monedula, "escalate_strandings",
                        lambda rows: strandings_checked.append(rows))
    monkeypatch.setitem(sys.modules, "handlers", types.SimpleNamespace(
        load_handlers=lambda rows: []
    ))
    if journal_condition == "locked":
        with journal.single_writer(path):
            code = monedula.run_entrypoint()
    else:
        code = monedula.run_entrypoint()
    assert code == 1
    assert strandings_checked == [[]]
    assert any("payment journal" in msg and kw.get("priority") == "blocker"
               for msg, kw in alerts)
    if journal_condition in {"not_initialized", "missing"}:
        assert any(kw.get("dedupe_key") == "monedula:payment_journal_missing"
                   for _, kw in alerts)
    assert not any("fatal error" in msg for msg, _ in alerts)


def test_reconcile_read_failure_makes_main_nonclean(tmp_path, monkeypatch, caplog):
    import monedula
    import consent_email
    import types
    from handlers.payment_profile import PaymentProfile

    path = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(path)
    journal.record_intent(path, "obligation", "approval", generation=1,
                          rail="wise", handler_name="synthetic")
    journal.record_transfer_created(path, "obligation", 3)
    journal.record_funding_pending(path, "obligation", 3, "COMPLETED")
    handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", payment_type="wise",
        amount_eur=1, calendar_keywords=[], due_date="",
    ))
    monkeypatch.setattr(consent_email, "STATE_FILE", tmp_path / "consent.json")
    monkeypatch.setattr(monedula, "STATE_FILE", tmp_path / "last_run")
    monkeypatch.setitem(sys.modules, "handlers", types.SimpleNamespace(
        load_handlers=lambda rows: [handler]
    ))
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_wise_get", lambda *a:
                        (_ for _ in ()).throw(RuntimeError("read failed")))
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda msg, **kw: alerts.append(msg))
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [])
    monkeypatch.setattr(monedula, "fetch_due_payment_tasks", lambda *a, **k: [])
    monkeypatch.setattr(monedula, "escalate_strandings", lambda rows: rows)
    assert monedula.main() is False
    assert any("read failed" in msg and "do not pay again" in msg for msg in alerts)
    monkeypatch.setattr(monedula, "_record_notify_health", lambda: None)
    assert monedula.run_entrypoint() == 1
    assert "Monedula run incomplete" in caplog.text
    assert "monedula run complete" not in alerts


def test_reconcile_schedule_write_failure_uses_journal_alert(attempt, monkeypatch):
    import monedula
    import consent_email
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_funding_pending(attempt, "obligation", 3, "COMPLETED")
    monkeypatch.setattr(consent_email, "STATE_FILE", attempt.parent / "consent.json")
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_wise_get", lambda *a: {
        "status": "bounced_back", "updated": datetime.now(timezone.utc).isoformat()
    })
    monkeypatch.setattr(journal, "schedule_wise_check", lambda *a:
                        (_ for _ in ()).throw(journal.JournalError("synthetic write failure")))
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda msg, **kw: alerts.append(msg))
    assert monedula._reconcile_wise_entries([]) is False
    assert any("payment journal could not be read or written" in msg for msg in alerts)


def test_daemon_env_is_loaded_before_module_constants_in_clean_process(tmp_path):
    import os
    import subprocess
    import sys

    fake = tmp_path / "fake.env"
    expected = {
        "TELEGRAM_BOT_TOKEN": "fake-bot", "TELEGRAM_CHAT_ID": "fake-chat",
        "TELEGRAM_ALLOWED_USER_ID": "fake-user", "TELEGRAM_TOPIC_MONEDULA": "fake-topic",
        "NEOTOMA_BEARER_TOKEN": "fake-bearer", "NEOTOMA_BASE_URL": "https://example.invalid",
        "MONEDULA_DEAD_GATE_THRESHOLD": "7",
    }
    fake.write_text("".join(f"{k}={v}\n" for k, v in expected.items()))
    repo = Path(__file__).resolve().parents[3]
    code = (
        "import json, monedula; "
        "print(json.dumps({k: str(getattr(monedula, k)) for k in "
        + repr(list(expected)) + "}))"
    )
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""),
           "ATELES_DOTENV_FILE": str(fake), "PYTHONPATH": f"{repo}:{Path(__file__).parent}"}
    completed = subprocess.run([sys.executable, "-c", code], env=env,
                               text=True, capture_output=True, check=True)
    assert json.loads(completed.stdout.splitlines()[-1]) == expected


def test_email_second_tick_pending_sends_no_new_consent(world, monkeypatch):
    import consent_email
    import monedula
    from handlers.payment_profile import PaymentProfile
    from lib.approval.email_channel import ReadRepliesOutcome
    from lib.approval.tokens import subject_marker

    world.handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="INVOICE", label="Synthetic invoice", payment_type="wise",
        calendar_keywords=[],
        amount_eur=1, due_date="2026-01-01", one_off=True,
        wise_recipient_id=5, wise_recipient_name="Synthetic",
    ))
    world.handler.recipient_status_lookup = lambda token, rid: {"id": rid, "active": True}
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_get_wise_profile_id", lambda *_: 1)
    monkeypatch.setattr(wise, "_create_quote", lambda *_: "quote")
    calls = []
    monkeypatch.setattr(wise, "_create_transfer", lambda *a: calls.append("create") or 3)
    monkeypatch.setattr(wise, "_fund_transfer", lambda *a: calls.append("fund") or
                        {"status": "COMPLETED"})
    monkeypatch.setattr(wise, "_wise_get", lambda *a: {
        "status": "incoming_payment_waiting", "updated": datetime.now(timezone.utc).isoformat()
    })
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda text, **kw: alerts.append(text))
    world.tick()
    assert len(world.sends) == 1
    monkeypatch.setattr(consent_email, "read_replies_with_status", lambda tokens, **kw:
                        ReadRepliesOutcome(kind="ok", texts=[
                            f"RE: {subject_marker(t)}\nAPPROVE" for t in tokens
                        ]))
    world.tick(minutes=15)
    assert calls == ["create", "fund"]
    world.tick(minutes=1440)
    assert len(world.sends) == 1
    assert calls == ["create", "fund"]
    assert any("Monedula is checking" in text and "do not pay again" in text
               for text in alerts)


def test_reconcile_does_not_read_terminal_failure_each_tick(attempt, monkeypatch):
    import monedula
    import consent_email
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(attempt, "obligation", 3, "cancelled",
                               "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(consent_email, "STATE_FILE", attempt.parent / "consent.json")
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_wise_get", lambda *a:
                        pytest.fail("terminal failure must not be read again"))
    assert monedula._reconcile_wise_entries([])
    assert monedula._reconcile_wise_entries([])


def test_overdue_pending_transfer_alerts_without_another_submission(attempt, monkeypatch):
    import monedula
    import consent_email
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_funding_pending(attempt, "obligation", 3, "COMPLETED")
    data = journal.load(attempt)
    data["obligations"]["obligation"]["funding_at"] = (
        time.time() - monedula.WISE_PENDING_ALERT_AFTER_SECONDS - 1
    )
    journal._write(attempt, data)
    monkeypatch.setattr(consent_email, "STATE_FILE", attempt.parent / "consent.json")
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_wise_get", lambda *a: {
        "status": "processing", "updated": datetime.now(timezone.utc).isoformat()
    })
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda text, **kw: alerts.append(text))
    assert monedula._reconcile_wise_entries([])
    assert any("two days" in text and "do not pay again" in text for text in alerts)
    assert journal.load(attempt)["obligations"]["obligation"]["state"] == "funding_pending"


def test_confirmed_oneoff_profile_is_reloaded_before_matching(tmp_path, monkeypatch):
    import monedula
    import consent_email
    import types
    from handlers.payment_profile import PaymentProfile

    journal.init_journal(tmp_path / journal.JOURNAL_NAME)
    monkeypatch.setattr(consent_email, "STATE_FILE", tmp_path / "consent.json")
    handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="INVOICE", label="Synthetic", payment_type="wise", amount_eur=1,
        calendar_keywords=[],
        due_date="2026-01-01", one_off=True,
    ))
    matched = []
    monkeypatch.setattr(handler, "matches", lambda events: matched.append(events) or [])
    active = [handler]
    monkeypatch.setitem(sys.modules, "handlers", types.SimpleNamespace(
        load_handlers=lambda rows: list(active)
    ))
    monkeypatch.setattr(monedula, "_reconcile_wise_entries", lambda handlers:
                        active.clear() or True)
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [])
    monkeypatch.setattr(monedula, "fetch_due_payment_tasks", lambda *a, **k: [])
    monkeypatch.setattr(monedula, "escalate_strandings", lambda rows: rows)
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None)
    monedula.main()
    assert matched == []


def test_not_submitted_attempt_can_only_be_reserved_with_new_approval(attempt):
    journal.record_wise_not_submitted(attempt, "obligation")
    with pytest.raises(journal.JournalError, match="fresh approval"):
        journal.record_approved_replacement_intent(
            attempt, "obligation", "approval", generation=2
        )
    journal.record_approved_replacement_intent(
        attempt, "obligation", "second-approval", generation=2
    )
    entry = journal.load(attempt)["obligations"]["obligation"]
    assert entry["state"] == "intent" and entry["attempt"] == 2
    assert entry["attempts"][0]["failure_status"] == "not_submitted"
    assert entry["attempts"][0].get("transfer_id") is None
    assert journal.load(attempt)["consumed_tokens"]["second-approval"] == "obligation"


def test_effect_retry_after_due_date_failure_does_not_duplicate_note(monkeypatch):
    from fake_neotoma import FakeNeotoma
    from handlers import task_writes
    from handlers.payment_profile import PaymentProfile

    fake = FakeNeotoma({"synthetic-task": {"title": "Synthetic task"}})
    monkeypatch.setattr(task_writes, "make_client", fake.connection)
    monkeypatch.setattr(wise, "_find_task_id", lambda profile: "synthetic-task")
    monkeypatch.setattr(wise, "_find_next_event_due_date", lambda profile: "2026-12-01")
    original = task_writes.write_field
    monkeypatch.setattr(task_writes, "write_field", lambda client, base, task, typ, field, value:
                        False if field == "due_date" else original(client, base, task, typ, field, value))
    profile = PaymentProfile(prefix="SYNTHETIC", label="Synthetic", payment_type="wise",
                             amount_eur=1, calendar_keywords=["synthetic"])
    result = {"status": "sent", "transfer_id": 3, "paid_at_date": "2026-01-01"}
    assert wise._update_task(profile, result) is False
    assert wise._update_task(profile, result) is False
    notes = fake.entities["synthetic-task"]["notes"]
    assert notes.count("Wise transfer_id=3") == 1
    assert len(fake.writes("notes")) == 1
