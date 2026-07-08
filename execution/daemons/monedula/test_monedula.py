"""
test_monedula.py — Unit tests for the rebuilt Monedula daemon.

Covers, per the rebuild spec:
  - event-end selection: past-end matched vs future-end skipped vs
    all-day ignored
  - marker dedup: the same event is not re-notified on a later tick
  - reply parsing: yes / no / garbage
  - skip rolls due_date WITHOUT calling execute() (never pays a skipped
    session)
  - approve calls execute() exactly once and marks the marker 'paid'
    (idempotency: a second sweep does not call execute() again)

Also covers, per the 2026-07-06 parquet-free/entity-ID redesign:
  - calendar_recurring_event_id matching selects ONLY the configured event,
    even when a second event's title contains the same keyword (reproduces
    the real "Therapy in-person" vs "Walk to therapy" duplicate-match bug)
  - keyword fallback still works for profiles with no id configured
  - Wise recipient resolution succeeds from a Neotoma contact even when
    contacts.parquet / DATA_DIR is entirely absent

No real network calls are made — gws/Gmail/handler.execute/Neotoma HTTP are
all mocked.
"""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gmail_channel  # noqa: E402
import markers  # noqa: E402
import monedula  # noqa: E402
from handlers.payment_profile import PaymentProfile  # noqa: E402
from handlers.wise_transfer import WiseTransferHandler  # noqa: E402
from handlers.btc_transfer import BtcTransferHandler  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_markers_file(tmp_path, monkeypatch):
    """Redirect markers.MARKERS_FILE to a scratch file per test."""
    scratch = tmp_path / ".monedula_pending.json"
    monkeypatch.setattr(markers, "MARKERS_FILE", scratch)
    yield scratch


@dataclass
class FakeProfile:
    name: str = "yoga"
    label: str = "Yoga"
    payment_type: str = "btc"
    amount_eur: int = 60
    calendar_keywords: tuple = ("yoga",)
    btc_address: str = "bc1qexampleexampleexampleexampleexample"
    wise_reference: str = ""
    neotoma_task_id: str = "ent_yoga_task"


class FakeHandler:
    """Minimal PaymentHandler-shaped stand-in for tests."""

    def __init__(self, profile: FakeProfile, execute_result: dict | None = None):
        self.profile = profile
        self._execute_result = execute_result or {"status": "sent", "txid": "abc123"}
        self.execute_calls: list[dict] = []

    @property
    def name(self) -> str:
        return self.profile.name

    def matches(self, events: list[dict]) -> list[dict]:
        out = []
        for event in events:
            summary = (event.get("summary") or "").lower()
            if any(kw in summary for kw in self.profile.calendar_keywords):
                out.append({"event": event, "summary": event.get("summary", "")})
        return out

    def preview(self, match: dict) -> str:
        return f"preview for {match.get('summary')}"

    def execute(self, match: dict) -> dict:
        self.execute_calls.append(match)
        return dict(self._execute_result)

    def format_confirmation(self, result: dict) -> str:
        return f"confirmation: {result}"


def _timed_event(event_id: str, end_dt: datetime, summary: str = "Yoga class") -> dict:
    start_dt = end_dt - timedelta(hours=1)
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start_dt.isoformat()},
        "end": {"dateTime": end_dt.isoformat()},
    }


def _all_day_event(event_id: str, day: str, summary: str = "Yoga retreat") -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "start": {"date": day},
        "end": {"date": day},
    }


MADRID = timezone(timedelta(hours=2))


# ---------------------------------------------------------------------------
# Event-end selection
# ---------------------------------------------------------------------------


def test_past_end_event_is_selected():
    now = datetime(2026, 7, 3, 12, 0, tzinfo=MADRID)
    past_event = _timed_event("evt-past", now - timedelta(minutes=30))
    handler = FakeHandler(FakeProfile())

    selected = monedula.select_newly_ended_sessions([past_event], [handler], now=now)

    assert len(selected) == 1
    assert selected[0].event_id == "evt-past"


