# ruff: noqa: F811  (the shared ``env`` fixture is imported, not redefined)
"""Series scoped payment matching and approval effects, with synthetic data."""

from __future__ import annotations

import sys
import json
import os
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import consent_email  # noqa: E402
import monedula  # noqa: E402
import payment_journal  # noqa: E402
from handlers.btc_transfer import BtcTransferHandler  # noqa: E402
from handlers import btc_transfer, wise_transfer  # noqa: E402
from handlers.payment_profile import (
    PaymentProfile,
    _load_profile,
    load_profiles_from_neotoma,
)  # noqa: E402
from handlers.wise_transfer import WiseTransferHandler  # noqa: E402
from handler_base import event_matches_profile, series_match_id, session_start_key  # noqa: E402
import payment_approval  # noqa: E402
from test_payment_approval import env  # noqa: E402,F401
from test_session_payments import SESSION_END, _event  # noqa: E402
from test_strandings import _profile, _serve  # noqa: E402
from strandings import REASON_BAD_CALENDAR_SERIES_ID  # noqa: E402


SERIES = "synthetic-series"
_REAL_NOTIFY = monedula._notify


def _payment_profile(
    payment_type: str = "wise", series: str = SERIES
) -> PaymentProfile:
    return PaymentProfile(
        prefix="TEST",
        label="Example class",
        calendar_keywords=["movement", "class"],
        payment_type=payment_type,
        amount_eur=10,
        calendar_recurring_event_id=series,
        wise_recipient_id=7,
        wise_recipient_name="Example Recipient",
        wise_reference="example",
        btc_address="synthetic-address",
    )


def _session(
    eid: str, *, recurring=SERIES, title="Movement class", end=SESSION_END
) -> dict:
    event = _event(eid, title, end)
    if recurring is not None:
        event["recurringEventId"] = recurring
    return event


@pytest.mark.parametrize("handler_type", [BtcTransferHandler, WiseTransferHandler])
def test_series_match_requires_title_and_exact_identity_on_both_rails(handler_type):
    handler = handler_type(_payment_profile())
    wrong = _session("wrong", recurring="other-series")
    missing = _session("missing", recurring=None)
    instance = _session("instance")
    parent = _session(SERIES, recurring=None)
    wrong_title = _session("wrong-title", title="Other activity")
    assert handler.matches([wrong, missing, wrong_title]) == []
    assert [m["event"]["id"] for m in handler.matches([instance])] == ["instance"]
    assert [m["event"]["id"] for m in handler.matches([parent])] == [SERIES]
    handler.profile.calendar_recurring_event_id = ""
    missing["summary"] = "Movement session"  # one keyword still suffices
    assert [m["event"]["id"] for m in handler.matches([wrong, missing])] == [
        "wrong",
        "missing",
    ]
    handler.profile.calendar_recurring_event_id = SERIES
    assert handler.matches([_session("case", recurring=SERIES.upper())]) == []


@pytest.mark.parametrize("handler_type", [BtcTransferHandler, WiseTransferHandler])
@pytest.mark.parametrize("bad", [12, "", "   ", None])
def test_malformed_event_series_values_fail_closed(handler_type, bad):
    handler = handler_type(_payment_profile())
    assert handler.matches([_session("event", recurring=bad)]) == []
    parent = _session(bad, recurring="other-series")
    assert handler.matches([parent]) == []
    assert handler.matches([_session(bad)]) == []


def test_profile_loaders_preserve_trimmed_case_sensitive_series_id(monkeypatch):
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-token")
    _serve(
        monkeypatch,
        {"entities": [_profile(calendar_recurring_event_id=f"  {SERIES}  ")]},
    )
    assert load_profiles_from_neotoma()[0].calendar_recurring_event_id == SERIES
    _serve(monkeypatch, {"entities": [_profile(calendar_recurring_event_id="  ")]})
    assert load_profiles_from_neotoma()[0].calendar_recurring_event_id == ""
    _serve(monkeypatch, {"entities": [_profile(calendar_recurring_event_id=123)]})
    stranded = []
    assert load_profiles_from_neotoma(stranded) == []
    assert len(stranded) == 1
    assert stranded[0].reason == REASON_BAD_CALENDAR_SERIES_ID
    monkeypatch.setenv("TEST_CALENDAR_KEYWORDS", "movement,class")
    monkeypatch.setenv("TEST_PAYMENT_TYPE", "wise")
    monkeypatch.setenv("TEST_AMOUNT_EUR", "10")
    monkeypatch.setenv("TEST_CALENDAR_RECURRING_EVENT_ID", f"  {SERIES}  ")
    assert _load_profile("TEST").calendar_recurring_event_id == SERIES
    monkeypatch.setenv("TEST_CALENDAR_RECURRING_EVENT_ID", "  ")
    assert _load_profile("TEST").calendar_recurring_event_id == ""


