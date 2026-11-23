# ruff: noqa: F811  (the shared ``env`` fixture is imported, not redefined)
"""Series scoped payment matching and approval effects, with synthetic data."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import consent_email  # noqa: E402
import monedula  # noqa: E402
import payment_journal  # noqa: E402
from handler_base import event_matches_profile, valid_calendar_id  # noqa: E402
from handlers import btc_transfer, wise_transfer  # noqa: E402
from handlers.btc_transfer import BtcTransferHandler  # noqa: E402
from handlers.payment_profile import (  # noqa: E402
    load_profiles_with_neotoma_fallback,
    PaymentProfile,
    _load_profile,
    load_profiles_from_neotoma,
)
from handlers.wise_transfer import WiseTransferHandler  # noqa: E402
from strandings import REASON_BAD_CALENDAR_SERIES_ID  # noqa: E402
from test_payment_approval import env  # noqa: E402,F401
from test_session_payments import SESSION_END, _event  # noqa: E402
from test_strandings import _profile, _serve  # noqa: E402


SERIES = "synthetic-series"
RAILS = [(BtcTransferHandler, "btc"), (WiseTransferHandler, "wise")]
_REAL_NOTIFY = monedula._notify


def _payment_profile(payment_type="wise", series=SERIES) -> PaymentProfile:
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


def _session(eid, *, recurring=SERIES, title="Movement class", end=SESSION_END):
    event = _event(eid, title, end)
    if recurring is not None:
        event["recurringEventId"] = recurring
    return event


@pytest.mark.parametrize("handler_type,rail", RAILS)
def test_series_match_requires_title_and_exact_identity_on_both_rails(handler_type, rail):
    handler = handler_type(_payment_profile(rail))
    wrong = _session("wrong", recurring="other-series")
    missing = _session("missing", recurring=None)
    instance = _session("instance")
    parent = _session(SERIES, recurring=None)
    wrong_title = _session("wrong-title", title="Other activity")
    assert handler.matches([wrong, missing, wrong_title]) == []
    assert [m["event"]["id"] for m in handler.matches([instance, parent])] == [
        "instance", SERIES,
    ]
    handler.profile.calendar_recurring_event_id = ""
    assert [m["event"]["id"] for m in handler.matches([wrong, missing])] == [
        "wrong", "missing",
    ]
    handler.profile.calendar_recurring_event_id = SERIES
    assert handler.matches([_session("case", recurring=SERIES.upper())]) == []


BAD_CONFIGURED = [
    " ",
    " " * 1025,
    " " * 1024 + "x",
    "x" * 1025,
    "bad\nseries",
    "bad\x7fseries",
    "bad\x85series",
    " " + SERIES,
    SERIES + " ",
    27,
    [SERIES],
]


@pytest.mark.parametrize("handler_type,rail", RAILS)
@pytest.mark.parametrize("value", BAD_CONFIGURED)
def test_invalid_configured_series_fails_closed_in_shared_matcher(handler_type, rail, value):
    profile = _payment_profile(rail, value)
    event = _session(SERIES, recurring=None)
    assert event_matches_profile(profile, event) is False
    assert handler_type(profile).matches([event]) == []


@pytest.mark.parametrize("rail", ["btc", "wise"])
@pytest.mark.parametrize("value", BAD_CONFIGURED)
def test_invalid_configured_series_strands_both_loaders(rail, value, monkeypatch, caplog):
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-token")
    _serve(monkeypatch, {"entities": [_profile(
        payment_type=rail, calendar_recurring_event_id=value,
    )]})
    stranded = []
    assert load_profiles_from_neotoma(stranded) == []
    assert [s.reason for s in stranded] == [REASON_BAD_CALENDAR_SERIES_ID]
    assert stranded[0].detail == (
        "Calendar series id is invalid; this profile was held. "
        "Check its type, length and control characters."
    )

    monkeypatch.setenv("TEST_CALENDAR_KEYWORDS", "movement")
    monkeypatch.setenv("TEST_PAYMENT_TYPE", rail)
    monkeypatch.setenv("TEST_AMOUNT_EUR", "10")
    monkeypatch.setattr(os, "environ", dict(os.environ, TEST_CALENDAR_RECURRING_EVENT_ID=value))
    stranded = []
    assert _load_profile("TEST", stranded) is None
    assert [s.reason for s in stranded] == [REASON_BAD_CALENDAR_SERIES_ID]
    assert stranded[0].detail == (
        "Calendar series id is invalid; this profile was held. "
        "Check its type, length and control characters."
    )
    assert all("Invalid calendar series id" in r.message or
               "malformed calendar series id" in r.message
               for r in caplog.records if "calendar series id" in r.message)


@pytest.mark.parametrize("handler_type,rail", RAILS)
@pytest.mark.parametrize("value", [None, ""])
def test_unset_series_keeps_keyword_only_matching(handler_type, rail, value):
    profile = _payment_profile(rail, value)
    assert len(handler_type(profile).matches([_session("other", recurring="another")])) == 1


@pytest.mark.parametrize("rail", ["btc", "wise"])
@pytest.mark.parametrize("value", [None, ""])
def test_unset_series_loads_as_keyword_only(rail, value, monkeypatch):
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-token")
    _serve(monkeypatch, {"entities": [_profile(
        payment_type=rail, calendar_recurring_event_id=value,
    )]})
    assert load_profiles_from_neotoma()[0].calendar_recurring_event_id == ""
    monkeypatch.setenv("TEST_CALENDAR_KEYWORDS", "movement")
    monkeypatch.setenv("TEST_PAYMENT_TYPE", rail)
    monkeypatch.setenv("TEST_AMOUNT_EUR", "10")
    monkeypatch.setenv("TEST_CALENDAR_RECURRING_EVENT_ID", value or "")
    assert _load_profile("TEST").calendar_recurring_event_id == ""


@pytest.mark.parametrize("handler_type,rail", RAILS)
@pytest.mark.parametrize("field,value", [
    ("id", " " + SERIES), ("id", SERIES + " "),
    ("id", "x" * 1025), ("id", "bad\nvalue"), ("id", 17),
    ("recurringEventId", "other-series"), ("recurringEventId", ""),
    ("recurringEventId", " " + SERIES), ("recurringEventId", SERIES + " "),
    ("recurringEventId", "x" * 1025), ("recurringEventId", "bad\x7fvalue"),
    ("recurringEventId", 17),
])
def test_invalid_event_ids_and_conflicting_recurrence_never_match(handler_type, rail, field, value):
    profile = _payment_profile(rail)
    event = _session(SERIES, recurring=None)
    event[field] = value
    assert not event_matches_profile(profile, event)
    assert handler_type(profile).matches([event]) == []


@pytest.mark.parametrize("handler_type,rail", RAILS)
@pytest.mark.parametrize("bad_id", [" " + SERIES, SERIES + " ", "x" * 1025, 17])
def test_invalid_instance_id_fails_even_with_matching_recurrence(handler_type, rail, bad_id):
    profile = _payment_profile(rail)
    event = _session(bad_id)
    assert not event_matches_profile(profile, event)
    assert handler_type(profile).matches([event]) == []


@pytest.mark.parametrize("handler_type,rail", RAILS)
def test_instance_parent_and_missing_recurrence_rules(handler_type, rail):
    handler = handler_type(_payment_profile(rail))
    instance = _session("instance")
    parent = _session(SERIES, recurring=None)
    other_parent = _session("other", recurring=None)
    conflicting = _session(SERIES, recurring="another-series")
    assert [m["event"]["id"] for m in handler.matches(
        [instance, parent, other_parent, conflicting]
    )] == ["instance", SERIES]
    assert valid_calendar_id("x" * 1024)
    assert not valid_calendar_id("x" * 1025)


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
        assert "fields" not in params
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"items": response_events}), ""
        )

    monkeypatch.setattr(module.subprocess, "run", fake_gws)
    profile = _payment_profile()
    assert module._find_next_event_due_date(profile) is None
    response_events.append(_session("instance"))
    assert module._find_next_event_due_date(profile) == "2026-11-12"


@pytest.mark.parametrize("handler_type,rail", RAILS)
def test_calendar_copies_raise_two_approvals_by_design(env, handler_type, rail):
    world, neo = env
    world.handler = handler_type(_payment_profile(rail))
    world.calendars = {
        "primary": [_session("event-b")],
        "shared": [_session("event-a")],
    }
    world.tick()
    assert neo.writes == 2  # Follow-up: identity for moved copies and old approvals.
    assert {row["session"] for row in neo.rows.values()} == {"event-a", "event-b"}
    assert {row["payment_key"] for row in neo.rows.values()} == {
        consent_email.obligation_key(world.handler, "2026-11-11", eid)
        for eid in ("event-a", "event-b")
    }


@pytest.mark.parametrize("handler_type,rail", RAILS)
def test_rejected_event_key_stays_rejected_on_next_tick(env, handler_type, rail):
    world, neo = env
    world.handler = handler_type(_payment_profile(rail))
    world.calendars = {"primary": [_session("event-a")]}
    world.tick()
    key = neo.only["payment_key"]
    neo.resolve(key, "rejected", "no")
    world.tick(minutes=15)
    assert neo.writes == 1
    assert neo.rows[key]["status"] == "rejected"
    assert payment_journal.session_state(world.journal, "test:event-a") == "skipped"


@pytest.mark.parametrize("handler_type,rail", RAILS)
@pytest.mark.parametrize("series", [SERIES, ""])
def test_series_and_unset_profiles_keep_origin_main_event_keys(env, handler_type, rail, series):
    world, _neo = env
    handler = handler_type(_payment_profile(rail, series))
    event = _session("event-a")
    (item,) = consent_email.build_pending_items(
        [(handler, handler.matches([event]))], "2026-11-11"
    )
    assert item.match_id == "event-a"
    basis = "|".join([
        handler.name, "2026-11-11", "event-a",
        consent_email.payee_identity(handler), "10", "EUR",
    ])
    assert item.obligation == hashlib.sha256(basis.encode()).hexdigest()[:32]
    assert payment_journal.session_key(handler.name, item.match_id) == "test:event-a"


def test_consent_builder_excludes_wrong_series_without_deduplication():
    handler = BtcTransferHandler(_payment_profile("btc"))
    events = [_session("wrong", recurring="other-series"), _session("b"), _session("a")]
    matches = [{"event": e, "summary": e["summary"]} for e in events]
    items = consent_email.build_pending_items([(handler, matches)], "2026-11-11")
    assert [item.match_id for item in items] == ["b", "a"]
    _subject, body = consent_email.build_request_body(items, superseded=False)
    assert "wrong" not in body


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
    current = notifier()
    world.tick(minutes=15)
    notices = [m for m in sent if "not in its configured calendar series" in m]
    assert len(notices) == 1
    assert "1 event(s) on 2026-11-11" in notices[0]
    assert "other-series" not in notices[0] and "other" not in notices[0]
    assert neo.writes == 0
    world.calendars = {"primary": [_session("plain", title="Unrelated activity")]}
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


@pytest.mark.parametrize("handler_type,rail", RAILS)
@pytest.mark.parametrize("value", [None, "", "   ", 17, False])
def test_present_empty_or_null_recurrence_is_not_a_series_parent(handler_type, rail, value):
    """A recurringEventId that is present but null or blank is recurrence metadata, never the
    'no metadata' parent case, even when the event's own id equals the series id."""
    profile = _payment_profile(rail)
    event = _session(SERIES, recurring=None)
    event["recurringEventId"] = value
    assert not event_matches_profile(profile, event)
    assert handler_type(profile).matches([event]) == []
    handler = handler_type(profile)
    items = consent_email.build_pending_items(
        [(handler, [{"event": event, "summary": event["summary"]}])], "2026-11-11"
    )
    assert items == []
    # control: the same event with the field genuinely absent is the series parent
    parent = _session(SERIES, recurring=None)
    assert "recurringEventId" not in parent
    assert len(handler.matches([parent])) == 1