def test_future_end_event_is_skipped():
    now = datetime(2026, 7, 3, 12, 0, tzinfo=MADRID)
    future_event = _timed_event("evt-future", now + timedelta(minutes=30))
    handler = FakeHandler(FakeProfile())

    selected = monedula.select_newly_ended_sessions([future_event], [handler], now=now)

    assert selected == []


def test_all_day_event_is_ignored():
    now = datetime(2026, 7, 3, 12, 0, tzinfo=MADRID)
    all_day = _all_day_event("evt-allday", "2026-07-03")
    handler = FakeHandler(FakeProfile())

    selected = monedula.select_newly_ended_sessions([all_day], [handler], now=now)

    assert selected == []


def test_non_matching_event_is_ignored():
    now = datetime(2026, 7, 3, 12, 0, tzinfo=MADRID)
    unrelated = _timed_event("evt-other", now - timedelta(minutes=30), summary="Dentist")
    handler = FakeHandler(FakeProfile())

    selected = monedula.select_newly_ended_sessions([unrelated], [handler], now=now)

    assert selected == []


# ---------------------------------------------------------------------------
# Marker dedup
# ---------------------------------------------------------------------------


def test_already_notified_event_not_selected_again():
    now = datetime(2026, 7, 3, 12, 0, tzinfo=MADRID)
    past_event = _timed_event("evt-dup", now - timedelta(minutes=30))
    handler = FakeHandler(FakeProfile())

    first_pass = monedula.select_newly_ended_sessions([past_event], [handler], now=now)
    assert len(first_pass) == 1

    # Simulate notify writing a marker.
    markers.save(
        markers.Marker(
            event_id="evt-dup",
            date=first_pass[0].session_date,
            profile_name="yoga",
            status="awaiting_approval",
        )
    )

    second_pass = monedula.select_newly_ended_sessions([past_event], [handler], now=now)
    assert second_pass == []


def test_notify_ended_sessions_writes_marker_with_gmail_ids(monkeypatch):
    now = datetime(2026, 7, 3, 12, 0, tzinfo=MADRID)
    event = _timed_event("evt-notify", now - timedelta(minutes=10))
    handler = FakeHandler(FakeProfile())
    ended = monedula.select_newly_ended_sessions([event], [handler], now=now)
    assert len(ended) == 1

    sent = {"id": "msg-1", "threadId": "thread-1"}
    with mock.patch.object(gmail_channel, "send_email", return_value=sent) as m_send:
        monedula.notify_ended_sessions(ended)

    m_send.assert_called_once()
    marker = markers.get("evt-notify", ended[0].session_date)
    assert marker is not None
    assert marker.status == "awaiting_approval"
    assert marker.gmail_thread_id == "thread-1"
    assert marker.gmail_message_id == "msg-1"


def test_notify_failure_does_not_write_marker():
    now = datetime(2026, 7, 3, 12, 0, tzinfo=MADRID)
    event = _timed_event("evt-fail", now - timedelta(minutes=10))
    handler = FakeHandler(FakeProfile())
    ended = monedula.select_newly_ended_sessions([event], [handler], now=now)

    with mock.patch.object(gmail_channel, "send_email", return_value=None):
        monedula.notify_ended_sessions(ended)

    assert markers.get("evt-fail", ended[0].session_date) is None


def test_notify_missing_thread_id_does_not_write_marker():
    # A send that returns a message id but NO threadId must not write a marker:
    # the sweep needs the thread id to find the reply, so a thread-less marker
    # would sit awaiting_approval forever (never paid, never re-notified).
    now = datetime(2026, 7, 3, 12, 0, tzinfo=MADRID)
    event = _timed_event("evt-nothread", now - timedelta(minutes=10))
    handler = FakeHandler(FakeProfile())
    ended = monedula.select_newly_ended_sessions([event], [handler], now=now)

    with mock.patch.object(gmail_channel, "send_email", return_value={"id": "msg-1"}):
        monedula.notify_ended_sessions(ended)

    # No marker → the next tick re-attempts notify (session not lost).
    assert markers.get("evt-nothread", ended[0].session_date) is None