def test_calendar_fetch_keeps_recurring_event_id(env):
    world, _neo = env
    world.calendars["primary"] = [_session("instance")]
    assert monedula.fetch_recent_events()[0]["recurringEventId"] == SERIES


@pytest.mark.parametrize("module", [btc_transfer, wise_transfer])
def test_next_event_search_filters_by_series(module, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/synthetic/gws")
    response_events = [_session("wrong", recurring="other-series")]

    def fake_gws(command, **_kwargs):
        params = json.loads(command[command.index("--params") + 1])
        assert params["singleEvents"] is True
        assert "fields" not in params  # the full event retains recurringEventId
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"items": response_events}), ""
        )

    monkeypatch.setattr(module.subprocess, "run", fake_gws)
    profile = _payment_profile()
    assert module._find_next_event_due_date(profile) is None
    response_events.append(_session("instance"))
    assert module._find_next_event_due_date(profile) == "2026-11-12"


@pytest.mark.parametrize(
    "handler_type,payment_type",
    [(BtcTransferHandler, "btc"), (WiseTransferHandler, "wise")],
)
def test_duplicate_calendars_raise_one_approval_and_distinct_sessions_raise_two(
    env, handler_type, payment_type
):
    world, neo = env
    world.handler = handler_type(_payment_profile(payment_type))
    world.calendars = {
        "primary": [_session("event-b")],
        "shared": [_session("event-a", title="  movement   CLASS  ")],
    }
    world.tick()
    assert neo.writes == 1
    assert len(neo.rows) == 1
    session_id = series_match_id(world.handler.profile, _session("event-a"))
    assert neo.only["session"] == session_id
    assert neo.only["payment_key"] == consent_email.obligation_key(
        world.handler, "2026-11-11", session_id
    )
    # The second session has a different start and therefore its own key.
    world.calendars["primary"].append(
        _session("event-c", end=SESSION_END - timedelta(hours=2))
    )
    world.tick(minutes=15)
    assert neo.writes == 2
    assert len(neo.rows) == 2
    assert {row["session"] for row in neo.rows.values()} == {
        session_id, series_match_id(world.handler.profile, _session("event-c", end=SESSION_END - timedelta(hours=2)))
    }


@pytest.mark.parametrize(
    "handler_type,payment_type",
    [(BtcTransferHandler, "btc"), (WiseTransferHandler, "wise")],
)
def test_rejected_duplicate_key_stays_rejected_on_next_tick(
    env, handler_type, payment_type
):
    world, neo = env
    world.handler = handler_type(_payment_profile(payment_type))
    world.calendars = {
        "primary": [_session("event-b")],
        "shared": [_session("event-a")],
    }
    world.tick()
    key = neo.only["payment_key"]
    neo.resolve(key, "rejected", "no")
    world.tick(minutes=15)
    world.tick(minutes=15)
    assert neo.writes == 1
    assert len(neo.rows) == 1
    assert neo.rows[key]["status"] == "rejected"
    session_id = series_match_id(world.handler.profile, _session("event-a"))
    assert payment_journal.session_state(world.journal, f"test:{session_id}") == "skipped"


def test_consent_builder_excludes_wrong_series_and_duplicate_copy():
    handler = BtcTransferHandler(_payment_profile("btc"))
    events = [_session("wrong", recurring="other-series"), _session("b"), _session("a")]
    matches = [{"event": e, "summary": e["summary"]} for e in events]
    items = consent_email.build_pending_items([(handler, matches)], "2026-11-11")
    assert len(items) == 1
    assert items[0].match_id == series_match_id(handler.profile, _session("a"))
    _subject, body = consent_email.build_request_body(items, superseded=False)
    assert "wrong" not in body