@pytest.mark.parametrize("rail", ["btc", "wise"])
@pytest.mark.parametrize("pass_strandings", [True, False])
def test_invalid_series_profile_is_held_not_replaced_by_env_profile(
    rail, pass_strandings, monkeypatch
):
    """When the Neotoma profile is stranded for an invalid series id, the env-var profile
    (which has no series id) must not take its place and resume title-only matching."""
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-token")
    _serve(monkeypatch, {"entities": [_profile(
        payment_type=rail, calendar_recurring_event_id="x" * 1025,
    )]})
    monkeypatch.setenv("MONEDULA_PROFILES", "TEST")
    monkeypatch.setenv("TEST_LABEL", "Example class")
    monkeypatch.setenv("TEST_CALENDAR_KEYWORDS", "movement")
    monkeypatch.setenv("TEST_PAYMENT_TYPE", rail)
    monkeypatch.setenv("TEST_AMOUNT_EUR", "10")
    stranded = [] if pass_strandings else None
    assert load_profiles_with_neotoma_fallback(stranded) == []
    if pass_strandings:
        assert [s.reason for s in stranded] == [REASON_BAD_CALENDAR_SERIES_ID]


def test_env_fallback_still_used_when_neotoma_has_no_profiles(monkeypatch):
    """Control: the env-var fallback is unchanged when nothing was stranded for a series id."""
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-token")
    _serve(monkeypatch, {"entities": []})
    monkeypatch.setenv("MONEDULA_PROFILES", "TEST")
    monkeypatch.setenv("TEST_LABEL", "Example class")
    monkeypatch.setenv("TEST_CALENDAR_KEYWORDS", "movement")
    monkeypatch.setenv("TEST_PAYMENT_TYPE", "btc")
    monkeypatch.setenv("TEST_AMOUNT_EUR", "10")
    monkeypatch.setenv("TEST_BTC_ADDRESS", "synthetic-address")
    profiles = load_profiles_with_neotoma_fallback([])
    assert [p.prefix for p in profiles] == ["TEST"]


