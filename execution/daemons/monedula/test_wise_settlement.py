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
    assert journal.obligation_state(data, "synthetic") == "failed"


def test_wise_execute_without_journal_context_never_calls_wise(monkeypatch):
    from handlers.payment_profile import PaymentProfile

    handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=[],
        payment_type="wise", amount_eur=1, wise_recipient_id=5,
        wise_recipient_name="Synthetic", entity_id="synthetic-profile",
    ))
    monkeypatch.setattr(wise, "_wise_get", lambda *a, **kw:
                        pytest.fail("Wise read without journal"))
    monkeypatch.setattr(wise, "_wise_post", lambda *a, **kw:
                        pytest.fail("Wise write without journal"))
    monkeypatch.setattr(wise, "_create_transfer", lambda *a, **kw:
                        pytest.fail("Wise create without journal"))
    monkeypatch.setattr(wise, "_fund_transfer", lambda *a, **kw:
                        pytest.fail("Wise fund without journal"))
    for _ in range(2):
        result = handler.execute({})
        assert result["status"] == "not_submitted"
        assert "journal context missing" in result["error"]
        assert "do not pay again" in result["error"]
        assert "no Wise call made" in handler.format_confirmation(result)


def test_wise_direct_helper_requires_durable_intent_before_network(monkeypatch):
    monkeypatch.setattr(wise, "_wise_get", lambda *a, **kw:
                        pytest.fail("Wise read without journal"))
    monkeypatch.setattr(wise, "_wise_post", lambda *a, **kw:
                        pytest.fail("Wise write without journal"))
    with pytest.raises(journal.JournalError, match="journal context missing"):
        wise._execute_wise_transfer("synthetic-token", "", "Synthetic", 1, "ref")


def test_wise_create_and_fund_helpers_require_durable_intent(tmp_path, monkeypatch):
    path = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(path)
    monkeypatch.setattr(wise, "_wise_post", lambda *a, **kw:
                        pytest.fail("Wise write without durable intent"))
    with pytest.raises(journal.JournalError, match="intent unavailable"):
        wise._create_transfer("token", 2, "quote", "ref", (path, "absent"), "id")
    with pytest.raises(journal.JournalError, match="intent unavailable"):
        wise._fund_transfer("token", 1, 3, (path, "absent"))


def test_wise_same_journal_context_cannot_submit_twice(attempt, monkeypatch):
    from handlers.payment_profile import PaymentProfile

    handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=[],
        payment_type="wise", amount_eur=1, wise_recipient_id=5,
        wise_recipient_name="Synthetic", entity_id="synthetic-profile",
    ))
    handler.recipient_status_lookup = lambda token, rid: {"id": rid, "active": True}
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_get_wise_profile_id", lambda token: 1)
    writes = []

    def post(token, path, body):
        writes.append(path)
        if path.endswith("/quotes"):
            return {"id": "quote"}
        if path == "/v1/transfers":
            return {"id": 3}
        return {"status": "COMPLETED"}

    monkeypatch.setattr(wise, "_wise_post", post)
    first = handler.execute({"_wise_journal": (attempt, "obligation")})
    assert first["status"] == "funding_pending"
    assert writes.count("/v1/transfers") == 1
    assert len([path for path in writes if path.endswith("/payments")]) == 1
    second = handler.execute({"_wise_journal": (attempt, "obligation")})
    assert second["status"] == "funding_pending"
    assert "do not pay again" in second["error"]
    assert writes.count("/v1/transfers") == 1
    assert len([path for path in writes if path.endswith("/payments")]) == 1


@pytest.mark.parametrize("create_result", ["raises", "no_id"])
def test_wise_create_unknown_response_never_replays_same_intent(attempt, monkeypatch, create_result):
    from handlers.payment_profile import PaymentProfile

    handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=[],
        payment_type="wise", amount_eur=1, wise_recipient_id=5,
        wise_recipient_name="Synthetic", entity_id="synthetic-profile",
    ))
    handler.recipient_status_lookup = lambda token, rid: {"id": rid, "active": True}
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_get_wise_profile_id", lambda token: 1)
    calls = []

    def post(token, path, body):
        calls.append(path)
        if path.endswith("/quotes"):
            return {"id": "quote"}
        if path == "/v1/transfers":
            if create_result == "raises":
                raise RuntimeError("mocked lost response")
            return {}
        pytest.fail("funding reached after unknown create")

    monkeypatch.setattr(wise, "_wise_post", post)
    for _ in range(2):
        result = handler.execute({"_wise_journal": (attempt, "obligation")})
        assert result["status"] == "submission_unknown"
        assert "do not pay again" in result["error"]
    assert calls.count("/v1/transfers") == 1
    assert journal.load(attempt)["obligations"]["obligation"]["create_entered"] is True


def test_wise_create_marker_write_failure_prevents_create(attempt, monkeypatch):
    monkeypatch.setattr(wise, "_wise_post", lambda *a, **kw: pytest.fail("Wise POST reached"))
    original = journal._write

    def fail_marker(path, data):
        if data["obligations"]["obligation"].get("create_entered"):
            raise journal.JournalError("mocked marker write failure")
        return original(path, data)

    monkeypatch.setattr(journal, "_write", fail_marker)
    entry = journal.load(attempt)["obligations"]["obligation"]
    with pytest.raises(journal.JournalError, match="marker write failure"):
        wise._create_transfer("token", 2, "quote", "ref", (attempt, "obligation"),
                              entry["customer_transaction_id"])
    assert journal.load(attempt)["obligations"]["obligation"].get("create_entered") is None


def test_wise_write_helpers_only_called_inside_guarded_flow():
    import ast

    root = Path(__file__).resolve().parents[3]
    helpers = {"_create_transfer", "_fund_transfer", "_execute_wise_transfer"}
    calls = []
    for path in root.rglob("*.py"):
        if path.name.startswith("test_") or ".git" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))

        def visit(node, owner=""):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = node.name
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else ""
                )
                if name in helpers:
                    calls.append((path.relative_to(root).as_posix(), owner, name,
                                  {n.id for arg in node.args for n in ast.walk(arg)
                                   if isinstance(n, ast.Name)}))
            for child in ast.iter_child_nodes(node):
                visit(child, owner)

        visit(tree)
    source = "execution/daemons/monedula/handlers/wise_transfer.py"
    assert {(path, owner, name) for path, owner, name, _ in calls} == {
        (source, "execute", "_execute_wise_transfer"),
        (source, "_execute_wise_transfer", "_create_transfer"),
        (source, "_execute_wise_transfer", "_fund_transfer"),
    }
    for _, owner, name, names in calls:
        if name in {"_create_transfer", "_fund_transfer"}:
            assert owner == "_execute_wise_transfer" and "journal_context" in names


def test_wise_approval_key_changes_with_profile_task_and_reference():
    import consent_email
    from handlers.payment_profile import PaymentProfile

    profile = PaymentProfile(
        prefix="INVOICE", label="Synthetic invoice", calendar_keywords=[],
        payment_type="wise", amount_eur=1, due_date="2026-01-01", one_off=True,
        wise_recipient_id=5, wise_recipient_name="Synthetic", wise_reference="first",
        entity_id="first-profile", neotoma_task_id="first-task",
    )
    original = consent_email.obligation_key(wise.WiseTransferHandler(profile), "2026-01-01", "")
    profile.entity_id = "second-profile"
    changed_profile = consent_email.obligation_key(wise.WiseTransferHandler(profile), "2026-01-01", "")
    profile.neotoma_task_id = "second-task"
    changed_task = consent_email.obligation_key(wise.WiseTransferHandler(profile), "2026-01-01", "")
    profile.wise_reference = "second"
    changed_reference = consent_email.obligation_key(wise.WiseTransferHandler(profile), "2026-01-01", "")
    assert len({original, changed_profile, changed_task, changed_reference}) == 4