@pytest.mark.parametrize("rail", ["btc", "wise"])
@pytest.mark.parametrize("value", ["x" * 4097, "x" * 1025, "bad\nseries", "bad\x7fseries", "bad\x85series", 27, [SERIES]])
def test_invalid_configured_series_strands_both_loaders(rail, value, monkeypatch):
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-token")
    _serve(monkeypatch, {"entities": [_profile(
        payment_type=rail, calendar_recurring_event_id=value,
    )]})
    stranded = []
    assert load_profiles_from_neotoma(stranded) == []
    assert [s.reason for s in stranded] == [REASON_BAD_CALENDAR_SERIES_ID]
    assert str(value) not in stranded[0].detail

    monkeypatch.setenv("TEST_CALENDAR_KEYWORDS", "movement")
    monkeypatch.setenv("TEST_PAYMENT_TYPE", rail)
    monkeypatch.setenv("TEST_AMOUNT_EUR", "10")
    monkeypatch.setattr(os, "environ", dict(os.environ, TEST_CALENDAR_RECURRING_EVENT_ID=value))
    stranded = []
    assert _load_profile("TEST", stranded) is None
    assert [s.reason for s in stranded] == [REASON_BAD_CALENDAR_SERIES_ID]
    assert str(value) not in stranded[0].detail


@pytest.mark.parametrize("handler_type,rail", [(BtcTransferHandler, "btc"), (WiseTransferHandler, "wise")])
@pytest.mark.parametrize("field,value", [
    ("configured", "x" * 1025), ("configured", "bad\nseries"), ("configured", 17),
    ("event", "x" * 1025), ("event", "bad\x7fseries"), ("event", 17),
    ("recurrence", "other-series"), ("recurrence", " "),
    ("recurrence", 17), ("recurrence", "x" * 1025),
    ("recurrence", "bad\nseries"),
])
def test_matcher_rejects_invalid_and_conflicting_ids(handler_type, rail, field, value):
    profile = _payment_profile(rail)
    event = _session(SERIES, recurring=None)
    if field == "configured":
        profile.calendar_recurring_event_id = value
    elif field == "event":
        event["id"] = value
    else:
        event["recurringEventId"] = value
    assert event_matches_profile(profile, event) is False
    assert handler_type(profile).matches([event]) == []


@pytest.mark.parametrize("handler_type,rail", [(BtcTransferHandler, "btc"), (WiseTransferHandler, "wise")])
def test_instance_and_parent_identity_and_start_type(handler_type, rail):
    profile = _payment_profile(rail)
    instance = _session("copy-a")
    offset_copy = _session("copy-b")
    offset_copy["start"]["dateTime"] = "2026-11-11T20:00:00Z"
    parent = _session(SERIES, recurring=None)
    assert len(handler_type(profile).matches([instance, offset_copy, parent])) == 1
    assert series_match_id(profile, instance) == series_match_id(profile, offset_copy)
    assert series_match_id(profile, parent) == series_match_id(profile, instance)
    all_day = _session("all-day")
    all_day["start"] = {"date": "2026-11-11"}
    assert session_start_key(all_day) == "date:2026-11-11"
    assert series_match_id(profile, all_day) != series_match_id(profile, instance)
    assert len(handler_type(profile).matches([all_day, instance])) == 2


@pytest.mark.parametrize("handler_type,rail", [(BtcTransferHandler, "btc"), (WiseTransferHandler, "wise")])
@pytest.mark.parametrize("order", ["lone_two_lone", "two_lone_two"])
@pytest.mark.parametrize("state", ["awaiting", "rejected", "paid"])
def test_series_identity_survives_calendar_copy_changes(env, handler_type, rail, order, state):
    world, neo = env
    world.handler = handler_type(_payment_profile(rail))
    first = [_session("event-b")]
    both = [_session("event-b"), _session("event-a")]
    views = [first, both, first] if order == "lone_two_lone" else [both, first, both]
    world.calendars = {"primary": views[0]}
    world.tick()
    key = neo.only["payment_key"]
    session = neo.only["session"]
    if state == "rejected":
        neo.resolve(key, "rejected", "no")
    elif state == "paid":
        payment_journal.record_intent(consent_email.journal_path(), key, "synthetic-token", generation=0)
        payment_journal.record_outcome(consent_email.journal_path(), key, "sent")
        payment_journal.set_session_state(
            consent_email.journal_path(), f"test:{session}", payment_journal.SESSION_SETTLED
        )
    for view in views[1:]:
        world.calendars = {"primary": view}
        world.tick(minutes=15)
    assert neo.writes == 1
    assert len(neo.rows) == 1
    assert world.handler.profile.calendar_recurring_event_id == SERIES
    assert world.handler.name + ":" + session in world.journal["sessions"]