def test_empty_keyword_does_not_match_everything():
    # A blank keyword must not turn into a wildcard that matches (and could pay
    # for) unrelated sessions in either match mode.
    from handler_base import match_events_for_profile

    profile = PaymentProfile(
        prefix="THERAPY",
        label="Therapy",
        calendar_keywords=["", "  "],  # only blank keywords
        payment_type="wise",
        amount_eur=60,
    )
    events = [
        {"id": "e1", "summary": "Completely unrelated event", "recurringEventId": None},
        {"id": "e2", "summary": "Dentist", "recurringEventId": None},
    ]
    assert match_events_for_profile(profile, events, "therapy") == []

    profile_prefix = PaymentProfile(
        prefix="THERAPY",
        label="Therapy",
        calendar_keywords=[""],
        payment_type="wise",
        amount_eur=60,
        keyword_match_mode="prefix",
    )
    assert match_events_for_profile(profile_prefix, events, "therapy") == []


def test_notify_is_capped_per_run(monkeypatch):
    # With a wide lookback, an empty marker file could surface many ended
    # sessions at once. The per-run cap must bound how many emails one tick
    # sends, so a marker reset can never trigger an unbounded burst.
    monkeypatch.setattr(monedula, "MAX_NOTIFY_PER_RUN", 3)
    now = datetime(2026, 7, 3, 12, 0, tzinfo=MADRID)
    events = [
        _timed_event(f"evt-cap-{i}", now - timedelta(hours=i + 1)) for i in range(7)
    ]
    handler = FakeHandler(FakeProfile())
    ended = monedula.select_newly_ended_sessions(events, [handler], now=now)
    assert len(ended) == 7  # all seven are genuinely ended + matched

    sent = {"id": "m", "threadId": "t"}
    with mock.patch.object(gmail_channel, "send_email", return_value=sent) as m_send:
        monedula.notify_ended_sessions(ended)

    # Only the cap's worth of emails go out this tick; the rest wait.
    assert m_send.call_count == 3


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("yes", "approve"),
        ("Yes", "approve"),
        ("YES\n\nOn Jul 3, operator wrote:\n> blah", "approve"),
        ("y", "approve"),
        ("approve", "approve"),
        ("pay", "approve"),
        ("attended", "approve"),
        ("no", "skip"),
        ("No thanks", "skip"),
        ("n", "skip"),
        ("skip", "skip"),
        ("garbage nonsense", None),
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_parse_approval_reply(text, expected):
    assert gmail_channel.parse_approval_reply(text) == expected


# ---------------------------------------------------------------------------
# Skip path: rolls due_date, never calls execute()
# ---------------------------------------------------------------------------


def test_skip_reply_rolls_due_date_without_executing(monkeypatch):
    profile = FakeProfile()
    handler = FakeHandler(profile)

    marker = markers.Marker(
        event_id="evt-skip",
        date="2026-07-02",
        profile_name="yoga",
        gmail_thread_id="thread-skip",
        gmail_message_id="msg-skip",
        status="awaiting_approval",
        extra={"match": {"event": {}, "summary": "Yoga"}},
    )
    markers.save(marker)

    with mock.patch.object(gmail_channel, "find_operator_reply", return_value="no"), \
         mock.patch.object(gmail_channel, "send_email", return_value={"id": "x", "threadId": "y"}), \
         mock.patch.object(monedula, "roll_due_date") as m_roll:
        monedula.sweep_pending_approvals([handler])

    assert handler.execute_calls == []  # never paid
    m_roll.assert_called_once()
    updated = markers.get("evt-skip", "2026-07-02")
    assert updated.status == "skipped"


