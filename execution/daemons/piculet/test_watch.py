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


# ── Watermark / meeting-recording partitioning (ateles#421) ─────────────────
#
# The daemon logged "Found 88 new meeting recording(s)" once a minute forever:
# recordings with no transcript were never added to the seen-set, so they were
# re-found and re-announced on every poll. partition_meeting_recordings draws
# the line the old loop was missing.


def _touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_partition_splits_ready_from_pending(tmp_path):
    ready = _touch(tmp_path / "a.wav")
    _touch(tmp_path / "a.txt")  # transcript present → ready
    pending = _touch(tmp_path / "b.wav")  # no transcript → pending

    got_ready, got_pending = watch.partition_meeting_recordings(
        [ready, pending], set()
    )
    assert [p.name for p in got_ready] == ["a.wav"]
    assert [p.name for p in got_pending] == ["b.wav"]


def test_partition_reports_nothing_when_all_pending(tmp_path):
    """The 88-file case: no transcripts means nothing is 'new' to announce."""
    recs = [_touch(tmp_path / f"r{i}.wav") for i in range(88)]
    ready, pending = watch.partition_meeting_recordings(recs, set())
    assert ready == []
    assert len(pending) == 88


def test_pending_file_becomes_ready_once_transcript_lands(tmp_path):
    """A pending recording is re-examined, not abandoned."""
    rec = _touch(tmp_path / "late.wav")
    ready, pending = watch.partition_meeting_recordings([rec], set())
    assert ready == [] and pending == [rec]

    _touch(tmp_path / "late.txt")
    ready, pending = watch.partition_meeting_recordings([rec], set())
    assert ready == [rec] and pending == []


def test_find_new_meeting_recordings_excludes_seen(tmp_path, monkeypatch):
    _touch(tmp_path / "seen.wav")
    _touch(tmp_path / "fresh.wav")
    monkeypatch.setattr(watch, "_audio_imports_dir", lambda: tmp_path)
    found = watch.find_new_meeting_recordings({"seen.wav"})
    assert [p.name for p in found] == ["fresh.wav"]


# ── Clarity gate: held-memo bookkeeping ─────────────────────────────────────


def test_held_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "HELD_STATE_FILE", tmp_path / "held.json")
    assert watch.load_held() == {}
    watch.save_held({"memo.m4a": {"reason": "check 1", "checks": [1]}})
    assert watch.load_held()["memo.m4a"]["checks"] == [1]


def test_load_held_tolerates_corrupt_state(tmp_path, monkeypatch):
    bad = tmp_path / "held.json"
    bad.write_text("{not json")
    monkeypatch.setattr(watch, "HELD_STATE_FILE", bad)
    assert watch.load_held() == {}


def test_hold_notification_names_check_and_shows_flagged_span():
    from lib.transcript_clarity import assess_transcript

    text = "A real sentence about the pipeline. " + ("Maybe there's a way to avoid this. " * 20)
    report = assess_transcript(text, duration_seconds=500.0)
    msg = watch.format_hold_notification("memo.m4a", report, None)

    assert "memo.m4a" in msg
    assert "NOT processed" in msg
    assert "Check 1" in msg
    assert "avoid this" in msg
    assert watch.RELEASE_COMMAND in msg
    # The operator gets the flagged span, not the whole transcript: the quoted
    # excerpt is bounded regardless of how long the memo was.
    assert all(len(f.excerpt) <= 200 for f in report.findings)
    assert text.strip() not in msg


def test_hold_notification_includes_transcript_path(tmp_path):
    from lib.transcript_clarity import assess_transcript

    report = assess_transcript("too short.", duration_seconds=60.0)
    p = tmp_path / "t.txt"
    msg = watch.format_hold_notification("memo.m4a", report, p)
    assert str(p) in msg


def test_release_unknown_memo_is_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(watch, "HELD_STATE_FILE", tmp_path / "held.json")
    watch.save_held({"real.m4a": {"reason": "check 2"}})
    assert watch.release_held("nonexistent.m4a") == 1
    assert "real.m4a" in capsys.readouterr().out


def test_discard_removes_from_held(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "HELD_STATE_FILE", tmp_path / "held.json")
    monkeypatch.setattr(watch, "notify", lambda *a, **k: None)
    watch.save_held({"memo.m4a": {"reason": "check 2"}})
    assert watch.release_held("memo.m4a", discard=True) == 0
    assert watch.load_held() == {}


def test_release_keeps_memo_held_when_extraction_fails(tmp_path, monkeypatch):
    """A failed release must not lose the memo."""
    monkeypatch.setattr(watch, "HELD_STATE_FILE", tmp_path / "held.json")
    monkeypatch.setattr(watch, "notify", lambda *a, **k: None)
    monkeypatch.setattr(watch, "log_error", lambda *a, **k: None)

    def boom(_files):
        raise RuntimeError("claude CLI missing")

    monkeypatch.setattr(watch, "run_entity_extraction", boom)
    watch.save_held({"memo.m4a": {"reason": "check 1"}})
    assert watch.release_held("memo.m4a") == 1
    assert "memo.m4a" in watch.load_held()


def test_release_substring_match_works(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "HELD_STATE_FILE", tmp_path / "held.json")
    monkeypatch.setattr(watch, "notify", lambda *a, **k: None)
    monkeypatch.setattr(watch, "run_entity_extraction", lambda _f: "none")
    watch.save_held({"20260905 093900-7C37CAC3.m4a": {"reason": "check 2"}})
    assert watch.release_held("7C37CAC3") == 0
    assert watch.load_held() == {}


def test_release_ambiguous_substring_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(watch, "HELD_STATE_FILE", tmp_path / "held.json")
    watch.save_held({"memo_a.m4a": {"reason": "x"}, "memo_b.m4a": {"reason": "y"}})
    assert watch.release_held("memo") == 1
    assert "be more specific" in capsys.readouterr().out
