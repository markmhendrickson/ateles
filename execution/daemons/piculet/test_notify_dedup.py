"""
test_notify_dedup.py — persistent-error alerts are deduped across BOTH channels.

Regression guard for the flood bug: log_error/log_warning previously deduped only
the Telegram path while emailing (lib/notify) on every call, so a per-loop
persistent error emailed every ~60s. Both channels must now share one gate.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import watch  # noqa: E402


def _reset():
    watch._telegram_alert_state.clear()


def _patch(monkeypatch):
    calls = {"tg": 0, "lib": 0}
    monkeypatch.setattr(watch, "_telegram", lambda m: calls.__setitem__("tg", calls["tg"] + 1))
    monkeypatch.setattr(watch, "_notify_lib",
                        lambda m, priority="info": calls.__setitem__("lib", calls["lib"] + 1))
    return calls


def test_repeated_error_emails_once(monkeypatch):
    _reset()
    calls = _patch(monkeypatch)
    for _ in range(10):
        watch.log_error("Errno 11 Resource deadlock avoided")
    assert calls["tg"] == 1, "Telegram should fire once for a persistent error"
    assert calls["lib"] == 1, "Email (lib/notify) should fire once — not per loop"


def test_distinct_errors_each_notify(monkeypatch):
    _reset()
    calls = _patch(monkeypatch)
    watch.log_error("error A")
    watch.log_error("error B")
    assert calls["tg"] == 2 and calls["lib"] == 2


def test_warning_and_error_are_separate_keys(monkeypatch):
    _reset()
    calls = _patch(monkeypatch)
    watch.log_error("same text")
    watch.log_warning("same text")
    # Different severity prefix → distinct keys → each notifies once.
    assert calls["lib"] == 2


def test_clear_lets_alert_fire_again(monkeypatch):
    _reset()
    calls = _patch(monkeypatch)
    watch.log_error("blip")
    watch._telegram_clear("error:blip")
    watch.log_error("blip")
    assert calls["lib"] == 2, "after clear, a recurrence should notify again"