def test_no_reply_yet_leaves_marker_pending():
    profile = FakeProfile()
    handler = FakeHandler(profile)

    marker = markers.Marker(
        event_id="evt-pending",
        date="2026-07-02",
        profile_name="yoga",
        gmail_thread_id="thread-pending",
        gmail_message_id="msg-pending",
        status="awaiting_approval",
        extra={"match": {"event": {}, "summary": "Yoga"}},
    )
    markers.save(marker)

    with mock.patch.object(gmail_channel, "find_operator_reply", return_value=None):
        monedula.sweep_pending_approvals([handler])

    assert handler.execute_calls == []
    updated = markers.get("evt-pending", "2026-07-02")
    assert updated.status == "awaiting_approval"


def test_unrecognised_reply_leaves_marker_pending():
    profile = FakeProfile()
    handler = FakeHandler(profile)

    marker = markers.Marker(
        event_id="evt-garbage",
        date="2026-07-02",
        profile_name="yoga",
        gmail_thread_id="thread-g",
        gmail_message_id="msg-g",
        status="awaiting_approval",
        extra={"match": {"event": {}, "summary": "Yoga"}},
    )
    markers.save(marker)

    with mock.patch.object(gmail_channel, "find_operator_reply", return_value="lol what"):
        monedula.sweep_pending_approvals([handler])

    assert handler.execute_calls == []
    updated = markers.get("evt-garbage", "2026-07-02")
    assert updated.status == "awaiting_approval"


# ---------------------------------------------------------------------------
# Approve path: calls execute() exactly once, marks paid, idempotent
# ---------------------------------------------------------------------------


def test_approve_reply_executes_payment_and_marks_paid():
    profile = FakeProfile()
    handler = FakeHandler(profile, execute_result={"status": "sent", "txid": "tx1"})

    marker = markers.Marker(
        event_id="evt-approve",
        date="2026-07-02",
        profile_name="yoga",
        gmail_thread_id="thread-a",
        gmail_message_id="msg-a",
        status="awaiting_approval",
        extra={"match": {"event": {}, "summary": "Yoga"}},
    )
    markers.save(marker)

    with mock.patch.object(gmail_channel, "find_operator_reply", return_value="yes"), \
         mock.patch.object(gmail_channel, "send_email", return_value={"id": "x", "threadId": "y"}):
        monedula.sweep_pending_approvals([handler])

    assert len(handler.execute_calls) == 1
    updated = markers.get("evt-approve", "2026-07-02")
    assert updated.status == "paid"


def test_approve_is_idempotent_does_not_double_pay():
    """
    Once a marker is 'paid', a later sweep tick (e.g. a stray duplicate
    reply, or a re-run before the thread state settles) must NOT call
    execute() again.
    """
    profile = FakeProfile()
    handler = FakeHandler(profile, execute_result={"status": "sent", "txid": "tx1"})

    marker = markers.Marker(
        event_id="evt-idem",
        date="2026-07-02",
        profile_name="yoga",
        gmail_thread_id="thread-i",
        gmail_message_id="msg-i",
        status="paid",  # already paid from a prior sweep
        extra={"match": {"event": {}, "summary": "Yoga"}},
    )
    markers.save(marker)

    # pending_awaiting_approval() only returns awaiting_approval markers,
    # so an already-paid marker is not even considered by the sweep.
    with mock.patch.object(gmail_channel, "find_operator_reply") as m_reply:
        monedula.sweep_pending_approvals([handler])
        m_reply.assert_not_called()

    assert handler.execute_calls == []


