"""Tests for piculet's operator-alert dedup gate (ateles#225).

Covers the shared _alert_is_due rate-limit gate and its two consumers,
log_error/log_warning, verifying that the lib/notify (email) channel is
suppressed exactly like Telegram instead of firing on every invocation —
the regression that produced one operator email per failed poll.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import watch  # noqa: E402


def setup_function(_fn=None):
    # Each test starts with a clean dedup table so runs don't interfere.
    watch._telegram_alert_state.clear()


# ── _alert_is_due — the shared gate ─────────────────────────────────────────


def test_alert_is_due_first_sighting_true():
    assert watch._alert_is_due("k") is True


def test_alert_is_due_suppressed_within_interval():
    assert watch._alert_is_due("k") is True
    # Immediately re-checking the same key, well within the repeat interval.
    assert watch._alert_is_due("k") is False
    assert watch._alert_is_due("k") is False


def test_alert_is_due_reminder_after_interval_elapsed(monkeypatch):
    now = 1_000_000.0
    watch._telegram_alert_state["k"] = (now, 1)
    # Interval has fully elapsed since first_sent for count=1.
    later = now + watch._TELEGRAM_REPEAT_INTERVAL
    monkeypatch.setattr(watch.time, "monotonic", lambda: later)

    assert watch._alert_is_due("k") is True
    first_sent, count = watch._telegram_alert_state["k"]
    assert first_sent == now
    assert count == 2


def test_alert_is_due_clear_resets_fresh():
    assert watch._alert_is_due("k") is True
    assert watch._alert_is_due("k") is False
    watch._telegram_clear("k")
    assert watch._alert_is_due("k") is True


def test_alert_is_due_per_key_independence():
    assert watch._alert_is_due("a") is True
    # A different key is unaffected by "a" already having fired.
    assert watch._alert_is_due("b") is True
    assert watch._alert_is_due("a") is False
    assert watch._alert_is_due("b") is False


def test_alert_is_due_single_evaluation_drives_both_channels():
    # One call decides the outcome; a caller must not re-evaluate per channel.
    due_first = watch._alert_is_due("shared-key")
    due_second = watch._alert_is_due("shared-key")
    assert due_first is True
    assert due_second is False


# ── log_error / log_warning — effect test: _notify_lib suppression ─────────


def test_log_error_calls_notify_lib_once_across_repeated_polls(monkeypatch):
    telegram_calls = []
    notify_calls = []
    monkeypatch.setattr(watch, "_telegram", lambda msg: telegram_calls.append(msg))
    monkeypatch.setattr(
        watch, "_notify_lib", lambda msg, priority="info": notify_calls.append((msg, priority))
    )

    for _ in range(5):
        watch.log_error("Neotoma unavailable — skipping poll: timed out")

    assert len(notify_calls) == 1
    assert notify_calls[0] == (
        "piculet error: Neotoma unavailable — skipping poll: timed out",
        "blocker",
    )
    assert len(telegram_calls) == 1


def test_log_warning_calls_notify_lib_once_across_repeated_polls(monkeypatch):
    telegram_calls = []
    notify_calls = []
    monkeypatch.setattr(watch, "_telegram", lambda msg: telegram_calls.append(msg))
    monkeypatch.setattr(
        watch, "_notify_lib", lambda msg, priority="info": notify_calls.append((msg, priority))
    )

    for _ in range(5):
        watch.log_warning("Entity extraction exited with code 1")

    assert len(notify_calls) == 1
    assert notify_calls[0] == (
        "piculet warning: Entity extraction exited with code 1",
        "info",
    )
    assert len(telegram_calls) == 1


def test_log_error_recurrence_after_clear_notifies_fresh(monkeypatch):
    notify_calls = []
    monkeypatch.setattr(watch, "_telegram", lambda msg: None)
    monkeypatch.setattr(
        watch, "_notify_lib", lambda msg, priority="info": notify_calls.append(msg)
    )

    watch.log_error("Neotoma unavailable — skipping poll: timed out")
    watch.log_error("Neotoma unavailable — skipping poll: timed out")
    assert len(notify_calls) == 1

    # Condition resolves — same clear path the main loop uses.
    watch._telegram_clear("Neotoma unavailable — skipping poll: timed out")

    watch.log_error("Neotoma unavailable — skipping poll: timed out")
    assert len(notify_calls) == 2


def test_log_error_different_message_notifies_immediately(monkeypatch):
    notify_calls = []
    monkeypatch.setattr(watch, "_telegram", lambda msg: None)
    monkeypatch.setattr(
        watch, "_notify_lib", lambda msg, priority="info": notify_calls.append(msg)
    )

    watch.log_error("Neotoma unavailable — skipping poll: timed out")
    watch.log_error("Entity extraction timed out after 1 hour.")

    assert len(notify_calls) == 2


def test_log_error_telegram_and_notify_agree_on_suppression(monkeypatch):
    # Regression guard for the bug: both channels must be suppressed together,
    # not independently gated on different keys.
    telegram_calls = []
    notify_calls = []
    monkeypatch.setattr(watch, "_telegram", lambda msg: telegram_calls.append(msg))
    monkeypatch.setattr(
        watch, "_notify_lib", lambda msg, priority="info": notify_calls.append(msg)
    )

    watch.log_error("same condition")
    watch.log_error("same condition")
    watch.log_error("same condition")

    assert len(telegram_calls) == len(notify_calls) == 1


# ── Non-regression: Telegram text/timing and _notify_lib contract untouched ─


def test_telegram_deduped_first_send_text_byte_for_byte(monkeypatch):
    telegram_calls = []
    monkeypatch.setattr(watch, "_telegram", lambda msg: telegram_calls.append(msg))

    watch._telegram_deduped("plain message")

    assert telegram_calls == ["plain message"]


def test_telegram_deduped_reminder_text_byte_for_byte(monkeypatch):
    telegram_calls = []
    monkeypatch.setattr(watch, "_telegram", lambda msg: telegram_calls.append(msg))

    now = 2_000_000.0
    watch._telegram_alert_state["plain message"] = (now, 1)
    later = now + watch._TELEGRAM_REPEAT_INTERVAL
    monkeypatch.setattr(watch.time, "monotonic", lambda: later)

    watch._telegram_deduped("plain message")

    assert len(telegram_calls) == 1
    assert telegram_calls[0].startswith("plain message (still ongoing, ")
    assert telegram_calls[0].endswith("m)")


def test_log_error_notify_lib_priority_and_args_unchanged(monkeypatch):
    monkeypatch.setattr(watch, "_telegram", lambda msg: None)
    captured = {}

    def fake_notify_lib(message, priority="info"):
        captured["message"] = message
        captured["priority"] = priority

    monkeypatch.setattr(watch, "_notify_lib", fake_notify_lib)

    watch.log_error("boom")

    assert captured == {"message": "piculet error: boom", "priority": "blocker"}


def test_log_warning_notify_lib_priority_and_args_unchanged(monkeypatch):
    monkeypatch.setattr(watch, "_telegram", lambda msg: None)
    captured = {}

    def fake_notify_lib(message, priority="info"):
        captured["message"] = message
        captured["priority"] = priority

    monkeypatch.setattr(watch, "_notify_lib", fake_notify_lib)

    watch.log_warning("hmm")

    assert captured == {"message": "piculet warning: hmm", "priority": "info"}