@pytest.fixture
def attempt(tmp_path):
    path = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(path)
    journal.record_intent(path, "obligation", "approval", generation=1, rail="wise",
                          handler_name="synthetic", session_key="synthetic:session",
                          profile_entity_id="synthetic-profile", task_id="synthetic-task",
                          approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")
    return path


_WISE_BINDING = {
    "handler_name": "synthetic", "profile_entity_id": "synthetic-profile",
    "task_id": "synthetic-task", "approved_terms_hash": "synthetic-hash",
    "payment_date": "2026-01-01", "match_id": "synthetic-match",
}


@pytest.mark.parametrize("field", sorted(_WISE_BINDING))
@pytest.mark.parametrize("value", ["", None, 42, "x" * 257])
def test_wise_intent_requires_complete_binding_before_write(tmp_path, field, value):
    path = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(path)
    binding = {**_WISE_BINDING, field: value}
    with pytest.raises(journal.JournalError, match="binding"):
        journal.record_intent(path, "obligation", "approval", generation=1,
                              rail="wise", **binding)
    data = journal.load(path)
    assert data["obligations"] == {}
    assert data["consumed_tokens"] == {}


@pytest.mark.parametrize("field", sorted(_WISE_BINDING))
@pytest.mark.parametrize("value", ["", None, 42, "x" * 257])
@pytest.mark.parametrize("route", ["create", "fund"])
def test_incomplete_stored_wise_binding_cannot_reach_post(
    attempt, monkeypatch, field, value, route
):
    if route == "fund":
        journal.record_transfer_created(attempt, "obligation", 3)
    data = journal.load(attempt)
    entry = data["obligations"]["obligation"]
    if value is None:
        entry.pop(field)
    else:
        entry[field] = value
    journal._write(attempt, data)
    posts = []
    monkeypatch.setattr(wise, "_wise_post", lambda *args: posts.append(args))
    with pytest.raises(journal.JournalError, match="intent unavailable"):
        if route == "create":
            wise._create_transfer("token", 2, "quote", "ref", (attempt, "obligation"),
                                  entry["customer_transaction_id"])
        else:
            wise._fund_transfer("token", 1, 3, (attempt, "obligation"))
    assert posts == []


def _bind_synthetic_attempt(path, profile, payment_date="2026-01-01"):
    import consent_email

    entry = journal.load(path)["obligations"]["obligation"]
    entry.update(
        profile_entity_id=profile.entity_id,
        task_id=profile.neotoma_task_id,
        approved_terms_hash=consent_email.obligation_key(
            wise.WiseTransferHandler(profile), payment_date, ""
        ),
        payment_date=payment_date,
        match_id=payment_date,
    )
    data = journal.load(path)
    data["obligations"]["obligation"] = entry
    journal._write(path, data)


def test_wise_malformed_handler_name_holds_profile(attempt):
    data = journal.load(attempt)
    data["obligations"]["obligation"]["handler_name"] = 42
    journal._write(attempt, data)
    loaded = journal.load(attempt)
    assert journal.classify_journal_row(loaded["obligations"]["obligation"])[0] == "invalid"
    assert journal.wise_replay_hold(loaded, "synthetic-profile")[0] == "obligation"


def test_confirmed_wise_effect_failure_keeps_session_pending_through_sweep(
    tmp_path, monkeypatch
):
    import consent_email
    import monedula
    from handlers.payment_profile import PaymentProfile
    from test_session_payments import _event

    profile = PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=["synthetic"],
        payment_type="wise", amount_eur=1, entity_id="synthetic-profile",
        neotoma_task_id="synthetic-task", wise_recipient_id=5,
        wise_recipient_name="Synthetic", wise_reference="original",
    )
    handler = wise.WiseTransferHandler(profile)
    event = _event("session", "synthetic")
    match = handler.matches([event])[0]
    payment_date = consent_email.event_session_date(match)
    attempt = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(attempt)
    obligation = consent_email.obligation_key(handler, payment_date, "session")
    journal.record_intent(
        attempt, obligation, "approval", generation=1, rail="wise",
        handler_name=handler.name, session_key="synthetic:session",
        profile_entity_id=profile.entity_id, task_id=profile.neotoma_task_id,
        approved_terms_hash=obligation, payment_date=payment_date, match_id=("session" or payment_date),
    )
    data = journal.load(attempt)
    entry = data["obligations"][obligation]
    journal.note_pending(attempt, "synthetic:session")
    journal.record_transfer_created(attempt, obligation, 3)
    journal.record_wise_status(
        attempt, obligation, 3, "outgoing_payment_sent", "2026-11-11T21:00:00Z"
    )
    monkeypatch.setattr(consent_email, "STATE_FILE", attempt.parent / "consent.json")
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda message, **kw: alerts.append(message))
    monkeypatch.setattr(wise, "_update_task", lambda *a, **kw: False)

    assert not monedula._apply_confirmed_wise_effects(
        attempt, obligation, entry, {profile.entity_id: handler}
    )
    assert monedula._apply_session_journal([(handler, [match])], reconcile=True) == []
    assert journal.session_state(journal.load(attempt), "synthetic:session") == "pending"
    assert monedula._apply_session_journal([], reconcile=True) == []
    assert journal.session_state(journal.load(attempt), "synthetic:session") == "pending"
    assert not journal.load(attempt)["obligations"][obligation].get("effects_applied")
    assert any("bookkeeping held" in message or "needs task bookkeeping" in message
               for message in alerts)


def test_session_sweep_rechecks_state_before_dropping(tmp_path, monkeypatch):
    import consent_email
    import monedula

    attempt = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(attempt)
    monkeypatch.setattr(consent_email, "STATE_FILE", tmp_path / "consent.json")
    journal.note_pending(attempt, "synthetic:session")
    original = journal.pending_sessions

    def settle_after_snapshot(data):
        pending = original(data)
        journal.set_session_state(attempt, "synthetic:session", journal.SESSION_SETTLED)
        return pending

    monkeypatch.setattr(journal, "pending_sessions", settle_after_snapshot)
    assert monedula._apply_session_journal([], reconcile=True) == []
    assert journal.session_state(journal.load(attempt), "synthetic:session") == "settled"


def test_settled_wise_event_with_new_id_offers_next_occurrence(approval_env, monkeypatch):
    import consent_email
    import monedula
    from handlers.payment_profile import PaymentProfile

    world, approvals = approval_env
    profile = PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", payment_type="wise",
        calendar_keywords=["yoga"], amount_eur=10, wise_reference="original",
        wise_recipient_id=5, wise_recipient_name="Synthetic",
        entity_id="synthetic-profile", neotoma_task_id="synthetic-task",
    )
    world.handler = wise.WiseTransferHandler(profile)
    old = consent_email.build_pending_items(
        [(world.handler, world.handler.matches(world.calendars["primary"]))], "2026-11-18"
    )[0]
    path = consent_email.journal_path()
    journal.record_intent(
        path, old.obligation, "old-token", generation=1, rail="wise",
        handler_name=world.handler.name, profile_entity_id=profile.entity_id,
        task_id=profile.neotoma_task_id, payment_date=old.payment_date,
        match_id=(old.match_id or old.payment_date), approved_terms_hash=old.obligation,
    )
    journal.record_transfer_created(path, old.obligation, 3)
    journal.record_wise_status(path, old.obligation, 3, "outgoing_payment_sent",
                               "2026-11-11T21:00:00Z")
    journal.mark_wise_effects_applied(path, old.obligation, 3)
    world.calendars["primary"][0]["id"] = "next-event"
    later = consent_email.build_pending_items(
        [(world.handler, world.handler.matches(world.calendars["primary"]))], "2026-11-18"
    )[0]
    assert consent_email.journal_state_for_item(journal.load(path), later) is None
    monkeypatch.setattr(monedula, "_reconcile_wise_entries", lambda *a: True)
    monkeypatch.setattr(wise, "_create_transfer", lambda *a, **k:
                        pytest.fail("consent has not been granted for the next event"))
    world.tick()
    assert approvals.writes == 1
    assert approvals.only["payment_key"] == later.obligation


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
        journal.record_intent(attempt, "obligation", "approval", generation=1, rail="wise",
                              profile_entity_id="synthetic-profile", handler_name="synthetic", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")


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
    loaded = journal.load(attempt)
    assert journal.classify_journal_row(loaded["obligations"]["obligation"])[0] == "invalid"
    assert journal.wise_replay_hold(loaded, "synthetic-profile")[0] == "obligation"


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


@pytest.mark.parametrize("stamp", [None, "not-a-date", "2026-01-01T00:00:00"])
def test_untimed_or_unorderable_status_cannot_establish_payment(stamp):
    body = {"status": "outgoing_payment_sent"}
    if stamp is not None:
        body["updated"] = stamp
    with pytest.raises(wise_reconcile.TransferReadError, match="timestamp"):
        wise_reconcile.latest_status(body)
    assert wise_reconcile.discrepancy_report(
        {"obligation": {"state": "done", "transfer_id": 3}}, {3: body}
    )[0]["category"] == "unmatched"


@pytest.mark.parametrize("prior_status", [None, "bounced_back"])
def test_untimed_status_read_keeps_wise_outcome_held(attempt, monkeypatch, prior_status):
    import monedula
    from handlers.payment_profile import PaymentProfile

    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_funding_pending(attempt, "obligation", 3, "COMPLETED")
    if prior_status:
        journal.record_wise_status(attempt, "obligation", 3, prior_status,
                                   "2026-01-01T00:00:00Z")
    before = journal.load(attempt)["obligations"]["obligation"].copy()
    handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=[],
        payment_type="wise", amount_eur=1, entity_id="synthetic-profile",
        neotoma_task_id="synthetic-task",
    ))
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_wise_get", lambda *_: {"status": "outgoing_payment_sent"})
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda text, **kw: alerts.append(text))
    assert monedula._reconcile_wise_entries([handler]) is False
    after = journal.load(attempt)["obligations"]["obligation"]
    assert after == before
    assert after["state"] == "funding_pending"
    assert after.get("outcome_status") != "sent"
    assert any("no orderable Wise timestamp" in text for text in alerts)


def test_timestamped_status_still_settles_pending_transfer(attempt):
    journal.record_transfer_created(attempt, "obligation", 3)
    status, occurred_at = wise_reconcile.latest_status({
        "status": "outgoing_payment_sent", "updated": "2026-01-02T00:00:00Z"
    })
    assert journal.record_wise_status(attempt, "obligation", 3, status, occurred_at)
    assert journal.load(attempt)["obligations"]["obligation"]["outcome_status"] == "sent"


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


