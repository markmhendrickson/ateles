"""
EFFECT-level characterization of Monedula's Telegram consent gate (#554).

Companion to `test_gate_channel_failure.py`, which stops at `_parse_reply`.
These two tests cross the layers that file cannot reach: the poll layer that
turns a channel error into `None`, and `main()`, where that `None` decides
whether money moves. They are characterization pins on CURRENT behaviour —
they assert nothing that does not already hold, and no production code changes
with them.

Why they exist: the QA lens mutated `monedula.py` to fail OPEN (a `None` reply
approving every triggered handler) and to let HTTP 409 escape the poll layer,
and the whole parser-level suite still passed. The only thing that went red was
an incidental `AttributeError` from a handler double with no `execute`. So the
mutation was caught by accident, not by an assertion. `test_dead_channel_
executes_no_payment` below closes that hole with a recording `execute()`.

NOT the #554 definition-of-done. #554's QA plan reserves this file for the full
effect suite and signs off `gate_status.qa` only when it also covers the
structured poll outcome (`TelegramPollResult`), the escalation POST body,
`_notify(priority="blocker")`, differentiated exit codes, dead-gate detection,
and `payment_approved` non-authority. None of that exists yet — it is owed by
the implementation PR, along with acceptance criteria 6 (effect-verified fix)
and 7 (cross-surface parity).

Run with: pytest execution/daemons/monedula/test_telegram_gate.py -v
"""

from __future__ import annotations

import sys
import types
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import monedula  # noqa: E402


# ── 1. Poll layer: a 409 must not escape as an exception ─────────────────────


class _FakeClock:
    """A monotonic clock that only advances when the code under test sleeps.

    The poll loop deadlines on `time.monotonic()` and backs off with
    `time.sleep(2)`. Left real, the failure path below would burn the full
    `timeout_sec` in wall time.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_http_409_returns_none_from_poll_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 409 from `getUpdates` retries to the deadline and returns None.

    `HTTPError` subclasses `URLError`, so `monedula.py:400` already catches it
    — this passes on current code. The point is that nothing pinned it, which
    is how a mutation narrowing that `except` clause went undetected.

    Telegram credentials are module-level constants bound at import
    (`monedula.py:365-382`), so `monkeypatch.setenv` would be a no-op here;
    they have to be set on the module.
    """
    monkeypatch.setattr(monedula, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(monedula, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(monedula, "TELEGRAM_ALLOWED_USER_ID", "67890")

    clock = _FakeClock()
    monkeypatch.setattr(monedula.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(monedula.time, "sleep", clock.sleep)

    attempts: list[str] = []

    def _conflict(url: str, timeout: float | None = None):
        attempts.append(url)
        raise urllib.error.HTTPError(url, 409, "Conflict", hdrs=None, fp=None)

    monkeypatch.setattr(monedula.urllib.request, "urlopen", _conflict)

    assert monedula.telegram_long_poll_once(timeout_sec=10) is None
    assert len(attempts) > 1, "a 409 must be retried, not given up on immediately"
    assert clock.now >= 10, "the loop must terminate on its deadline, not early"


# ── 2. main(): a dead channel must move no money ─────────────────────────────


class _RecordingHandler:
    """Payment handler double that records being asked to pay.

    `execute()` is real and recording on purpose. A double that simply lacks
    the attribute turns a fail-open regression into an `AttributeError` — the
    suite goes red for the wrong reason, and the assertion that should have
    caught it is never written.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.execute_calls: list = []
        self.profile = types.SimpleNamespace(
            one_off=False,
            due_date="",
            calendar_keywords=[name],
            label=name,
        )

    def matches(self, events):
        return [{"trigger": "calendar", "handler": self.name}]

    def preview(self, match):
        return f"preview:{self.name}"

    def execute(self, match):
        self.execute_calls.append(match)
        return {"status": "unapproved payment executed"}


@pytest.fixture
def sent_messages(tmp_path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Neutralise every outbound effect of `main()` and capture Telegram sends."""
    monkeypatch.setattr(monedula, "STATE_FILE", tmp_path / ".monedula_last_run")
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        monedula, "fetch_due_payment_tasks", lambda *a, **k: [], raising=False
    )
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [])

    sent: list[str] = []
    monkeypatch.setattr(
        monedula, "telegram_send", lambda msg: sent.append(msg), raising=False
    )
    return sent


def test_dead_channel_executes_no_payment(
    monkeypatch: pytest.MonkeyPatch, sent_messages: list[str]
) -> None:
    """The guard that actually protects the money.

    One triggered handler, a poll that returns `None` (the 409'd channel).
    `main()` must execute nothing and must tell the operator it skipped —
    a silent return would be just as wrong as a payment.
    """
    handler = _RecordingHandler("therapy")
    fake_handlers = types.ModuleType("handlers")
    fake_handlers.load_handlers = lambda: [handler]
    monkeypatch.setitem(sys.modules, "handlers", fake_handlers)

    monkeypatch.setattr(
        monedula, "telegram_long_poll_once", lambda *a, **k: None, raising=False
    )

    monedula.main()

    assert handler.execute_calls == [], (
        "a dead consent channel must not execute a payment"
    )

    yesterday_str = monedula._yesterday().isoformat()
    assert f"⏭️ Monedula: skipped all payments for {yesterday_str}." in sent_messages, (
        "the operator must see the skip; a silent return hides a broken gate"
    )