@pytest.mark.parametrize("handler_type,rail", [(BtcTransferHandler, "btc"), (WiseTransferHandler, "wise")])
@pytest.mark.parametrize("old_id", ["event-a", "event-b"])
@pytest.mark.parametrize("state", ["awaiting", "rejected", "paid"])
def test_old_event_id_keys_remain_recognized(env, handler_type, rail, old_id, state):
    world, neo = env
    world.handler = handler_type(_payment_profile(rail))
    world.calendars = {"primary": [_session("event-b"), _session("event-a")]}
    selected = world.handler.matches(world.calendars["primary"])[0]
    item = consent_email.build_pending_items([(world.handler, [selected])], "2026-11-11")[0]
    old_key = consent_email.obligation_key(world.handler, "2026-11-11", old_id)
    if state == "paid":
        payment_journal.record_intent(consent_email.journal_path(), old_key, "synthetic-token", generation=0)
        payment_journal.record_outcome(consent_email.journal_path(), old_key, "sent")
    else:
        payment_approval.raise_request(replace(item, obligation=old_key, match_id=old_id))
        if state == "rejected":
            neo.resolve(old_key, "rejected", "no")
    payment_journal.note_pending(consent_email.journal_path(), f"test:{old_id}")
    world.tick()
    world.tick(minutes=15)
    assert neo.writes == (0 if state == "paid" else 1)
    assert len(neo.rows) == (0 if state == "paid" else 1)
    assert item.obligation not in neo.rows


@pytest.mark.parametrize("handler_type,rail", [(BtcTransferHandler, "btc"), (WiseTransferHandler, "wise")])
@pytest.mark.parametrize("state", [payment_journal.SESSION_SKIPPED, payment_journal.SESSION_SETTLED])
def test_old_terminal_session_key_suppresses_new_approval(env, handler_type, rail, state):
    world, neo = env
    world.handler = handler_type(_payment_profile(rail))
    world.calendars = {"primary": [_session("event-b"), _session("event-a")]}
    payment_journal.note_pending(consent_email.journal_path(), "test:event-b")
    payment_journal.set_session_state(consent_email.journal_path(), "test:event-b", state)
    world.tick()
    assert neo.writes == 0


def test_mismatch_notice_dedupes_across_ticks_and_restart(env, monkeypatch, tmp_path):
    from lib.notify import Notifier

    world, neo = env
    world.handler = BtcTransferHandler(_payment_profile("btc"))
    world.calendars = {"primary": [_session("other", recurring="other-series")]}
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    sent = []

    def notifier():
        n = Notifier(rubric={})
        n._in_silence_window = lambda: False
        n._deliver = lambda message, **kwargs: sent.append(message) or True
        n._deliver_email = lambda message: sent.append(message) or True
        return n

    current = notifier()
    monkeypatch.setattr(monedula, "_notify", _REAL_NOTIFY)
    monkeypatch.setattr(monedula, "_get_notifier", lambda: current)
    world.tick()
    world.tick(minutes=15)
    current = notifier()  # same dedupe file, new process-equivalent instance
    world.tick(minutes=15)
    notices = [m for m in sent if "not in its configured calendar series" in m]
    assert len(notices) == 1
    assert "1 event(s) on 2026-11-11" in notices[0]
    assert "other-series" not in notices[0] and "other" not in notices[0]
    assert neo.writes == 0
    world.calendars = {"primary": [_session("plain", title="Unrelated activity")]}
    world.tick(minutes=15)
    world.calendars = {"primary": [_session("instance")]}
    world.tick(minutes=15)
    assert len([m for m in sent if "not in its configured calendar series" in m]) == 1


@pytest.mark.parametrize("event,expected_writes", [
    (_session("unrelated", title="Unrelated activity"), 0),
    (_session("instance"), 1),
])
def test_no_mismatch_notice_without_wrong_series_keyword_hit(env, monkeypatch, event, expected_writes):
    world, neo = env
    world.handler = WiseTransferHandler(_payment_profile("wise"))
    world.calendars = {"primary": [event]}
    notices = []
    monkeypatch.setattr(monedula, "_notify", lambda message, **kwargs: notices.append(message))
    world.tick()
    assert not [message for message in notices if "configured calendar series" in message]
    assert neo.writes == expected_writes