def test_handle_approve_refuses_if_marker_not_awaiting_approval():
    """Direct guard test: _handle_approve bails if marker status raced away."""
    profile = FakeProfile()
    handler = FakeHandler(profile)

    marker = markers.Marker(
        event_id="evt-race",
        date="2026-07-02",
        profile_name="yoga",
        gmail_thread_id="thread-r",
        gmail_message_id="msg-r",
        status="awaiting_approval",
        extra={"match": {"event": {}, "summary": "Yoga"}},
    )
    markers.save(marker)
    # Simulate a race: status flips to 'paid' before _handle_approve's
    # internal re-check runs.
    markers.update_status("evt-race", "2026-07-02", "paid")

    monedula._handle_approve(handler, marker)

    assert handler.execute_calls == []


def test_execute_exception_does_not_mark_paid_and_notifies():
    profile = FakeProfile()
    handler = FakeHandler(profile)

    def boom(match):
        raise RuntimeError("network exploded")

    handler.execute = boom  # type: ignore[assignment]

    marker = markers.Marker(
        event_id="evt-boom",
        date="2026-07-02",
        profile_name="yoga",
        gmail_thread_id="thread-b",
        gmail_message_id="msg-b",
        status="awaiting_approval",
        extra={"match": {"event": {}, "summary": "Yoga"}},
    )
    markers.save(marker)

    with mock.patch.object(gmail_channel, "find_operator_reply", return_value="yes"), \
         mock.patch.object(gmail_channel, "send_email") as m_send:
        monedula.sweep_pending_approvals([handler])

    updated = markers.get("evt-boom", "2026-07-02")
    assert updated.status == "approved"  # not 'paid'
    m_send.assert_not_called()  # no confirmation email sent for a failed execute


# ---------------------------------------------------------------------------
# gmail_channel JSON parsing helper
# ---------------------------------------------------------------------------


def test_parse_json_output_strips_keyring_preamble():
    stdout = 'Using keyring backend: keyring\n{"id": "abc", "threadId": "t1"}\n'
    parsed = gmail_channel._parse_json_output(stdout)
    assert parsed == {"id": "abc", "threadId": "t1"}


def test_parse_json_output_returns_none_on_garbage():
    assert gmail_channel._parse_json_output("not json at all") is None
    assert gmail_channel._parse_json_output("") is None


# ---------------------------------------------------------------------------
# Fix B: stable calendar-event-id matching (reproduces the real
# "Therapy in-person" vs "Walk to therapy" duplicate-match bug from the
# 2026-07-06 live staged run, and confirms keyword matching still works when
# no id is configured).
# ---------------------------------------------------------------------------


def _real_therapy_in_person_event() -> dict:
    """The actual payable event from the 2026-07-06 staged run (id-matched)."""
    return {
        "id": "f77e80a6734c4d979c85b7ae296513ed",
        "summary": "Therapy in-person",
        "start": {"dateTime": "2026-07-02T17:30:00+02:00", "timeZone": "Europe/Madrid"},
        "end": {"dateTime": "2026-07-02T18:30:00+02:00", "timeZone": "Europe/Madrid"},
    }


def _real_walk_to_therapy_event() -> dict:
    """The actual incidental event that the old keyword match wrongly hit too."""
    return {
        "id": "f9bfc5c4dcc541e9a417f730b6b40717",
        "summary": "Walk to therapy",
        "start": {"dateTime": "2026-07-02T17:00:00+02:00", "timeZone": "Europe/Madrid"},
        "end": {"dateTime": "2026-07-02T17:30:00+02:00", "timeZone": "Europe/Madrid"},
    }


def _real_yoga_recurring_event() -> dict:
    """The actual yoga series instance from the staged run (recurringEventId set)."""
    return {
        "id": "ee0dad5f7f5d4879949ffdbbf65d9374_20260702T060000Z",
        "recurringEventId": "ee0dad5f7f5d4879949ffdbbf65d9374",
        "summary": "🧘‍♂️ Yoga with Manel",
        "start": {"dateTime": "2026-07-02T08:00:00+02:00", "timeZone": "Europe/Madrid"},
        "end": {"dateTime": "2026-07-02T09:30:00+02:00", "timeZone": "Europe/Madrid"},
    }