@pytest.mark.parametrize("handler_type,rail", RAILS)
@pytest.mark.parametrize("event,expected_writes", [
    (_session("other-instance", recurring="other-series"), 0),
    (_session("no-recurrence"), 0),
    (_session("instance"), 1),
])
def test_wrong_series_keyword_event_raises_no_approval_end_to_end(
    env, monkeypatch, handler_type, rail, event, expected_writes
):
    """The reported defect through the real tick on BOTH rails: a keyword-matching event outside
    the configured series writes no payment_approval; the real series instance writes one."""
    if event["id"] == "no-recurrence":
        event.pop("recurringEventId", None)
    world, neo = env
    world.handler = handler_type(_payment_profile(rail))
    world.calendars = {"primary": [event]}
    monkeypatch.setattr(monedula, "_notify", lambda message, **kwargs: None)
    world.tick()
    assert neo.writes == expected_writes


def _env_profile(monkeypatch, rail="btc"):
    monkeypatch.setenv("MONEDULA_PROFILES", "TEST")
    monkeypatch.setenv("TEST_LABEL", "Example class")
    monkeypatch.setenv("TEST_CALENDAR_KEYWORDS", "movement")
    monkeypatch.setenv("TEST_PAYMENT_TYPE", rail)
    monkeypatch.setenv("TEST_AMOUNT_EUR", "10")
    monkeypatch.setenv("TEST_BTC_ADDRESS", "synthetic-address")


