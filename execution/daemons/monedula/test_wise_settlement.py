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
from test_session_payments import _event  # noqa: E402


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


@pytest.mark.parametrize("status", ["REJECTED", "SOMETHING_NEW", None])
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
    assert "submitted; not confirmed; do not pay again" in body
    assert "already paid" not in body


def test_monitor_schedule_extends_to_day_thirty():
    confirmed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert wise_reconcile.next_check_at(confirmed, confirmed).date().isoformat() == "2026-01-02"
    assert wise_reconcile.next_check_at(confirmed, datetime(2026, 2, 1, tzinfo=timezone.utc)) is None


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
    assert calls == ["create", "fund"]
    assert journal.session_state(journal.load(path), "synthetic:evt-1") == "pending"
