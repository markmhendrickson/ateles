# ruff: noqa: F811  (the shared ``env`` fixture is imported, not redefined)
"""Series scoped payment matching and approval effects, with synthetic data."""

from __future__ import annotations

import sys
import json
import subprocess
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
from test_payment_approval import env  # noqa: E402,F401
from test_session_payments import SESSION_END, _event  # noqa: E402
from test_strandings import _profile, _serve  # noqa: E402
from strandings import REASON_BAD_CALENDAR_SERIES_ID  # noqa: E402


SERIES = "synthetic-series"


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
    assert neo.only["session"] == "event-a"
    assert neo.only["payment_key"] == consent_email.obligation_key(
        world.handler, "2026-11-11", "event-a"
    )
    # The second session has a different start and therefore its own key.
    world.calendars["primary"].append(
        _session("event-c", end=SESSION_END - timedelta(hours=2))
    )
    world.tick(minutes=15)
    assert neo.writes == 2
    assert len(neo.rows) == 2
    assert {row["session"] for row in neo.rows.values()} == {"event-a", "event-c"}


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
    assert payment_journal.session_state(world.journal, "test:event-a") == "skipped"


def test_consent_builder_excludes_wrong_series_and_duplicate_copy():
    handler = BtcTransferHandler(_payment_profile("btc"))
    events = [_session("wrong", recurring="other-series"), _session("b"), _session("a")]
    matches = [{"event": e, "summary": e["summary"]} for e in events]
    items = consent_email.build_pending_items([(handler, matches)], "2026-11-11")
    assert len(items) == 1
    assert items[0].match_id == "a"
    _subject, body = consent_email.build_request_body(items, superseded=False)
    assert "wrong" not in body