def test_event_id_allowlist_selects_only_configured_event_ignoring_keyword_overlap():
    """
    Reproduces the real duplicate-match bug: both events' titles contain
    "therapy", but only "Therapy in-person" is payable. With an explicit
    calendar_event_ids allowlist configured, exactly one event matches.
    """
    profile = PaymentProfile(
        prefix="THERAPY",
        label="Therapy",
        calendar_keywords=["therapy", "terapia"],
        payment_type="wise",
        amount_eur=60,
        calendar_event_ids=["f77e80a6734c4d979c85b7ae296513ed"],
    )
    handler = WiseTransferHandler(profile)

    events = [_real_therapy_in_person_event(), _real_walk_to_therapy_event()]
    matched = handler.matches(events)

    assert len(matched) == 1
    assert matched[0]["event"]["id"] == "f77e80a6734c4d979c85b7ae296513ed"
    assert matched[0]["summary"] == "Therapy in-person"


def test_recurring_event_id_selects_only_matching_series_instance():
    """
    Yoga profile with calendar_recurring_event_id configured matches only
    the event whose recurringEventId equals it, even alongside an unrelated
    event that happens to share a keyword.
    """
    profile = PaymentProfile(
        prefix="YOGA",
        label="Yoga",
        calendar_keywords=["yoga", "manel"],
        payment_type="btc",
        amount_eur=60,
        btc_address="bc1qexampleexampleexampleexampleexample",
        calendar_recurring_event_id="ee0dad5f7f5d4879949ffdbbf65d9374",
    )
    handler = BtcTransferHandler(profile)

    unrelated_same_keyword = {
        "id": "unrelated-1",
        "summary": "Chat with Manel about something else",
        "start": {"dateTime": "2026-07-02T10:00:00+02:00", "timeZone": "Europe/Madrid"},
        "end": {"dateTime": "2026-07-02T10:30:00+02:00", "timeZone": "Europe/Madrid"},
    }
    events = [_real_yoga_recurring_event(), unrelated_same_keyword]
    matched = handler.matches(events)

    assert len(matched) == 1
    assert matched[0]["event"]["recurringEventId"] == "ee0dad5f7f5d4879949ffdbbf65d9374"


def test_keyword_fallback_still_works_when_no_id_configured():
    """
    A profile with neither calendar_recurring_event_id nor
    calendar_event_ids configured must keep matching by keyword — this is
    the therapy profile's current real-world state, since its calendar
    event has no stable recurring/event id (recreated weekly with a new id).
    """
    profile = PaymentProfile(
        prefix="THERAPY",
        label="Therapy",
        calendar_keywords=["therapy", "terapia"],
        payment_type="wise",
        amount_eur=60,
        # calendar_recurring_event_id and calendar_event_ids both left unset
    )
    handler = WiseTransferHandler(profile)

    events = [_real_therapy_in_person_event(), _real_walk_to_therapy_event()]
    matched = handler.matches(events)

    # Without a configured id, both events legitimately match on keyword —
    # this is the documented, unresolved trade-off for an unconfigured
    # profile (see README follow-up note), not a regression.
    assert len(matched) == 2
    summaries = {m["summary"] for m in matched}
    assert summaries == {"Therapy in-person", "Walk to therapy"}


def test_prefix_match_mode_excludes_incidental_mention():
    """
    keyword_match_mode='prefix' requires the title to START WITH the keyword,
    so the payable 'Therapy in-person' matches while the incidental
    'Walk to therapy' does not — the therapy fix with no stable calendar id.
    """
    profile = PaymentProfile(
        prefix="THERAPY",
        label="Therapy",
        calendar_keywords=["therapy", "terapia"],
        payment_type="wise",
        amount_eur=60,
        keyword_match_mode="prefix",
    )
    handler = WiseTransferHandler(profile)

    events = [_real_therapy_in_person_event(), _real_walk_to_therapy_event()]
    matched = handler.matches(events)

    assert len(matched) == 1
    assert matched[0]["summary"] == "Therapy in-person"