@pytest.mark.parametrize("status", ["not_submitted", "funds_refunded", "cancelled"])
def test_terminal_wise_messages_do_not_offer_approval_as_recovery(status):
    import monedula
    import consent_email
    from handlers.payment_profile import PaymentProfile

    profile = PaymentProfile(prefix="SYNTHETIC", label="Synthetic", calendar_keywords=[],
                             payment_type="wise", amount_eur=1)
    handler = wise.WiseTransferHandler(profile)
    item = consent_email.build_pending_items([(handler, [{"due_date": "2026-01-01"}])],
                                             "2026-01-01")[0]
    entry = {"state": "failed", "failure_status": status}
    _, alert = monedula._outcome_alert(item, {"status": status})
    hold = monedula._wise_hold_text(entry)
    confirmation = handler.format_confirmation({"status": status})
    for message in (alert, hold, confirmation):
        assert "original attempt remains held" in message.lower()
        assert "replacement route is not available yet" in message.lower()
        assert "approval alone will not advance" in message.lower()
        assert "do not pay again" in message.lower()


def test_reconciliation_confirms_and_applies_effects_once(attempt, monkeypatch):
    import monedula
    from handlers.payment_profile import PaymentProfile

    profile = PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=["synthetic"],
        payment_type="wise", amount_eur=1, wise_recipient_id=5,
        wise_recipient_name="Synthetic", neotoma_task_id="synthetic-task",
        entity_id="synthetic-profile",
    )
    handler = wise.WiseTransferHandler(profile)
    _bind_synthetic_attempt(attempt, profile)
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
        one_off=one_off, entity_id="synthetic-profile",
        due_date="2026-01-01" if one_off else "",
    )
    _bind_synthetic_attempt(attempt, profile)
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


@pytest.mark.parametrize("one_off", [False, True], ids=["recurring", "one_off"])
@pytest.mark.parametrize("history", ["sent_bounced_still_bounced", "sent_bounced_sent",
                                     "refunded_then_sent", "effects_then_return"])
def test_latest_wise_status_controls_real_effects(attempt, monkeypatch, one_off, history):
    import monedula
    import consent_email
    from fake_neotoma import FakeNeotoma
    from handlers import task_writes
    from handlers.payment_profile import PaymentProfile

    profile = PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=[] if one_off else ["synthetic"],
        payment_type="wise", amount_eur=1, neotoma_task_id="synthetic-task",
        one_off=one_off, entity_id="synthetic-profile",
        due_date="2026-01-01" if one_off else "",
    )
    _bind_synthetic_attempt(attempt, profile)
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_funding_pending(attempt, "obligation", 3, "COMPLETED")
    fake = FakeNeotoma({
        "synthetic-task": {"status": "awaiting_approval", "notes": "prior"},
        "synthetic-profile": {"status": "active"},
    })
    monkeypatch.setattr(consent_email, "STATE_FILE", attempt.parent / "consent.json")
    monkeypatch.setattr(task_writes, "make_client", fake.connection)
    monkeypatch.setattr(wise, "_find_next_event_due_date", lambda *_: "2026-12-01")
    monkeypatch.setattr(wise, "_wise_get", lambda *_: pytest.fail("network read not mocked by scenario"))
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda text, **kw: alerts.append(text))

    if history == "refunded_then_sent":
        journal.record_wise_status(attempt, "obligation", 3, "funds_refunded",
                                   "2026-01-01T00:00:00Z")
        assert monedula._reconcile_wise_entries([wise.WiseTransferHandler(profile)])
        assert fake.posts == []
        journal.record_wise_status(attempt, "obligation", 3, "outgoing_payment_sent",
                                   "2026-01-02T00:00:00Z")
    else:
        journal.record_wise_status(attempt, "obligation", 3, "outgoing_payment_sent",
                                   "2026-01-01T00:00:00Z")
        if history == "effects_then_return":
            assert monedula._reconcile_wise_entries([wise.WiseTransferHandler(profile)])
        journal.record_wise_status(attempt, "obligation", 3, "bounced_back",
                                   "2026-01-02T00:00:00Z")
        journal.schedule_wise_check(attempt, "obligation", time.time() + 86400)
        if history != "effects_then_return":
            assert journal.obligation_state(journal.load(attempt), "obligation") == "failed"
            with pytest.raises(journal.JournalError, match="before settlement"):
                journal.mark_wise_effects_applied(attempt, "obligation", 3)
            assert monedula._reconcile_wise_entries([wise.WiseTransferHandler(profile)])
            assert fake.posts == []
            assert journal.session_state(journal.load(attempt), "synthetic:session") is None
        if history == "sent_bounced_sent":
            journal.record_wise_status(attempt, "obligation", 3, "outgoing_payment_sent",
                                       "2026-01-03T00:00:00Z")

    if history == "sent_bounced_still_bounced":
        assert any("latest status is bounced_back" in text for text in alerts)
        assert not journal.load(attempt)["obligations"]["obligation"].get("effects_applied")
        return
    if history == "effects_then_return":
        posts = len(fake.posts)
        assert monedula._reconcile_wise_entries([wise.WiseTransferHandler(profile)])
        assert len(fake.posts) == posts
        assert journal.load(attempt)["obligations"]["obligation"]["effects_applied"] is True
        assert any("effects already applied" in text for text in alerts)
        return

    assert monedula._reconcile_wise_entries([wise.WiseTransferHandler(profile)])
    assert journal.load(attempt)["obligations"]["obligation"]["effects_applied"] is True
    assert journal.session_state(journal.load(attempt), "synthetic:session") == "settled"
    assert "transfer_id=3" in fake.entities["synthetic-task"]["notes"]
    if one_off:
        assert fake.entities["synthetic-task"]["status"] == "done"
        assert fake.entities["synthetic-profile"]["status"] == "archived"
    else:
        assert fake.entities["synthetic-task"]["due_date"] == "2026-12-01"
        assert fake.entities["synthetic-task"]["status"] == "awaiting_approval"
    posts = len(fake.posts)
    assert monedula._reconcile_wise_entries([wise.WiseTransferHandler(profile)])
    assert len(fake.posts) == posts


@pytest.mark.parametrize("one_off", [False, True], ids=["recurring", "one_off"])
def test_due_wise_read_checks_return_before_applying_effects(attempt, monkeypatch, one_off):
    import monedula
    import consent_email
    from fake_neotoma import FakeNeotoma
    from handlers import task_writes
    from handlers.payment_profile import PaymentProfile

    profile = PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=[] if one_off else ["synthetic"],
        payment_type="wise", amount_eur=1, neotoma_task_id="synthetic-task",
        one_off=one_off, entity_id="synthetic-profile",
        due_date="2026-01-01" if one_off else "",
    )
    _bind_synthetic_attempt(attempt, profile)
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(attempt, "obligation", 3, "outgoing_payment_sent",
                               "2026-01-01T00:00:00Z")
    journal.schedule_wise_check(attempt, "obligation", time.time() - 1)
    fake = FakeNeotoma({
        "synthetic-task": {"status": "awaiting_approval", "notes": "prior"},
        "synthetic-profile": {"status": "active"},
    })
    monkeypatch.setattr(consent_email, "STATE_FILE", attempt.parent / "consent.json")
    monkeypatch.setattr(task_writes, "make_client", fake.connection)
    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(wise, "_wise_get", lambda *_: {"statusHistory": [
        {"status": "bounced_back", "occurred_at": "2026-01-02T00:00:00Z"},
        {"status": "outgoing_payment_sent", "occurred_at": "2026-01-01T00:00:00Z"},
    ]})
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda text, **kw: alerts.append(text))
    assert monedula._reconcile_wise_entries([wise.WiseTransferHandler(profile)])
    assert fake.posts == []
    assert journal.session_state(journal.load(attempt), "synthetic:session") is None
    assert journal.load(attempt)["obligations"]["obligation"]["last_status"] == "bounced_back"
    assert any("latest status is bounced_back" in text for text in alerts)


def test_returned_invoice_due_date_edit_cannot_offer_fresh_consent(approval_env, monkeypatch):
    import consent_email
    from handlers.payment_profile import PaymentProfile

    world, approvals = approval_env
    profile = PaymentProfile(
        prefix="INVOICE", label="Synthetic invoice", calendar_keywords=[],
        payment_type="wise", amount_eur=10, due_date="2026-01-01",
        one_off=True, wise_recipient_id=5, wise_recipient_name="Synthetic",
        entity_id="synthetic-profile", neotoma_task_id="synthetic-task",
    )
    world.handler = wise.WiseTransferHandler(profile)
    old = consent_email.build_pending_items(
        [(world.handler, world.handler.matches([]))], "2026-11-18"
    )[0]
    path = consent_email.journal_path()
    journal.record_intent(path, old.obligation, "old-token", generation=1, rail="wise",
                          handler_name=world.handler.name, profile_entity_id=profile.entity_id,
                          task_id=profile.neotoma_task_id, payment_date=old.payment_date,
                          approved_terms_hash=old.obligation, match_id=old.payment_date)
    journal.record_transfer_created(path, old.obligation, 3)
    journal.record_wise_status(path, old.obligation, 3, "outgoing_payment_sent",
                               "2026-01-01T00:00:00Z")
    journal.record_wise_status(path, old.obligation, 3, "bounced_back",
                               "2026-01-02T00:00:00Z")
    journal.schedule_wise_check(path, old.obligation, time.time() + 86400)
    profile.due_date = "2026-02-01"
    monkeypatch.setattr(wise, "_wise_get", lambda *a: pytest.fail("unexpected Wise read"))
    world.tick()
    assert approvals.writes == 0
    assert list(journal.load(path)["obligations"]) == [old.obligation]


