"""Session-payment loop: PR A acceptance tests (defects 1, 3 calendars, 4, 5).

Everything external is faked: gws, Neotoma profile loading, the mailbox.
Synthetic fixtures only.

Defect 1: a profile-load failure must not claim the day or clear consent.
Defect 4: every calendar is read; one failed calendar is not an empty day.
Defect 5: each tick evaluates sessions that ended in a rolling window, and a
session is raised once, deduped per event id in the journal.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import consent_email  # noqa: E402
import monedula  # noqa: E402
import payment_journal  # noqa: E402
from lib.approval.email_channel import ReadRepliesOutcome  # noqa: E402
from lib.approval.tokens import subject_marker  # noqa: E402
from strandings import REASON_FETCH_FAILED, Stranding  # noqa: E402

UTC = timezone.utc
# Madrid is UTC+1 in November.
SESSION_END = datetime(2026, 11, 11, 21, 0, tzinfo=UTC)  # 22:00 Madrid


def _event(eid: str, summary: str = "yoga", end: datetime = SESSION_END) -> dict:
    start = end - timedelta(hours=1)
    return {
        "id": eid,
        "summary": summary,
        "start": {"dateTime": start.astimezone(timezone(timedelta(hours=1))).isoformat()},
        "end": {"dateTime": end.astimezone(timezone(timedelta(hours=1))).isoformat()},
    }


class _Handler:
    name = "yoga"

    def __init__(self) -> None:
        self.execute_calls: list = []
        self.profile = types.SimpleNamespace(
            label="Yoga Example",
            amount_eur=10,
            calendar_keywords=["yoga"],
            one_off=False,
            due_date="",
        )

    def matches(self, events):
        return [
            {"event": e, "summary": e["summary"]}
            for e in events
            if "yoga" in (e.get("summary") or "").lower()
        ]

    def preview(self, match):
        return "preview"

    def execute(self, match):
        self.execute_calls.append(match)
        return {"status": "sent", "handler": self.name}


class _World:
    """Fake gws + clock + profile loader + mailbox, shared across ticks."""

    def __init__(self, monkeypatch, tmp_path, handler):
        self.handler = handler
        self.now = SESSION_END + timedelta(minutes=10)
        self.calendars: dict[str, list[dict]] = {"primary": []}
        self.failing_calendars: set[str] = set()
        self.profile_fetch_fails = False
        self.sends: list = []
        self.reply = False  # operator replied ATTENDED
        self.gws_calls: list[list[str]] = []

        monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", "email")
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
        monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.net")
        cfg = tmp_path / "swarm-gws"
        cfg.mkdir()
        monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(cfg))
        monkeypatch.delenv("MONEDULA_SESSION_WINDOW_HOURS", raising=False)

        def load_handlers(strandings=None):
            if self.profile_fetch_fails:
                if strandings is not None:
                    strandings.append(
                        Stranding(
                            entity_id="",
                            label="(all profiles)",
                            reason=REASON_FETCH_FAILED,
                            detail="timed out",
                        )
                    )
                return []
            return [self.handler]

        fake = types.ModuleType("handlers")
        fake.load_handlers = load_handlers
        monkeypatch.setitem(sys.modules, "handlers", fake)
        monkeypatch.setattr(monedula, "STATE_FILE", tmp_path / "last_run")
        monkeypatch.setattr(monedula, "GATE_HEALTH_FILE", tmp_path / "gate_health")
        monkeypatch.setattr(consent_email, "STATE_FILE", tmp_path / "consent.json")
        monkeypatch.setattr(monedula, "fetch_due_payment_tasks", lambda *a, **k: [])
        monkeypatch.setattr(monedula, "telegram_send", lambda *a, **k: None)
        monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None)
        monkeypatch.setattr(monedula, "escalate_strandings", lambda s: s)
        monkeypatch.setattr(monedula, "_emit_consent_escalation", lambda *a, **k: True)
        monkeypatch.setattr(monedula, "_clear_notify_dedupe", lambda *a, **k: None)
        monkeypatch.setattr(monedula, "_now", lambda: self.now)
        monkeypatch.setattr("shutil.which", lambda name: "/bin/gws")
        monkeypatch.setattr(monedula.subprocess, "run", self._gws)

        def send(subject, body, to=None):
            self.sends.append(subject)
            return True

        def read(tokens, on_sender_rejected=None, **k):
            if not self.reply:
                return ReadRepliesOutcome(kind="ok", texts=[])
            return ReadRepliesOutcome(
                kind="ok",
                texts=[f"RE: {subject_marker(t)}\nATTENDED" for t in tokens],
            )

        monkeypatch.setattr(consent_email, "send_request", send)
        monkeypatch.setattr(consent_email, "read_replies_with_status", read)
        self.tmp_path = tmp_path

    def _gws(self, cmd, **kw):
        self.gws_calls.append(list(cmd))
        params = json.loads(cmd[cmd.index("--params") + 1])
        if cmd[2] == "calendarList":
            body = {"items": [{"id": cid} for cid in self.calendars]}
        else:
            cid = params["calendarId"]
            if cid in self.failing_calendars:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
            body = {"items": self.calendars[cid]}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(body), stderr="")

    def tick(self, minutes: float = 0) -> bool:
        self.now += timedelta(minutes=minutes)
        return monedula.main()

    @property
    def journal(self) -> dict:
        return payment_journal.load(consent_email.journal_path())


@pytest.fixture
def world(monkeypatch, tmp_path):
    return _World(monkeypatch, tmp_path, _Handler())


# ── Defect 1: profile timeout must not drop pending consent ─────────────────


def test_profile_timeout_preserves_pending_consent_and_does_not_claim_day(world):
    world.calendars["primary"] = [_event("evt-1")]
    world.tick()  # consent raised
    assert len(world.sends) == 1
    assert (world.tmp_path / "consent.json").exists()

    # 08:15 next day: the payment_profile fetch times out, zero handlers load.
    world.profile_fetch_fails = True
    world.tick(minutes=600)

    assert (world.tmp_path / "consent.json").exists(), "consent state was cleared"
    assert not (world.tmp_path / "last_run").exists(), "a degraded tick claimed the day"
    pending = payment_journal.pending_sessions(world.journal)
    assert list(pending) == ["yoga:evt-1"], "pending obligation lost from the journal"

    # Profiles recover: the same request is still outstanding (no re-send)
    # and the operator's approval pays exactly once.
    world.profile_fetch_fails = False
    world.reply = True
    world.tick(minutes=15)
    assert len(world.sends) == 1
    assert len(world.handler.execute_calls) == 1


def test_every_profile_stranded_does_not_clear_pending(world):
    world.calendars["primary"] = [_event("evt-1")]
    world.tick()
    assert (world.tmp_path / "consent.json").exists()
    # All profiles stranded for a non-fetch reason: strandings, no handlers.
    world.handler_loaded = False

    def stranded(strandings=None):
        strandings.append(
            Stranding(entity_id="e", label="Yoga", reason="invalid_amount", detail="x")
        )
        return []

    sys.modules["handlers"].load_handlers = stranded
    world.tick(minutes=15)
    assert (world.tmp_path / "consent.json").exists()
    assert not (world.tmp_path / "last_run").exists()
    assert "yoga:evt-1" in payment_journal.pending_sessions(world.journal)


# ── Defect 4: all calendars ─────────────────────────────────────────────────


def test_session_on_a_non_primary_calendar_is_found(world):
    world.calendars = {"primary": [], "work@example.test": [_event("evt-2")]}
    events = monedula.fetch_yesterday_events()
    assert [e["id"] for e in events] == ["evt-2"]
    listed = [c[2:4] for c in world.gws_calls]
    assert ["calendarList", "list"] in listed
    assert sum(1 for c in world.gws_calls if c[2] == "events") == 2


def test_one_failed_calendar_is_not_read_as_an_empty_day(world):
    world.calendars = {"primary": [], "work@example.test": [_event("evt-2")]}
    world.failing_calendars = {"work@example.test"}
    assert monedula.fetch_yesterday_events() is None


def test_failed_calendar_does_not_claim_the_day_or_drop_pending(world):
    world.calendars["primary"] = [_event("evt-1")]
    world.tick()
    world.calendars["other@example.test"] = []
    world.failing_calendars = {"other@example.test"}
    world.tick(minutes=15)
    assert not (world.tmp_path / "last_run").exists()
    assert "yoga:evt-1" in payment_journal.pending_sessions(world.journal)


def test_events_are_deduped_by_id_across_calendars(world):
    ev = _event("evt-3")
    world.calendars = {"primary": [ev], "shared@example.test": [dict(ev)]}
    assert len(monedula.fetch_yesterday_events()) == 1


# ── Defect 5: ended-session window, raised once ─────────────────────────────


def test_session_in_window_raises_once_and_is_not_raised_again(world):
    world.calendars["primary"] = [_event("evt-1")]
    world.tick()
    assert len(world.sends) == 1
    world.tick(minutes=15)
    world.tick(minutes=15)
    assert len(world.sends) == 1, "the same session was raised again"


def test_session_raised_once_across_midnight_and_restart(world, monkeypatch):
    world.calendars["primary"] = [_event("evt-1")]
    world.tick()
    # "Restart" is a fresh main() with only on-disk state; cross midnight.
    world.tick(minutes=180)
    assert len(world.sends) == 1


def test_session_not_yet_ended_is_not_raised(world):
    world.calendars["primary"] = [_event("evt-1", end=SESSION_END + timedelta(hours=3))]
    world.tick()
    assert world.sends == []


def test_session_that_ended_before_the_window_is_not_raised(world):
    world.calendars["primary"] = [_event("evt-old", end=SESSION_END - timedelta(days=3))]
    world.tick()
    assert world.sends == []


def test_paid_session_is_not_raised_again(world):
    world.calendars["primary"] = [_event("evt-1")]
    world.tick()
    world.reply = True
    world.tick(minutes=15)
    assert len(world.handler.execute_calls) == 1
    world.tick(minutes=15)
    world.tick(minutes=15)
    assert len(world.handler.execute_calls) == 1
    assert len(world.sends) == 1


def test_two_sessions_same_day_are_raised_separately(world):
    world.calendars["primary"] = [
        _event("evt-1"),
        _event("evt-2", end=SESSION_END + timedelta(minutes=5)),
    ]
    world.now = SESSION_END + timedelta(minutes=30)
    world.tick()
    assert set(payment_journal.pending_sessions(world.journal)) == {
        "yoga:evt-1",
        "yoga:evt-2",
    }