def test_keyword_fallback_ignores_non_matching_title():
    profile = PaymentProfile(
        prefix="YOGA",
        label="Yoga",
        calendar_keywords=["yoga", "manel"],
        payment_type="btc",
        amount_eur=60,
        btc_address="bc1qexampleexampleexampleexampleexample",
    )
    handler = BtcTransferHandler(profile)

    events = [
        {
            "id": "dentist-1",
            "summary": "Dentist appointment",
            "start": {"dateTime": "2026-07-02T10:00:00+02:00", "timeZone": "Europe/Madrid"},
            "end": {"dateTime": "2026-07-02T10:30:00+02:00", "timeZone": "Europe/Madrid"},
        }
    ]
    assert handler.matches(events) == []


# ---------------------------------------------------------------------------
# Fix A: Wise recipient resolution from Neotoma (parquet/DATA_DIR absent)
# ---------------------------------------------------------------------------


# Fake, format-valid fixtures only — NEVER the operator's real payee PII in a
# public repo (gitleaks pii-iban/pii-phone rules; CLAUDE.md PII scan). The real
# recipient is resolved at runtime from the Neotoma contact, never from source.
THERAPY_CONTACT_SNAPSHOT = {
    "name": "Test Payee",
    "iban": "ES0000000000000000000000",  # synthetic placeholder (all-zero)
    "wise_recipient_id": "9999999999",
    "phone": "+34600000000",
}


def _therapy_profile(**overrides) -> PaymentProfile:
    base = dict(
        prefix="THERAPY",
        label="Therapy",
        calendar_keywords=["therapy", "terapia"],
        payment_type="wise",
        amount_eur=60,
        contact_id="ent_4158e205c379f73179089135",
        wise_reference="Pago terapia",
    )
    base.update(overrides)
    return PaymentProfile(**base)


def test_wise_execute_resolves_iban_from_neotoma_when_data_dir_absent(monkeypatch):
    """
    The real bug: with DATA_DIR unset, the old _load_contact() hard-relied
    on contacts.parquet and execute() returned 'No IBAN found in contact'
    even though the IBAN lives in the payment_profile/Neotoma contact.
    After the fix, Neotoma resolution must succeed on its own.
    """
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("WISE_API_TOKEN", "test-token")

    profile = _therapy_profile()
    handler = WiseTransferHandler(profile)

    fake_wise_result = {
        "status": "sent",
        "transfer_id": 999,
        "quote_uuid": "q1",
        "account_id": int(THERAPY_CONTACT_SNAPSHOT["wise_recipient_id"]),
        "amount_eur": 60,
        "iban": THERAPY_CONTACT_SNAPSHOT["iban"],
        "recipient_name": THERAPY_CONTACT_SNAPSHOT["name"],
        "reference": "Pago terapia",
        "wise_status": "COMPLETED",
    }

    with mock.patch(
        "handlers.wise_transfer._load_contact_from_neotoma",
        return_value=dict(THERAPY_CONTACT_SNAPSHOT),
    ) as m_neotoma, mock.patch(
        "handlers.wise_transfer._load_contact_from_parquet"
    ) as m_parquet, mock.patch(
        "handlers.wise_transfer._execute_wise_transfer", return_value=fake_wise_result
    ) as m_transfer, mock.patch(
        "handlers.wise_transfer._update_task"
    ):
        result = handler.execute({"event": _real_therapy_in_person_event(), "summary": "Therapy in-person"})

    m_neotoma.assert_called_once_with(profile)
    m_parquet.assert_not_called()  # Neotoma had a usable IBAN — no parquet fallback needed
    assert result["status"] == "sent"
    assert result.get("error") != "No IBAN found in contact"
    m_transfer.assert_called_once()
    called_iban = m_transfer.call_args[0][1]
    assert called_iban == THERAPY_CONTACT_SNAPSHOT["iban"]