def test_confirmed_older_invoice_updates_only_its_approved_profile(attempt, monkeypatch):
    import monedula
    from fake_neotoma import FakeNeotoma
    from handlers import task_writes
    from handlers.payment_profile import PaymentProfile

    old = PaymentProfile(
        prefix="SYNTHETIC", label="Old invoice", calendar_keywords=[],
        payment_type="wise", amount_eur=1, due_date="2026-01-01", one_off=True,
        entity_id="old-profile", neotoma_task_id="old-task", wise_recipient_id=5,
        wise_recipient_name="Synthetic",
    )
    new = PaymentProfile(
        prefix="SYNTHETIC", label="New invoice", calendar_keywords=[],
        payment_type="wise", amount_eur=1, due_date="2026-02-01", one_off=True,
        entity_id="new-profile", neotoma_task_id="new-task", wise_recipient_id=5,
        wise_recipient_name="Synthetic",
    )
    _bind_synthetic_attempt(attempt, old)
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(attempt, "obligation", 3, "outgoing_payment_sent",
                               datetime.now(timezone.utc).isoformat())
    fake = FakeNeotoma({
        "old-task": {"status": "awaiting_approval", "notes": "old"},
        "old-profile": {"status": "active"},
        "new-task": {"status": "awaiting_approval", "notes": "new"},
        "new-profile": {"status": "active"},
    })
    alerts = []
    monkeypatch.setattr(task_writes, "make_client", fake.connection)
    monkeypatch.setattr(monedula, "_notify", lambda text, **kw: alerts.append(text))
    assert not monedula._reconcile_wise_entries([wise.WiseTransferHandler(new)])
    assert fake.posts == []
    assert not journal.load(attempt)["obligations"]["obligation"].get("effects_applied")
    assert any("cannot be tied" in text for text in alerts)

    assert monedula._reconcile_wise_entries([
        wise.WiseTransferHandler(old), wise.WiseTransferHandler(new)
    ])
    assert fake.entities["old-task"]["status"] == "done"
    assert fake.entities["old-profile"]["status"] == "archived"
    assert fake.entities["new-task"] == {"status": "awaiting_approval", "notes": "new"}
    assert fake.entities["new-profile"] == {"status": "active"}
    assert journal.load(attempt)["obligations"]["obligation"]["effects_applied"] is True


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
        neotoma_task_id="synthetic-task", entity_id="synthetic-profile",
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
    alerts = []
    import monedula
    monkeypatch.setattr(monedula, "_notify", lambda text, **kw: alerts.append(text))
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
    if lost_response == "create":
        assert any("submission outcome unknown" in text and "Wise Activity" in text
                   and "cannot check automatically" in text and "do not pay again" in text
                   for text in alerts)
    world.tick(minutes=15)
    if lost_response == "create":
        assert journal.obligation_state(world.journal, key) == "intent"
        assert journal.session_state(world.journal, "yoga:evt-1") == "pending"
        assert calls == ["create"] and effects == []
        assert any("submission outcome unknown" in text and "Wise Activity" in text
                   and "cannot check automatically" in text for text in alerts)
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
        neotoma_task_id="synthetic-task", entity_id="synthetic-profile",
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


def test_telegram_lost_create_response_warns_without_promising_reconciliation(
    sent_messages, monkeypatch
):
    import consent_email
    import monedula
    from handlers.payment_profile import PaymentProfile

    handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", calendar_keywords=["synthetic"],
        payment_type="wise", amount_eur=1, wise_reference="ref",
        wise_recipient_id=5, wise_recipient_name="Synthetic",
        entity_id="synthetic-profile", neotoma_task_id="synthetic-task",
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
    monkeypatch.setattr(wise, "_create_transfer", lambda *a: (
        calls.append("create"), (_ for _ in ()).throw(TimeoutError("lost response"))
    )[1])
    monkeypatch.setattr(wise, "_fund_transfer", lambda *a: pytest.fail("funded without ID"))
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda text, **kw: alerts.append(text))

    assert monedula.main() is True
    path = consent_email.journal_path()
    entry = next(iter(journal.load(path)["obligations"].values()))
    assert entry["state"] == "intent" and entry.get("transfer_id") is None
    assert calls == ["create"]
    assert any("submission outcome unknown" in msg and "Wise Activity" in msg
               and "cannot check it automatically" in msg and "do not pay again" in msg
               for msg in sent_messages)
    assert monedula.main() is False
    assert calls == ["create"]
    assert any("submission outcome unknown" in msg and "Wise Activity" in msg
               and "cannot check automatically" in msg for msg in alerts)


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
        entity_id="synthetic-profile",
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