def _fail_fetch(monkeypatch, error):
    def _raise(req, timeout=None):
        raise error
    monkeypatch.setattr("urllib.request.urlopen", _raise)


@pytest.mark.parametrize("failure", [
    "unreachable", "server_error", "rejected", "bad_amount", "invalid_series",
])
@pytest.mark.parametrize("pass_strandings", [True, False])
def test_configured_neotoma_source_that_yields_no_profile_holds_the_env_fallback(
    failure, pass_strandings, monkeypatch
):
    """A configured Neotoma source that fails, is rejected, or strands every active profile must
    not be replaced by an env-var profile (it has no series binding: title-only matching)."""
    import io
    import urllib.error

    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-token")
    _env_profile(monkeypatch)
    if failure == "unreachable":
        _fail_fetch(monkeypatch, urllib.error.URLError("synthetic outage"))
    elif failure == "server_error":
        _fail_fetch(monkeypatch, urllib.error.HTTPError(
            "https://example.invalid", 503, "Unavailable", {}, io.BytesIO(b"")))
    elif failure == "rejected":
        _fail_fetch(monkeypatch, urllib.error.HTTPError(
            "https://example.invalid", 403, "Forbidden", {}, io.BytesIO(b"")))
    elif failure == "bad_amount":
        _serve(monkeypatch, {"entities": [_profile(
            payment_type="btc", amount_eur="not-a-number",
            calendar_recurring_event_id=SERIES,
        )]})
    else:
        _serve(monkeypatch, {"entities": [_profile(
            payment_type="btc", calendar_recurring_event_id="x" * 1025,
        )]})
    stranded = [] if pass_strandings else None
    assert load_profiles_with_neotoma_fallback(stranded) == []
    if pass_strandings:
        assert stranded, "the stranding must still be escalated by the caller"


def test_env_fallback_remains_for_env_only_install(monkeypatch):
    """Control: with no Neotoma base URL (an env-only install) the env profile loads as before."""
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    _env_profile(monkeypatch)
    profiles = load_profiles_with_neotoma_fallback([])
    assert [p.prefix for p in profiles] == ["TEST"]


@pytest.mark.parametrize("rail", ["btc", "wise"])
@pytest.mark.parametrize("body", [
    {"error": "synthetic"}, {}, {"entities": None}, {"entities": "none"},
    {"entities": [17]}, "unexpected", 17, None,
])
@pytest.mark.parametrize("pass_strandings", [True, False])
def test_successful_response_without_a_valid_profile_collection_is_a_rejected_read(
    rail, body, pass_strandings, monkeypatch
):
    """A 2xx body that lacks an explicit, valid profile collection must not read as 'no profiles
    configured' (which enables the env-var fallback); it holds, on both rails' configurations."""
    from strandings import REASON_FETCH_REJECTED

    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-token")
    _env_profile(monkeypatch, rail)
    _serve(monkeypatch, body)
    stranded = [] if pass_strandings else None
    assert load_profiles_with_neotoma_fallback(stranded) == []
    if pass_strandings:
        assert [s.reason for s in stranded] == [REASON_FETCH_REJECTED]


@pytest.mark.parametrize("key", ["entities", "items", "results"])
def test_explicit_empty_collection_still_allows_the_env_fallback(key, monkeypatch):
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-token")
    _env_profile(monkeypatch)
    _serve(monkeypatch, {key: []})
    assert [p.prefix for p in load_profiles_with_neotoma_fallback([])] == ["TEST"]
    _serve(monkeypatch, [])
    assert [p.prefix for p in load_profiles_with_neotoma_fallback([])] == ["TEST"]
