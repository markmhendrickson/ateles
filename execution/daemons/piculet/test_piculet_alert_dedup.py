"""Tests for piculet's operator-alert rate limiting.

Regression cover for the error-email loop: `log_error` deduped only the
Telegram alert and passed the lib/notify (email) alert straight through, so a
persistent condition — e.g. "Neotoma unavailable — skipping poll: Neotoma
request timed out (15.0s)" — emailed the operator once per poll interval,
indefinitely. Both channels must share one rate-limit gate.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import watch  # noqa: E402


def _reset(monkeypatch, sent_tg, sent_notify):
    monkeypatch.setattr(watch, "_telegram_alert_state", {})
    monkeypatch.setattr(watch, "_telegram", lambda m: sent_tg.append(m))
    monkeypatch.setattr(
        watch, "_notify_lib", lambda m, priority="info": sent_notify.append((m, priority))
    )


def test_repeated_error_emails_operator_once(monkeypatch):
    tg, notify = [], []
    _reset(monkeypatch, tg, notify)

    # Same failing poll, ten cycles in a row.
    for _ in range(10):
        watch.log_error("Neotoma unavailable — skipping poll: request timed out (15.0s)")

    assert len(notify) == 1, f"expected 1 operator email, got {len(notify)}"
    assert notify[0][1] == "blocker"
    assert len(tg) == 1


def test_distinct_errors_each_notify(monkeypatch):
    # Dedup is per-message: a genuinely different error must still reach the
    # operator rather than being suppressed by an unrelated active alert.
    tg, notify = [], []
    _reset(monkeypatch, tg, notify)

    watch.log_error("Neotoma unavailable")
    watch.log_error("disk full")

    assert len(notify) == 2


def test_repeated_warning_notifies_once(monkeypatch):
    tg, notify = [], []
    _reset(monkeypatch, tg, notify)

    for _ in range(5):
        watch.log_warning("slow response")

    assert len(notify) == 1
    assert notify[0][1] == "info"


def test_alert_refires_after_clear(monkeypatch):
    # On resolve the daemon calls _telegram_clear; the next occurrence is a
    # genuinely new incident and must alert again.
    tg, notify = [], []
    _reset(monkeypatch, tg, notify)

    watch.log_error("Neotoma unavailable")
    assert len(notify) == 1

    watch._telegram_clear("🔴 [piculet] ERROR: Neotoma unavailable")
    watch._telegram_clear("notify:error:Neotoma unavailable")

    watch.log_error("Neotoma unavailable")
    assert len(notify) == 2


def test_alert_is_due_gate_first_true_then_suppressed(monkeypatch):
    monkeypatch.setattr(watch, "_telegram_alert_state", {})
    assert watch._alert_is_due("k") is True
    assert watch._alert_is_due("k") is False
    assert watch._alert_is_due("k") is False