def test_settled_older_invoice_does_not_hide_later_invoice(approval_env, monkeypatch):
    import consent_email
    from handlers.payment_profile import PaymentProfile

    world, approvals = approval_env
    world.handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="INVOICE", label="Later invoice", calendar_keywords=[],
        payment_type="wise", amount_eur=10, due_date="2026-02-01",
        one_off=True, wise_recipient_id=5, wise_recipient_name="Synthetic",
        entity_id="later-profile", neotoma_task_id="later-task",
    ))
    old_key = "older-obligation"
    path = consent_email.journal_path()
    journal.record_intent(path, old_key, "old-token", generation=1,
                          rail="wise", handler_name=world.handler.name,
                          profile_entity_id="older-profile", task_id="older-task",
                          payment_date="2026-01-01", approved_terms_hash=old_key, match_id="2026-01-01")
    journal.record_transfer_created(path, old_key, 3)
    journal.record_wise_status(path, old_key, 3, "outgoing_payment_sent",
                               datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(wise, "_wise_get", lambda *a: pytest.fail("Wise read not due"))
    world.tick()
    assert approvals.writes == 1
    assert approvals.only["payment_key"] != old_key


def test_later_invoice_same_profile_is_offered_after_prior_settlement(approval_env, monkeypatch):
    import consent_email
    from handlers.payment_profile import PaymentProfile

    world, approvals = approval_env
    profile = PaymentProfile(
        prefix="INVOICE", label="Synthetic invoice", calendar_keywords=[],
        payment_type="wise", amount_eur=10, wise_reference="first",
        due_date="2026-01-01", one_off=True, wise_recipient_id=5,
        wise_recipient_name="Synthetic", entity_id="synthetic-profile",
        neotoma_task_id="synthetic-task",
    )
    world.handler = wise.WiseTransferHandler(profile)
    old = consent_email.build_pending_items(
        [(world.handler, world.handler.matches([]))], "2026-11-18"
    )[0]
    path = consent_email.journal_path()
    journal.record_intent(path, old.obligation, "old-token", generation=1, rail="wise",
                          handler_name=world.handler.name, profile_entity_id=profile.entity_id,
                          task_id=profile.neotoma_task_id,
                          payment_date=old.payment_date, match_id=(old.match_id or old.payment_date),
                          approved_terms_hash=old.obligation)
    journal.record_transfer_created(path, old.obligation, 3)
    journal.record_wise_status(path, old.obligation, 3, "outgoing_payment_sent",
                               datetime.now(timezone.utc).isoformat())
    journal.mark_wise_effects_applied(path, old.obligation, 3)
    profile.due_date = "2026-02-01"
    profile.wise_reference = "second"
    profile.neotoma_task_id = "later-task"
    monkeypatch.setattr(wise, "_wise_get", lambda *a: pytest.fail("unexpected Wise read"))

    world.tick()
    assert approvals.writes == 1
    assert approvals.only["payment_key"] != old.obligation


def test_calendar_hold_does_not_capture_another_profile_on_same_event(tmp_path):
    import consent_email
    from handlers.payment_profile import PaymentProfile

    path = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(path)
    event = _event("shared-event")
    profiles = [
        PaymentProfile(prefix=prefix, label=prefix, calendar_keywords=["yoga"],
                       payment_type="wise", amount_eur=10, wise_recipient_id=5,
                       wise_recipient_name="Synthetic", entity_id=prefix)
        for prefix in ("FIRST", "SECOND")
    ]
    first, second = (wise.WiseTransferHandler(profile) for profile in profiles)
    first_item, second_item = (
        consent_email.build_pending_items([(handler, [{"event": event}])], "2026-11-18")[0]
        for handler in (first, second)
    )
    journal.record_intent(path, first_item.obligation, "old-token", generation=1,
                          rail="wise", handler_name=first.name,
                          profile_entity_id=profiles[0].entity_id,
                          match_id=(first_item.match_id or first_item.payment_date), payment_date=first_item.payment_date, task_id="synthetic-task", approved_terms_hash="synthetic-hash")
    assert consent_email.journal_state_for_item(journal.load(path), second_item) is None


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
        assert result["status"] == "submission_unknown"
        assert entry["state"] == "intent" and entry.get("transfer_id") is None
        assert "submission outcome unknown" in alert and "do not pay again" in alert
        assert "cannot check automatically" in alert and "Wise Activity" in alert
        confirmation = handler.format_confirmation(result)
        assert "submission outcome unknown" in confirmation
        assert "cannot check it automatically" in confirmation
        assert calls == ["create"]
    else:
        assert result["status"] == "funding_rejected"
        assert entry["state"] == "intent" and entry["transfer_id"] == 3
        assert "rejected funding" in alert and calls == ["create", "fund"]
    with pytest.raises(journal.JournalError, match="already recorded"):
        journal.record_intent(attempt, "obligation", "new-approval", generation=2, rail="wise",
                              profile_entity_id="synthetic-profile", handler_name="synthetic", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")


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


def test_incomplete_btc_legacy_done_non_sent_holds():
    assert journal.obligation_state({"obligations": {"btc": {
        "state": "done", "outcome_status": "manual_required"
    }}}, "btc") == "failed"


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
                          rail="wise", handler_name="synthetic",
                          profile_entity_id="synthetic-profile", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")
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
        entity_id="synthetic-profile", neotoma_task_id="synthetic-task",
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


def test_overdue_no_id_attempt_reports_manual_wise_check_on_later_ticks(attempt, monkeypatch):
    import monedula
    import consent_email

    data = journal.load(attempt)
    data["obligations"]["obligation"]["intent_at"] = (
        time.time() - monedula.WISE_PENDING_ALERT_AFTER_SECONDS - 1
    )
    journal._write(attempt, data)
    monkeypatch.setattr(consent_email, "STATE_FILE", attempt.parent / "consent.json")
    monkeypatch.setattr(wise, "_wise_get", lambda *a:
                        pytest.fail("a no-ID attempt cannot be reconciled by transfer ID"))
    alerts = []
    monkeypatch.setattr(monedula, "_notify", lambda text, **kw: alerts.append(text))

    for _ in range(2):
        assert not monedula._reconcile_wise_entries([])
    overdue = [text for text in alerts if "for two days" in text]
    assert len(overdue) == 2
    assert all("submission outcome unknown" in text and "Wise Activity" in text
               and "cannot check automatically" in text and "do not pay again" in text
               and "Monedula is checking" not in text for text in overdue)
    assert all("Wise Activity" in text and "cannot check automatically" in text
               for text in alerts)
    assert journal.load(attempt)["obligations"]["obligation"]["state"] == "intent"


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


@pytest.mark.parametrize("route", ["payment_approval", "email", "telegram"])
@pytest.mark.parametrize("attempt_state", [
    "intent", "funding_pending", "submission_unknown", "done_then_returned",
    "funds_refunded", "cancelled", "confirmed_unapplied",
])
def test_unresolved_profile_holds_changed_obligation_on_every_route(
    approval_env, monkeypatch, route, attempt_state
):
    import consent_email
    import monedula
    import payment_approval
    from handlers.payment_profile import PaymentProfile

    world, approvals = approval_env
    profile = PaymentProfile(
        prefix="FIRST", label="Synthetic", payment_type="wise",
        calendar_keywords=["yoga"], amount_eur=10, wise_reference="first",
        wise_recipient_id=5, wise_recipient_name="First Synthetic",
        entity_id="profile-p", neotoma_task_id="task-p",
    )
    world.handler = wise.WiseTransferHandler(profile)
    event = world.calendars["primary"][0]
    old = consent_email.build_pending_items(
        [(world.handler, world.handler.matches([event]))], "2026-11-18"
    )[0]
    path = consent_email.journal_path()
    journal.record_intent(
        path, old.obligation, "old-token", generation=1, rail="wise",
        handler_name=world.handler.name, profile_entity_id=profile.entity_id,
        task_id=profile.neotoma_task_id, payment_date=old.payment_date,
        match_id=(old.match_id or old.payment_date), approved_terms_hash=old.obligation,
    )
    if attempt_state == "submission_unknown":
        data = journal.load(path)
        data["obligations"][old.obligation]["submission_status"] = "unknown"
        journal._write(path, data)
    if attempt_state in {"funding_pending", "done_then_returned", "funds_refunded", "cancelled", "confirmed_unapplied"}:
        journal.record_transfer_created(path, old.obligation, 3)
    if attempt_state == "funding_pending":
        journal.record_funding_pending(path, old.obligation, 3, "COMPLETED")
    elif attempt_state == "done_then_returned":
        journal.record_wise_status(path, old.obligation, 3, "outgoing_payment_sent",
                                   "2026-11-11T21:00:00Z")
        journal.record_wise_status(path, old.obligation, 3, "bounced_back",
                                   "2026-11-12T21:00:00Z")
    elif attempt_state in {"funds_refunded", "cancelled"}:
        journal.record_wise_status(path, old.obligation, 3, attempt_state,
                                   "2026-11-12T21:00:00Z")
    elif attempt_state == "confirmed_unapplied":
        journal.record_wise_status(path, old.obligation, 3, "outgoing_payment_sent",
                                   "2026-11-12T21:00:00Z")

    profile.amount_eur = 20
    profile.wise_reference = "second"
    profile.wise_recipient_name = "Second Synthetic"
    profile.due_date = "2026-12-01"
    profile.prefix = "SECOND"
    event["id"] = "new-event"
    event["start"]["dateTime"] = "2026-11-11T20:30:00+01:00"
    changed = consent_email.build_pending_items(
        [(world.handler, world.handler.matches([event]))], "2026-11-18"
    )[0]
    assert changed.obligation != old.obligation
    assert world.handler.name != old.handler_name
    assert journal.wise_replay_hold(journal.load(path), profile.entity_id)[0] == old.obligation
    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", route)
    monkeypatch.setattr(monedula, "_reconcile_wise_entries", lambda *a: True)
    monkeypatch.setattr(payment_approval, "collect", lambda *a, **kw:
                        pytest.fail("held obligation offered for default consent"))
    monkeypatch.setattr(consent_email, "request_and_collect", lambda *a, **kw:
                        pytest.fail("held obligation offered for email consent"))
    monkeypatch.setattr(monedula, "telegram_send", lambda *a, **kw:
                        pytest.fail("held obligation offered for Telegram consent"))
    monkeypatch.setattr(wise, "_create_transfer", lambda *a, **kw:
                        pytest.fail("second Wise transfer created"))
    messages = []
    monkeypatch.setattr(monedula, "_notify", lambda message, **kw: messages.append(message))
    assert monedula._apply_session_journal([(world.handler, [world.handler.matches([event])[0]])],
                                           reconcile=True) == []
    world.tick()
    assert approvals.writes == 0 and world.sends == []
    assert any(old.obligation in message and "not lost" in message
               and "do not pay again" in message.lower() for message in messages)
    assert list(journal.load(path)["obligations"]) == [old.obligation]


def test_wise_hold_is_scoped_to_profile_and_unapplied_sent_blocks_later_obligation(
    approval_env, monkeypatch
):
    import consent_email
    import monedula
    from handlers.payment_profile import PaymentProfile

    world, approvals = approval_env
    old_profile = PaymentProfile(
        prefix="FIRST", label="First", payment_type="wise", calendar_keywords=["yoga"],
        amount_eur=10, entity_id="profile-p", wise_recipient_id=5,
        wise_recipient_name="Synthetic",
    )
    old_handler = wise.WiseTransferHandler(old_profile)
    old = consent_email.build_pending_items(
        [(old_handler, old_handler.matches(world.calendars["primary"]))], "2026-11-18"
    )[0]
    path = consent_email.journal_path()
    journal.record_intent(path, old.obligation, "old-token", generation=1,
                          rail="wise", profile_entity_id=old_profile.entity_id, handler_name="synthetic", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")
    other_profile = PaymentProfile(
        prefix="SECOND", label="Second", payment_type="wise", calendar_keywords=["yoga"],
        amount_eur=10, entity_id="profile-q", wise_recipient_id=5,
        wise_recipient_name="Synthetic",
    )
    world.handler = wise.WiseTransferHandler(other_profile)
    assert journal.wise_replay_hold(journal.load(path), other_profile.entity_id) is None
    monkeypatch.setattr(monedula, "_reconcile_wise_entries", lambda *a: True)
    world.tick()
    assert approvals.writes == 1

    journal.record_transfer_created(path, old.obligation, 3)
    journal.record_wise_status(path, old.obligation, 3, "outgoing_payment_sent",
                               "2026-11-11T21:00:00Z")
    old_profile.prefix = "LATER"
    old_profile.amount_eur = 11
    world.handler = wise.WiseTransferHandler(old_profile)
    later = consent_email.build_pending_items(
        [(world.handler, world.handler.matches(world.calendars["primary"]))], "2026-11-18"
    )[0]
    assert later.obligation != old.obligation
    assert journal.wise_replay_hold(journal.load(path), old_profile.entity_id)[0] == old.obligation
    world.tick(minutes=15)
    assert approvals.writes == 1
    assert list(journal.load(path)["obligations"]) == [old.obligation]


def test_wise_profile_releases_only_after_effects_and_session_settlement(attempt):
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(attempt, "obligation", 3, "outgoing_payment_sent",
                               "2026-11-11T21:00:00Z")
    assert journal.wise_replay_hold(journal.load(attempt), "synthetic-profile") is not None
    journal.mark_wise_effects_applied(attempt, "obligation", 3)
    assert journal.wise_replay_hold(journal.load(attempt), "synthetic-profile") is not None
    journal.set_session_state(attempt, "synthetic:session", journal.SESSION_SETTLED)
    assert journal.wise_replay_hold(journal.load(attempt), "synthetic-profile") is None


@pytest.mark.parametrize("legacy_outcome", [None, "manual_required", "failed"])
@pytest.mark.parametrize("route", ["payment_approval", "email", "telegram"])
def test_unclassified_legacy_attempt_holds_all_wise_profiles_with_row_key(
    approval_env, monkeypatch, route, legacy_outcome
):
    import consent_email
    import monedula
    import payment_approval
    from handlers.payment_profile import PaymentProfile

    world, approvals = approval_env
    path = consent_email.journal_path()
    old_key = "legacy-unclassified-attempt"
    journal.record_intent(path, old_key, "old-token", generation=1)
    if legacy_outcome is not None:
        data = journal.load(path)
        data["obligations"][old_key].update(state="done", outcome_status=legacy_outcome)
        journal._write(path, data)
    world.handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="OTHER", label="Other", payment_type="wise", calendar_keywords=["yoga"],
        amount_eur=10, entity_id="profile-q", wise_recipient_id=5,
        wise_recipient_name="Synthetic",
    ))
    assert journal.wise_replay_hold(journal.load(path), "profile-q")[0] == old_key
    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", route)
    monkeypatch.setattr(monedula, "_reconcile_wise_entries", lambda *a: True)
    monkeypatch.setattr(payment_approval, "collect", lambda *a, **kw:
                        pytest.fail("legacy attempt offered for default consent"))
    monkeypatch.setattr(consent_email, "request_and_collect", lambda *a, **kw:
                        pytest.fail("legacy attempt offered for email consent"))
    monkeypatch.setattr(monedula, "telegram_send", lambda *a, **kw:
                        pytest.fail("legacy attempt offered for Telegram consent"))
    messages = []
    monkeypatch.setattr(monedula, "_notify", lambda message, **kw: messages.append(message))
    world.tick()
    assert approvals.writes == 0 and world.sends == []
    assert any(old_key in message and "Old attempt needs to be reconciled by hand"
               in message and "Wise Activity" in message and "not lost" in message
               for message in messages)


def test_many_legacy_sent_rows_never_hold_later_wise_offers(approval_env, monkeypatch):
    import consent_email
    import monedula
    from handlers.payment_profile import PaymentProfile

    world, approvals = approval_env
    path = consent_email.journal_path()
    for index in range(3):
        key = f"legacy-paid-{index}"
        journal.record_intent(path, key, f"token-{index}", generation=index + 1)
        journal.record_outcome(path, key, "sent")
    data = journal.load(path)
    for index in range(3):
        assert journal.wise_replay_hold(data, f"profile-{index}") is None
    world.handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="LATER", label="Later", payment_type="wise", calendar_keywords=["yoga"],
        amount_eur=10, entity_id="profile-1", wise_recipient_id=5,
        wise_recipient_name="Synthetic",
    ))
    monkeypatch.setattr(monedula, "_reconcile_wise_entries", lambda *a: True)
    world.tick()
    assert approvals.writes == 1