def test_wise_execute_falls_back_to_parquet_when_neotoma_has_no_iban(monkeypatch):
    """contacts.parquet remains a legacy fallback when Neotoma has nothing."""
    monkeypatch.setenv("DATA_DIR", "/fake/data/dir")
    monkeypatch.setenv("WISE_API_TOKEN", "test-token")

    profile = _therapy_profile()
    handler = WiseTransferHandler(profile)

    parquet_contact = {
        "name": THERAPY_CONTACT_SNAPSHOT["name"],
        "iban": THERAPY_CONTACT_SNAPSHOT["iban"],
    }

    with mock.patch(
        "handlers.wise_transfer._load_contact_from_neotoma", return_value=None
    ) as m_neotoma, mock.patch(
        "handlers.wise_transfer._load_contact_from_parquet", return_value=parquet_contact
    ) as m_parquet, mock.patch(
        "handlers.wise_transfer._execute_wise_transfer",
        return_value={"status": "sent", "transfer_id": 1, "iban": parquet_contact["iban"],
                       "recipient_name": parquet_contact["name"], "reference": "Pago terapia",
                       "amount_eur": 60, "quote_uuid": "q", "account_id": 1, "wise_status": "COMPLETED"},
    ), mock.patch("handlers.wise_transfer._update_task"):
        result = handler.execute({"event": _real_therapy_in_person_event(), "summary": "Therapy in-person"})

    m_neotoma.assert_called_once()
    m_parquet.assert_called_once()
    assert result["status"] == "sent"


def test_wise_execute_fails_safe_when_no_source_has_iban(monkeypatch):
    """Neither Neotoma nor parquet has an IBAN — must NOT execute a payment."""
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("WISE_API_TOKEN", "test-token")

    profile = _therapy_profile(contact_id="ent_doesnotexist")
    handler = WiseTransferHandler(profile)

    with mock.patch(
        "handlers.wise_transfer._load_contact_from_neotoma", return_value=None
    ), mock.patch(
        "handlers.wise_transfer._load_contact_from_parquet", return_value=None
    ), mock.patch(
        "handlers.wise_transfer._execute_wise_transfer"
    ) as m_transfer:
        result = handler.execute({"event": _real_therapy_in_person_event(), "summary": "Therapy in-person"})

    assert result["status"] == "manual_required"
    m_transfer.assert_not_called()  # fail-safe: no IBAN resolved -> never attempt a send


def test_load_contact_from_neotoma_reads_iban_and_wise_recipient_id():
    """Direct unit test of the Neotoma HTTP resolution helper (network mocked)."""
    from handlers import wise_transfer

    profile = _therapy_profile()

    fake_response = json.dumps({"snapshot": dict(THERAPY_CONTACT_SNAPSHOT)}).encode()

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return fake_response

    with mock.patch("urllib.request.urlopen", return_value=_FakeResp()):
        contact = wise_transfer._load_contact_from_neotoma(profile)

    assert contact is not None
    assert contact["iban"] == THERAPY_CONTACT_SNAPSHOT["iban"]
    assert contact["wise_recipient_id"] == THERAPY_CONTACT_SNAPSHOT["wise_recipient_id"]
    assert contact["name"] == THERAPY_CONTACT_SNAPSHOT["name"]


def test_load_contact_from_neotoma_returns_none_for_non_entity_contact_id():
    """A legacy parquet-style contact_id (not 'ent_...') skips Neotoma entirely."""
    from handlers import wise_transfer

    profile = _therapy_profile(contact_id="578f6ce3-f9a4-4f")
    assert wise_transfer._load_contact_from_neotoma(profile) is None