def test_legacy_wise_done_sent_without_status_history_never_holds(tmp_path):
    path = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(path)
    journal.record_intent(path, "old-paid", "old-token", generation=1)
    journal.record_outcome(path, "old-paid", "sent")
    assert journal.is_legacy_settled_wise_row(journal.load(path)["obligations"]["old-paid"])
    assert journal.obligation_state(journal.load(path), "old-paid") == journal.STATE_DONE
    assert journal.wise_replay_hold(journal.load(path), "profile-p") is None


@pytest.mark.parametrize("missing_field", sorted(journal._MODERN_SETTLED_WISE_FIELDS))
def test_settled_modern_wise_row_missing_field_holds_profile(attempt, missing_field):
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(attempt, "obligation", 3, "outgoing_payment_sent",
                               "2026-11-11T21:00:00Z")
    journal.mark_wise_effects_applied(attempt, "obligation", 3)
    journal.set_session_state(attempt, "synthetic:session", journal.SESSION_SETTLED)
    data = journal.load(attempt)
    assert journal.wise_replay_hold(data, "synthetic-profile") is None
    data["obligations"]["obligation"].pop(missing_field)
    journal._write(attempt, data)
    try:
        loaded = journal.load(attempt)
    except journal.JournalError as exc:
        assert "obligation" in str(exc)
    else:
        assert journal.wise_replay_hold(loaded, "synthetic-profile")[0] == "obligation"
    with pytest.raises(journal.JournalError):
        journal.record_intent(attempt, "later", "later-token", generation=2,
                              rail="wise", profile_entity_id="synthetic-profile", handler_name="synthetic", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")
    assert "later" not in data["obligations"]


def test_modern_without_any_status_history_keeps_profile_held(attempt):
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(attempt, "obligation", 3, "outgoing_payment_sent",
                               "2026-11-11T21:00:00Z")
    journal.mark_wise_effects_applied(attempt, "obligation", 3)
    journal.set_session_state(attempt, "synthetic:session", journal.SESSION_SETTLED)
    data = journal.load(attempt)
    data["obligations"]["obligation"].pop("last_status")
    data["obligations"]["obligation"].pop("last_occurred_at")
    journal._write(attempt, data)
    assert journal.wise_replay_hold(journal.load(attempt), "synthetic-profile")[0] == "obligation"
    with pytest.raises(journal.JournalError, match="obligation"):
        journal.record_intent(attempt, "later", "later-token", generation=2,
                              rail="wise", profile_entity_id="synthetic-profile", handler_name="synthetic", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(rail="unknown"),
    lambda row: row.update(unexpected_field=True),
    lambda row: row.update(rail="wise"),
    lambda row: row.update(attempt=1, transfer_id=3),
])
def test_nonlegacy_settled_row_holds_every_profile(tmp_path, mutation):
    path = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(path)
    journal.record_intent(path, "old-paid", "old-token", generation=1)
    journal.record_outcome(path, "old-paid", "sent")
    data = journal.load(path)
    mutation(data["obligations"]["old-paid"])
    journal._write(path, data)
    assert not journal.is_legacy_settled_wise_row(data["obligations"]["old-paid"])
    try:
        loaded = journal.load(path)
    except journal.JournalError as exc:
        assert "old-paid" in str(exc)
    else:
        assert journal.wise_replay_hold(loaded, "any-profile")[0] == "old-paid"
    with pytest.raises(journal.JournalError, match="old-paid"):
        journal.record_intent(path, "later", "later-token", generation=2,
                              rail="wise", profile_entity_id="any-profile", handler_name="synthetic", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")


def test_wise_hold_census_matches_journal_for_synthetic_rows(tmp_path, capsys):
    import wise_journal_hold_census as census

    path = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(path)
    journal.record_intent(path, "old-paid", "old-token", generation=1)
    journal.record_outcome(path, "old-paid", "sent")
    journal.record_intent(path, "new-paid", "new-token", generation=2, rail="wise",
                          handler_name="synthetic", profile_entity_id="profile-p",
                          task_id="synthetic-task", approved_terms_hash="synthetic-hash",
                          payment_date="2026-01-01", match_id="2026-01-01")
    journal.record_transfer_created(path, "new-paid", 3)
    journal.record_wise_status(path, "new-paid", 3, "outgoing_payment_sent",
                               "2026-11-11T21:00:00Z")
    journal.mark_wise_effects_applied(path, "new-paid", 3)
    data = journal.load(path)
    assert census.classify(data["obligations"]["old-paid"], data) == "legacy_settled_sent"
    assert census.classify(data["obligations"]["new-paid"], data) == "wise_new_resolved_paid"
    data["obligations"]["new-paid"].pop("last_status")
    data["obligations"]["unknown"] = {
        **data["obligations"]["old-paid"], "rail": "unexpected"
    }
    journal._write(path, data)
    assert census.main(["census", str(path)]) == 0
    output = capsys.readouterr().out
    assert "wise_new_UNRESOLVED_with_profile_id: 1" in output
    assert "unclassified_UNRESOLVED_all_profiles: 1" in output
    assert "legacy_settled_sent: 1" in output


@pytest.mark.parametrize("shape", ["legacy", "unknown_rail", "stripped_modern"])
def test_reconciliation_skips_only_exact_legacy_paid_row(tmp_path, monkeypatch, shape):
    import consent_email
    import monedula

    path = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(path)
    if shape == "stripped_modern":
        journal.record_intent(path, "held-key", "token", generation=1, rail="wise",
                              profile_entity_id="profile-p", handler_name="synthetic", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")
        journal.record_transfer_created(path, "held-key", 3)
        journal.record_wise_status(path, "held-key", 3, "outgoing_payment_sent",
                                   "2026-11-11T21:00:00Z")
        data = journal.load(path)
        data["obligations"]["held-key"].pop("last_status")
        data["obligations"]["held-key"].pop("transfer_id")
        journal._write(path, data)
    else:
        journal.record_intent(path, "held-key", "token", generation=1)
        journal.record_outcome(path, "held-key", "sent")
        if shape == "unknown_rail":
            data = journal.load(path)
            data["obligations"]["held-key"]["rail"] = "unexpected"
            journal._write(path, data)
    monkeypatch.setattr(consent_email, "STATE_FILE", tmp_path / "consent.json")
    monkeypatch.setattr(wise, "_wise_get", lambda *a, **kw:
                        pytest.fail("no Wise network permitted"))
    messages = []
    monkeypatch.setattr(monedula, "_notify", lambda message, **kw: messages.append(message))
    result = monedula._reconcile_wise_entries([])
    assert result is (shape == "legacy")
    if shape != "legacy":
        assert any("held-key" in message for message in messages)


@pytest.mark.parametrize("changes", [
    {"state": "done", "last_status": "bounced_back"},
    {"state": "failed", "outcome_status": "sent"},
    {"transfer_id": None},
    {"last_status": "processing"},
    {"discrepancy_status": "bounced_back"},
    {"failure_status": "cancelled"},
    {"effects_applied": False},
])
def test_contradictory_wise_row_loads_but_holds_profile(attempt, changes):
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(attempt, "obligation", 3, "outgoing_payment_sent",
                               "2026-11-11T21:00:00Z")
    journal.mark_wise_effects_applied(attempt, "obligation", 3)
    journal.set_session_state(attempt, "synthetic:session", journal.SESSION_SETTLED)
    assert journal.wise_replay_hold(journal.load(attempt), "synthetic-profile") is None
    data = journal.load(attempt)
    data["obligations"]["obligation"].update(changes)
    journal._write(attempt, data)
    loaded = journal.load(attempt)
    assert journal.wise_replay_hold(loaded, "synthetic-profile")[0] == "obligation"
    with pytest.raises(journal.JournalError, match="obligation"):
        journal.record_intent(attempt, "new-obligation", "new-token", generation=2,
                              rail="wise", profile_entity_id="synthetic-profile", handler_name="synthetic", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")


@pytest.mark.parametrize("route", ["payment_approval", "email", "telegram"])
def test_legacy_done_sent_wise_row_settles_same_calendar_event_without_new_consent(
    approval_env, monkeypatch, route
):
    import consent_email
    import monedula
    import payment_approval
    from handlers.payment_profile import PaymentProfile

    world, approvals = approval_env
    profile = PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", payment_type="wise",
        calendar_keywords=["yoga"], amount_eur=10, wise_recipient_id=5,
        wise_recipient_name="Synthetic", entity_id="synthetic-profile",
    )
    world.handler = wise.WiseTransferHandler(profile)
    event = world.calendars["primary"][0]
    match = world.handler.matches([event])[0]
    old_key = consent_email._base_obligation_key(
        world.handler, consent_email.event_session_date(match), ""
    )
    current = consent_email.build_pending_items([(world.handler, [match])], "2026-11-18")[0]
    assert current.obligation != old_key
    path = consent_email.journal_path()
    journal.record_intent(path, old_key, "legacy-token", generation=1)
    journal.record_outcome(path, old_key, "sent")
    assert journal.is_legacy_settled_wise_row(journal.load(path)["obligations"][old_key])
    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", route)
    monkeypatch.setattr(monedula, "_reconcile_wise_entries", lambda *a: True)
    monkeypatch.setattr(payment_approval, "collect", lambda *a, **kw:
                        pytest.fail("legacy paid event offered for default consent"))
    monkeypatch.setattr(consent_email, "request_and_collect", lambda *a, **kw:
                        pytest.fail("legacy paid event offered for email consent"))
    monkeypatch.setattr(monedula, "telegram_send", lambda *a, **kw:
                        pytest.fail("legacy paid event offered for Telegram consent"))
    monkeypatch.setattr(wise, "_create_transfer", lambda *a, **kw:
                        pytest.fail("legacy paid event created a transfer"))
    monkeypatch.setattr(wise, "_fund_transfer", lambda *a, **kw:
                        pytest.fail("legacy paid event funded a transfer"))
    world.tick()
    session = journal.session_key(world.handler.name, event["id"])
    assert journal.session_state(journal.load(path), session) == journal.SESSION_SETTLED
    assert approvals.writes == 0 and world.sends == []
    assert list(journal.load(path)["obligations"]) == [old_key]


@pytest.mark.parametrize("missing_field", [
    "last_status", "last_occurred_at", "both", "malformed_timestamp",
    "btc_tag", "mixed_btc", "unknown_key",
])
@pytest.mark.parametrize("route", ["payment_approval", "email", "telegram"])
def test_stripped_modern_row_cannot_settle_calendar_or_request_payment(
    approval_env, monkeypatch, route, missing_field
):
    import consent_email
    import monedula
    import payment_approval
    from handlers.payment_profile import PaymentProfile

    world, approvals = approval_env
    world.handler = wise.WiseTransferHandler(PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", payment_type="wise",
        calendar_keywords=["yoga"], amount_eur=10, wise_recipient_id=5,
        wise_recipient_name="Synthetic", entity_id="synthetic-profile",
    ))
    event = world.calendars["primary"][0]
    match = world.handler.matches([event])[0]
    key = consent_email._base_obligation_key(
        world.handler, consent_email.event_session_date(match), ""
    )
    path = consent_email.journal_path()
    journal.record_intent(path, key, "modern-token", generation=1, rail="wise",
                          handler_name=world.handler.name,
                          profile_entity_id="synthetic-profile", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")
    journal.record_transfer_created(path, key, 3)
    journal.record_wise_status(path, key, 3, "outgoing_payment_sent",
                               "2026-11-11T21:00:00Z")
    data = journal.load(path)
    if missing_field == "malformed_timestamp":
        data["obligations"][key]["last_occurred_at"] = "not-a-date"
        data["obligations"][key]["next_check_at"] = time.time() + 86400
    elif missing_field in {"btc_tag", "mixed_btc"}:
        data["obligations"][key]["rail"] = "btc"
        if missing_field == "mixed_btc":
            data["obligations"][key]["unknown_extra"] = True
    elif missing_field == "unknown_key":
        data["obligations"][key]["unknown_extra"] = True
    else:
        for field in (("last_status", "last_occurred_at") if missing_field == "both"
                      else (missing_field,)):
            data["obligations"][key].pop(field)
    journal._write(path, data)
    messages = []
    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", route)
    monkeypatch.setattr(monedula, "_reconcile_wise_entries", lambda *a: True)
    monkeypatch.setattr(monedula, "_notify", lambda message, **kw: messages.append(message))
    monkeypatch.setattr(payment_approval, "collect", lambda *a, **kw:
                        pytest.fail("stripped row offered for approval"))
    monkeypatch.setattr(consent_email, "request_and_collect", lambda *a, **kw:
                        pytest.fail("stripped row offered for email consent"))
    monkeypatch.setattr(monedula, "telegram_send", lambda *a, **kw:
                        pytest.fail("stripped row offered for Telegram consent"))
    monkeypatch.setattr(wise, "_create_transfer", lambda *a, **kw:
                        pytest.fail("stripped row created a transfer"))
    monkeypatch.setattr(wise, "_fund_transfer", lambda *a, **kw:
                        pytest.fail("stripped row funded a transfer"))
    world.tick()
    assert journal.session_state(journal.load(path), journal.session_key(
        world.handler.name, event["id"])) != journal.SESSION_SETTLED
    assert approvals.writes == 0 and world.sends == []
    assert any(key in message for message in messages)


@pytest.mark.parametrize("bad_id", [None, 42, ""])
def test_unreadable_wise_profile_identity_holds_all_profiles(tmp_path, bad_id):
    path = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(path)
    journal.record_intent(path, "old-attempt", "old-token", generation=1,
                          rail="wise", profile_entity_id="profile-p", handler_name="synthetic", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")
    data = journal.load(path)
    data["obligations"]["old-attempt"]["profile_entity_id"] = bad_id
    journal._write(path, data)
    assert journal.wise_replay_hold(journal.load(path), "profile-q")[0] == "old-attempt"
    with pytest.raises(journal.JournalError, match="old-attempt"):
        journal.record_intent(path, "new-attempt", "new-token", generation=2,
                              rail="wise", profile_entity_id="profile-q", handler_name="synthetic", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")


def test_handler_durable_intent_refuses_another_unresolved_profile_attempt(tmp_path):
    path = tmp_path / journal.JOURNAL_NAME
    journal.init_journal(path)
    journal.record_intent(path, "first", "first-token", generation=1,
                          rail="wise", profile_entity_id="profile-p", handler_name="synthetic", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")
    journal.record_intent(path, "second", "second-token", generation=2,
                          rail="wise", profile_entity_id="profile-q", handler_name="synthetic", task_id="synthetic-task", approved_terms_hash="synthetic-hash", payment_date="2026-01-01", match_id="2026-01-01")
    data = journal.load(path)
    data["obligations"]["second"]["profile_entity_id"] = "profile-p"
    journal._write(path, data)
    with pytest.raises(journal.JournalError, match="first"):
        wise._require_wise_intent((path, "second"))


def _minimal_sent_row(path):
    journal.record_transfer_created(path, "obligation", 3)
    journal.record_wise_status(path, "obligation", 3, "outgoing_payment_sent",
                               "2026-11-11T21:00:00Z")
    journal.mark_wise_effects_applied(path, "obligation", 3)
    journal.set_session_state(path, "synthetic:session", journal.SESSION_SETTLED)
    return journal.load(path)["obligations"]["obligation"]


@pytest.mark.parametrize("field", sorted(journal._MODERN_SETTLED_WISE_FIELDS | {"effects_applied"}))
@pytest.mark.parametrize("change", ["remove", "wrong_type"])
def test_every_modern_sent_field_is_validated(attempt, field, change):
    row = copy.deepcopy(_minimal_sent_row(attempt))
    assert journal.classify_journal_row(row, journal.load(attempt))[0] == "wise_modern_resolved"
    if change == "remove":
        row.pop(field)
    else:
        row[field] = []
    assert journal.classify_journal_row(row, journal.load(attempt))[0] in {
        "invalid", "wise_modern_unresolved"
    }, field
    data = journal.load(attempt)
    data["obligations"]["obligation"] = row
    assert journal.wise_replay_hold(data, "synthetic-profile")[0] == "obligation"


@pytest.mark.parametrize("field", ["intent_at", "outcome_at", "last_checked_at", "last_occurred_at"])
@pytest.mark.parametrize("value", ["not-a-date", "2026-11-11T21:00:00", "", 0, float("nan")])
def test_modern_sent_timestamp_values_hold(attempt, field, value):
    row = copy.deepcopy(_minimal_sent_row(attempt))
    row[field] = value
    assert journal.classify_journal_row(row)[0] == "invalid"
    data = journal.load(attempt)
    data["obligations"]["obligation"] = row
    assert journal.wise_replay_hold(data, "synthetic-profile")[0] == "obligation"


@pytest.mark.parametrize("field,value", [
    ("attempt", True), ("attempt", 0), ("transfer_id", "3"),
    ("transfer_id", -1), ("generation", True), ("generation", -1),
    ("token", ""), ("profile_entity_id", ""),
    ("last_status", "not-a-status"), ("outcome_status", "pending"),
    ("effects_applied", "true"), ("unknown_extra", "surprise"),
    ("next_check_at", float("inf")),
])
def test_modern_sent_out_of_range_and_extra_fields_hold(attempt, field, value):
    row = copy.deepcopy(_minimal_sent_row(attempt))
    row[field] = value
    assert journal.classify_journal_row(row)[0] == "invalid"
    data = journal.load(attempt)
    data["obligations"]["obligation"] = row
    assert journal.wise_replay_hold(data, "synthetic-profile")[0] == "obligation"


@pytest.mark.parametrize("field", sorted(journal._WISE_OPTIONAL_FIELDS))
def test_every_optional_wise_field_rejects_a_wrong_type(attempt, field):
    row = copy.deepcopy(_minimal_sent_row(attempt))
    row[field] = "broken" if field == "attempts" else []
    assert journal.classify_journal_row(row)[0] == "invalid", field


@pytest.mark.parametrize("corruption", ["btc_tag", "missing_btc_field", "mixed_btc", "valid_btc"])
def test_btc_rows_are_exempt_only_with_their_own_valid_shape(attempt, corruption):
    row = copy.deepcopy(_minimal_sent_row(attempt))
    if corruption == "valid_btc":
        row = {key: row[key] for key in journal._LEGACY_SETTLED_WISE_FIELDS}
        row["rail"] = "btc"
    elif corruption == "missing_btc_field":
        row = {key: row[key] for key in journal._LEGACY_SETTLED_WISE_FIELDS}
        row["rail"] = "btc"
        row.pop("outcome_at")
    elif corruption == "mixed_btc":
        row["rail"] = "btc"
        row["extra"] = "mixed"
    else:
        row["rail"] = "btc"
    data = journal.load(attempt)
    data["obligations"]["obligation"] = row
    kind, _ = journal.classify_journal_row(row, data)
    if corruption == "valid_btc":
        assert kind == "btc_valid_settled"
        assert journal.wise_replay_hold(data, "synthetic-profile") is None
    else:
        assert kind == "invalid"
        assert journal.wise_replay_hold(data, "synthetic-profile")[0] == "obligation"


def test_malformed_modern_status_timestamp_holds_and_cannot_apply_effects(
    attempt, monkeypatch
):
    import consent_email
    import monedula

    _minimal_sent_row(attempt)
    data = journal.load(attempt)
    row = data["obligations"]["obligation"]
    row["last_occurred_at"] = "not-a-date"
    row["next_check_at"] = time.time() + 86400
    row.pop("effects_applied")
    data["sessions"]["synthetic:session"]["state"] = journal.SESSION_PENDING
    journal._write(attempt, data)
    assert journal.classify_journal_row(journal.load(attempt)["obligations"]["obligation"])[0] == "invalid"
    monkeypatch.setattr(consent_email, "STATE_FILE", attempt.parent / "consent.json")
    monkeypatch.setattr(wise, "_wise_get", lambda *a, **kw: pytest.fail("invalid row contacted Wise"))
    monkeypatch.setattr(wise, "_update_task", lambda *a, **kw: pytest.fail("invalid row updated task"))
    monkeypatch.setattr(monedula, "_notify", lambda *a, **kw: None)
    assert not monedula._apply_confirmed_wise_effects(attempt, "obligation", row, {})
    assert not monedula._reconcile_wise_entries([])
    loaded = journal.load(attempt)
    assert not loaded["obligations"]["obligation"].get("effects_applied")
    assert journal.session_state(loaded, "synthetic:session") == journal.SESSION_PENDING


@pytest.mark.parametrize("field", sorted(journal._MODERN_SETTLED_WISE_FIELDS))
def test_corrupt_sent_field_never_updates_task_or_settles_session(
    attempt, monkeypatch, field
):
    import monedula
    from handlers.payment_profile import PaymentProfile

    profile = PaymentProfile(
        prefix="SYNTHETIC", label="Synthetic", payment_type="wise",
        calendar_keywords=["synthetic"], amount_eur=1,
        entity_id="synthetic-profile", neotoma_task_id="synthetic-task",
    )
    handler = wise.WiseTransferHandler(profile)
    _bind_synthetic_attempt(attempt, profile)
    journal.record_transfer_created(attempt, "obligation", 3)
    journal.record_wise_status(attempt, "obligation", 3, "outgoing_payment_sent",
                               "2026-11-11T21:00:00Z")
    data = journal.load(attempt)
    data["obligations"]["obligation"][field] = []
    journal._write(attempt, data)
    calls = []
    monkeypatch.setattr(wise, "_update_task", lambda *a, **kw: calls.append("task") or True)
    monkeypatch.setattr(monedula, "_notify", lambda *a, **kw: None)
    if field == "state":
        with pytest.raises(journal.JournalError):
            monedula._apply_confirmed_wise_effects(
                attempt, "obligation", data["obligations"]["obligation"],
                {profile.entity_id: handler},
            )
        loaded = data
    else:
        assert not monedula._apply_confirmed_wise_effects(
            attempt, "obligation", data["obligations"]["obligation"],
            {profile.entity_id: handler},
        )
        loaded = journal.load(attempt)
    assert calls == []
    assert not loaded["obligations"]["obligation"].get("effects_applied")
    assert journal.session_state(loaded, "synthetic:session") != journal.SESSION_SETTLED


def test_wise_hold_has_one_decider_and_all_offer_paths_reach_it():
    import ast

    root = Path(__file__).parent
    sources = {name: ast.parse((root / name).read_text()) for name in (
        "payment_journal.py", "consent_email.py", "monedula.py", "handlers/wise_transfer.py"
    )}
    definitions = [node for tree in sources.values() for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "wise_replay_hold"]
    assert len(definitions) == 1

    def calls(function, name):
        return any(isinstance(node, ast.Call) and (
            isinstance(node.func, ast.Name) and node.func.id == name
            or isinstance(node.func, ast.Attribute) and node.func.attr == name
        ) for node in ast.walk(function))

    functions = {node.name: node for tree in sources.values() for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef)}
    for name in ("record_intent", "journal_entry_for_item", "_require_wise_intent"):
        assert calls(functions[name], "wise_replay_hold"), name
    assert calls(functions["journal_state_for_item"], "journal_entry_for_item")
    for name in ("request_and_collect", "_apply_session_journal", "main"):
        assert calls(functions[name], "journal_state_for_item"), name
