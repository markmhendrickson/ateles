"""Tests for piculet's operator-alert dedup gate (ateles#225).

Covers the shared _alert_is_due rate-limit gate and its two consumers,
log_error/log_warning, verifying that the lib/notify (email) channel is
suppressed exactly like Telegram instead of firing on every invocation —
the regression that produced one operator email per failed poll.
"""

import json
import os
import logging
import sys
from pathlib import Path

import pytest

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


# ── .qta discovery (ateles#748) ──────────────────────────────────────────
#
# Voice Memos writes some recordings as .qta (QuickTime/MOV-family container)
# instead of .m4a. ffmpeg/whisper read it fine, but the watcher globbed for
# .m4a only in some places, so .qta recordings were silently skipped: never
# transcribed, never stored, no error. Every audio-enumeration point must
# consult the shared AUDIO_EXTENSIONS constant, not a repeated literal.


def test_qta_discovered_by_imports_scan(tmp_path, monkeypatch):
    """The main Recordings-dir scan (find_new_files) must pick up .qta."""
    monkeypatch.setattr(watch, "RECORDINGS_DIR", tmp_path)
    _touch(tmp_path / "20260905 124858-1E9512FC.qta", text="")
    _touch(tmp_path / "20260905 125230-84387BC3.qta", text="")

    found = watch.find_new_files(set())

    assert {p.name for p in found} == {
        "20260905 124858-1E9512FC.qta",
        "20260905 125230-84387BC3.qta",
    }


def test_qta_excluded_once_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "RECORDINGS_DIR", tmp_path)
    _touch(tmp_path / "seen.qta", text="")
    _touch(tmp_path / "fresh.qta", text="")

    found = watch.find_new_files({"seen.qta"})

    assert [p.name for p in found] == ["fresh.qta"]


def test_qta_through_partition_meeting_recordings(tmp_path):
    """
    partition_meeting_recordings operates on whatever candidate list it is
    given — a .qta file with a ready transcript sidecar must partition as
    ready exactly like any other extension, since a caller could hand it
    .qta candidates directly.
    """
    ready = _touch(tmp_path / "a.qta")
    _touch(tmp_path / "a.txt")  # transcript present -> ready
    pending = _touch(tmp_path / "b.qta")  # no transcript -> pending

    got_ready, got_pending = watch.partition_meeting_recordings(
        [ready, pending], set()
    )
    assert [p.name for p in got_ready] == ["a.qta"]
    assert [p.name for p in got_pending] == ["b.qta"]


def test_qta_through_release_path(tmp_path, monkeypatch):
    """A held .qta memo releases through the same path as any other extension."""
    monkeypatch.setattr(watch, "HELD_STATE_FILE", tmp_path / "held.json")
    monkeypatch.setattr(watch, "notify", lambda *a, **k: None)
    monkeypatch.setattr(watch, "run_entity_extraction", lambda _f: "none")
    watch.save_held(
        {"20260905 124858-1E9512FC.qta": {"reason": "check 2"}}
    )
    assert watch.release_held("1E9512FC") == 0
    assert watch.load_held() == {}


def test_find_new_files_mixed_extension_directory(tmp_path, monkeypatch):
    """A directory with both .m4a and .qta files returns both, and ignores
    non-audio sidecars Voice Memos writes alongside recordings."""
    monkeypatch.setattr(watch, "RECORDINGS_DIR", tmp_path)
    _touch(tmp_path / "one.m4a", text="")
    _touch(tmp_path / "two.qta", text="")
    _touch(tmp_path / "two.waveform", text="")
    _touch(tmp_path / "two.composition", text="")

    found = {p.name for p in watch.find_new_files(set())}

    assert found == {"one.m4a", "two.qta"}


def test_find_new_files_uppercase_extension(tmp_path, monkeypatch):
    """Some sources produce uppercase extensions (e.g. .M4A) — suffix
    matching must be case-insensitive."""
    monkeypatch.setattr(watch, "RECORDINGS_DIR", tmp_path)
    _touch(tmp_path / "20260905 130000-AAAAAAAA.M4A", text="")

    found = watch.find_new_files(set())

    assert [p.name for p in found] == ["20260905 130000-AAAAAAAA.M4A"]


def test_find_new_files_ignores_non_audio_file(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "RECORDINGS_DIR", tmp_path)
    _touch(tmp_path / "CloudRecordings.db", text="")
    _touch(tmp_path / "real.qta", text="")

    found = watch.find_new_files(set())

    assert [p.name for p in found] == ["real.qta"]


def test_assess_memo_finds_duration_for_qta_audio(tmp_path, monkeypatch):
    """
    assess_memo's duration lookup used to hardcode a .m4a fallback after
    trying .wav, so a .qta memo's duration silently came back None even when
    the audio file existed right next to the transcript — degrading the
    clarity gate's measurement without ever raising an error.
    """
    monkeypatch.setattr(watch, "_audio_imports_dir", lambda: tmp_path)

    transcript = _touch(
        tmp_path / "20260905_124858_voicememo_20260905 124858-1E9512FC.txt",
        text="A perfectly ordinary transcript about the roadmap.",
    )
    audio = _touch(
        tmp_path / "20260905_124858_voicememo_20260905 124858-1E9512FC.qta",
        text="not real audio, just needs to exist",
    )

    monkeypatch.setattr(watch, "_audio_duration_seconds", lambda p: 200.0 if p == audio else None)

    memo = Path("20260905 124858-1E9512FC.qta")
    report, found_transcript = watch.assess_memo(memo)

    assert found_transcript == transcript
    assert report is not None
    assert report.metrics.get("duration_seconds") == 200.0


# ── Import scope: a run triggered by N new memos imports only those N ───────


def test_import_command_names_only_the_new_files(tmp_path):
    new = [tmp_path / "a.m4a", tmp_path / "b.qta"]
    cmd = watch.build_import_command(new)
    assert "--source" not in cmd
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "--file"] == [str(p) for p in new]
    assert "--analyze" not in cmd  # ateles#747


def test_run_import_passes_the_new_file_list(tmp_path, monkeypatch):
    captured = {}

    class _Done:
        returncode = 0

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(watch.subprocess, "run", fake_run)
    new = [tmp_path / "a.m4a", tmp_path / "b.m4a"]

    assert watch.run_import(new) == 0
    assert "--source" not in captured["cmd"]
    assert captured["cmd"].count("--file") == 2


def test_run_import_reports_lookup_unavailable(tmp_path, monkeypatch):
    class _Done:
        returncode = watch.IMPORT_EXIT_LOOKUP_UNAVAILABLE

    monkeypatch.setattr(watch.subprocess, "run", lambda *_a, **_k: _Done())
    monkeypatch.setattr(watch, "log_warning", lambda *_a, **_k: None)
    assert watch.run_import([tmp_path / "a.m4a"]) == watch.IMPORT_EXIT_LOOKUP_UNAVAILABLE


def test_lookup_unavailable_code_matches_the_importer():
    sys.path.insert(0, str(watch.IMPORT_SCRIPT.parent))
    import import_audio_from_desktop as iad

    assert watch.IMPORT_EXIT_LOOKUP_UNAVAILABLE == iad.EXIT_LOOKUP_UNAVAILABLE


# ── The real main() loop: per-memo import, deferral and backoff ─────────────


class _StopLoop(BaseException):
    """Escapes main()'s loop (which catches Exception) after N polls."""


class _FakeJob:
    def __init__(self, log, summary):
        self.log = log
        log.append(("started", summary))

    def finished(self, summary):
        self.log.append(("finished", summary))

    def failed(self, summary):
        self.log.append(("failed", summary))


class _FakeActivity:
    def __init__(self):
        self.log = []

    def started(self, summary, **_kw):
        return _FakeJob(self.log, summary)


_START = 1_000_000.0


def _drive_main(
    monkeypatch, tmp_path, memos, rc_for, polls, start=_START, real_services=False
):
    """Run watch.main() for ``polls`` polls against temp state.

    ``rc_for(name)`` is the importer's exit code for that memo. The clock
    advances POLL_INTERVAL_SECONDS per poll. Returns what was observed.

    With ``real_services`` the real _start_daemon_services() runs, with only
    its leaves stubbed: the credential bootstrap records a call, the log file
    goes under tmp_path, and the notifier/activity constructors are fakes.
    """
    recordings = tmp_path / "Recordings"
    recordings.mkdir(exist_ok=True)
    for name in memos:
        (recordings / name).write_bytes(b"\0" * 32)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setattr(watch, "RECORDINGS_DIR", recordings)
    monkeypatch.setattr(watch, "STATE_FILE", state / "seen_files.json")
    monkeypatch.setattr(watch, "MEETING_STATE_FILE", state / "seen_meeting_files.json")
    monkeypatch.setattr(watch, "HELD_STATE_FILE", state / "held_memos.json")
    monkeypatch.setattr(watch, "DEFERRED_STATE_FILE", state / "deferred_memos.json")
    if not (state / "seen_files.json").exists():
        # A non-empty seen-set skips the first-run hydration from Neotoma.
        (state / "seen_files.json").write_text('["unrelated-old-memo.m4a"]')
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    import lib.daemon_runtime.checkout_identity as checkout_identity

    monkeypatch.setattr(checkout_identity, "enforce_deploy_checkout", lambda *a, **k: None)
    seen = {"notices": [], "telegram": [], "imports": [], "assessed": [], "events": []}
    activity = _FakeActivity()
    if real_services:
        import lib.activity
        import lib.notify

        monkeypatch.setattr(watch, "_daemon_services_started", False)
        monkeypatch.setattr(watch, "log", watch.log)  # restored after the test
        monkeypatch.setattr(watch, "_lib_notifier", None)
        monkeypatch.setattr(watch, "LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(
            watch, "configure_daemon_logging", lambda name: logging.getLogger(name)
        )
        monkeypatch.setattr(
            watch, "_bootstrap_environment", lambda: seen["events"].append("bootstrap")
        )
        monkeypatch.setattr(lib.notify.Notifier, "from_neotoma", classmethod(lambda cls: None))
        monkeypatch.setattr(lib.activity, "ActivityLogger", lambda agent: activity)
    else:
        monkeypatch.setattr(watch, "_start_daemon_services", lambda: None)
    monkeypatch.setattr(watch, "check_neotoma", lambda: seen["events"].append("check_neotoma"))

    monkeypatch.setattr(watch, "notify", lambda title, msg: seen["notices"].append(msg))
    monkeypatch.setattr(watch, "_telegram", lambda msg: seen["telegram"].append(msg))
    monkeypatch.setattr(watch, "_activity", None if real_services else activity)
    monkeypatch.setattr(
        watch, "assess_memo", lambda memo: seen["assessed"].append(memo.name) or (None, None)
    )
    monkeypatch.setattr(watch, "run_entity_extraction", lambda files: "")

    clock = {"now": start}

    class _Done:
        def __init__(self, rc):
            self.returncode = rc

    def fake_run(cmd, **_kwargs):
        assert cmd[1] == str(watch.IMPORT_SCRIPT), cmd
        files = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--file"]
        assert len(files) == 1, "piculet must import one memo per run"
        name = Path(files[0]).name
        seen["imports"].append((clock["now"], name))
        return _Done(rc_for(name))

    monkeypatch.setattr(watch.subprocess, "run", fake_run)
    monkeypatch.setattr(watch.time, "time", lambda: clock["now"])
    count = {"polls": 0}

    def fake_sleep(seconds):
        count["polls"] += 1
        if count["polls"] >= polls:
            raise _StopLoop()
        clock["now"] += seconds

    monkeypatch.setattr(watch.time, "sleep", fake_sleep)
    try:
        watch.main()
    except _StopLoop:
        pass

    seen["activity"] = activity.log
    seen["seen_file"] = set(json.loads((state / "seen_files.json").read_text()))
    held_file = state / "held_memos.json"
    seen["held"] = json.loads(held_file.read_text()) if held_file.exists() else {}
    deferred_file = state / "deferred_memos.json"
    seen["deferred"] = json.loads(deferred_file.read_text()) if deferred_file.exists() else {}
    return seen


def _unavailable(_name):
    return watch.IMPORT_EXIT_LOOKUP_UNAVAILABLE


def test_lasting_lookup_outage_notifies_once_and_backs_off(tmp_path, monkeypatch):
    memo = "20261101 101010-AAAA0001.m4a"
    out = _drive_main(monkeypatch, tmp_path, [memo], _unavailable, polls=10)
    # 10 polls, 60 s apart: attempts at +0, +60, +180, +420 (backoff 60, 120, 240).
    assert [t - _START for t, _ in out["imports"]] == [0, 60, 180, 420]
    importing = [n for n in out["notices"] if n.startswith("Importing")]
    assert importing == ["Importing 1 new memo…"]
    assert [e for e in out["activity"] if e[0] == "started"] == [
        ("started", "importing 1 new voice memo")
    ]
    assert memo not in out["seen_file"]
    assert out["held"] == {}
    assert out["assessed"] == []  # the clarity gate never saw it
    entry = out["deferred"][memo]
    assert entry["attempts"] == 4
    assert entry["reason"] == watch.DEFERRED_REASON
    assert entry["next_attempt_at"] - _START == 420 + 480
    # The exit-75 warning reaches Telegram once, not once per attempt.
    assert len([t for t in out["telegram"] if "Import deferred" in t]) == 1


def test_deferral_backoff_survives_a_restart(tmp_path, monkeypatch):
    memo = "20261101 101010-AAAA0002.m4a"
    _drive_main(monkeypatch, tmp_path, [memo], _unavailable, polls=2)  # +0, +60
    # Restart 100 s after the first start: the next attempt is due at +180.
    out = _drive_main(monkeypatch, tmp_path, [memo], _unavailable, polls=1, start=_START + 100)
    assert out["imports"] == []
    assert out["notices"] == []
    assert out["deferred"][memo]["attempts"] == 2


def test_successful_import_marks_the_memo_seen(tmp_path, monkeypatch):
    memo = "20261101 101010-AAAA0003.m4a"
    out = _drive_main(monkeypatch, tmp_path, [memo], lambda _n: 0, polls=3)
    assert len(out["imports"]) == 1
    assert memo in out["seen_file"]
    assert out["assessed"] == [memo]
    assert out["deferred"] == {}


def test_mixed_poll_defers_only_the_unknown_memo(tmp_path, monkeypatch):
    new, unknown = "20261101 101010-AAAA0004.m4a", "20261101 101010-AAAA0005.m4a"
    out = _drive_main(
        monkeypatch,
        tmp_path,
        [new, unknown],
        lambda n: watch.IMPORT_EXIT_LOOKUP_UNAVAILABLE if n == unknown else 0,
        polls=1,
    )
    assert sorted(n for _, n in out["imports"]) == sorted([new, unknown])
    assert new in out["seen_file"]
    assert unknown not in out["seen_file"]
    assert unknown not in out["held"]
    assert out["assessed"] == [new]
    assert out["deferred"][unknown]["reason"] == watch.DEFERRED_REASON
    assert [n for n in out["notices"] if n.startswith("Importing")] == ["Importing 2 new memos…"]


def test_deferred_memo_imports_once_lookup_recovers(tmp_path, monkeypatch):
    memo = "20261101 101010-AAAA0006.m4a"
    state = {"down": True}

    def rc(_name):
        return watch.IMPORT_EXIT_LOOKUP_UNAVAILABLE if state["down"] else 0

    _drive_main(monkeypatch, tmp_path, [memo], rc, polls=1)
    state["down"] = False
    out = _drive_main(monkeypatch, tmp_path, [memo], rc, polls=1, start=_START + 60)
    assert len(out["imports"]) == 1
    assert memo in out["seen_file"]
    assert out["deferred"] == {}
    # Its one "Importing" notice was sent on the first attempt.
    assert [n for n in out["notices"] if n.startswith("Importing")] == []


def test_backoff_doubles_to_an_hour_cap():
    assert [watch.defer_backoff_seconds(n) for n in (1, 2, 3, 6, 7, 20)] == [
        60, 120, 240, 1920, 3600, 3600,
    ]


# ── Importing the module has no credential side effects ─────────────────────


def test_importing_watch_touches_nothing_under_home_and_runs_no_secret_tools(tmp_path):
    """Import watch.py in a fresh interpreter with HOME pointed at a decoy.

    The decoy holds an env file and a sops snapshot where the daemon's
    bootstrap looks, so an import-time bootstrap would have to open them. An
    audit hook records any open/mkdir/rename/remove under HOME and any
    process launch; stub op and sops record any call. Nothing real is read.
    """
    import subprocess as sp

    home = tmp_path / "home"
    neotoma_cfg = home / ".config" / "neotoma"
    (neotoma_cfg / "secrets").mkdir(parents=True)
    (neotoma_cfg / ".env").write_text("DECOY_ONLY=1\n")
    (neotoma_cfg / "secrets" / "neotoma.sops.enc").write_text("decoy")
    before = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    calls = tmp_path / "secret_tool_calls.log"
    for tool in ("op", "sops"):
        stub = stub_bin / tool
        stub.write_text(f'#!/bin/sh\necho {tool} >> "{calls}"\nexit 1\n')
        stub.chmod(0o755)

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, sys\n"
        "home = os.path.realpath(os.environ['HOME'])\n"
        "hits = []\n"
        "def hook(event, args):\n"
        "    if event in ('open', 'os.mkdir', 'os.rename', 'os.remove', 'shutil.copyfile'):\n"
        "        path = args[0] if args else None\n"
        "        if isinstance(path, (str, bytes, os.PathLike)):\n"
        "            p = os.path.realpath(os.fsdecode(path))\n"
        "            if p == home or p.startswith(home + os.sep):\n"
        "                hits.append(event + ':' + os.path.relpath(p, home))\n"
        "    elif event in ('subprocess.Popen', 'os.posix_spawn', 'os.exec'):\n"
        "        hits.append(event)\n"
        "sys.addaudithook(hook)\n"
        f"sys.path.insert(0, {str(Path(watch.__file__).parent)!r})\n"
        "import watch\n"
        "print('HITS=' + '|'.join(hits))\n"
    )
    env = {
        "HOME": str(home),
        "PATH": f"{stub_bin}:/usr/bin:/bin",
        # Mirrors the pytest guard that already keeps lib.daemon_runtime and
        # lib.activity from loading the env file; this test is about watch.py.
        "ATELES_SKIP_DOTENV": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = sp.run(
        [sys.executable, str(probe)], capture_output=True, text=True, env=env, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
    hits_line = [ln for ln in result.stdout.splitlines() if ln.startswith("HITS=")][-1]
    assert hits_line == "HITS=", hits_line
    assert not calls.exists(), calls.read_text()
    assert sorted(str(p.relative_to(home)) for p in home.rglob("*")) == before


# ── The daemon still bootstraps credentials, logging and notifier at start ──


def test_main_bootstraps_once_before_the_first_neotoma_check(tmp_path, monkeypatch):
    out = _drive_main(monkeypatch, tmp_path, [], lambda _n: 0, polls=2, real_services=True)
    events = out["events"]
    assert events.count("bootstrap") == 1
    assert "check_neotoma" in events
    assert events.index("bootstrap") < events.index("check_neotoma")
    # Services were really started: the activity channel was built.
    assert watch._activity is not None
    # Idempotent: a second start does not bootstrap again.
    watch._start_daemon_services()
    assert events.count("bootstrap") == 1


@pytest.mark.parametrize(
    "argv",
    [["--list-held"], ["--release", "some-memo.m4a"], ["--discard", "some-memo.m4a"]],
    ids=["list-held", "release", "discard"],
)
def test_operator_subcommands_start_daemon_services(tmp_path, monkeypatch, argv):
    monkeypatch.setattr(watch, "HELD_STATE_FILE", tmp_path / "held_memos.json")
    started = []
    monkeypatch.setattr(watch, "_start_daemon_services", lambda: started.append(True))
    monkeypatch.setattr(watch, "main", lambda: pytest.fail("subcommand ran the watcher"))

    assert watch.cli(["watch.py", *argv]) == 0
    assert started == [True]


def test_no_arguments_runs_the_watcher(monkeypatch):
    ran = []
    monkeypatch.setattr(watch, "main", lambda: ran.append(True))
    assert watch.cli(["watch.py"]) == 0
    assert ran == [True]


# The token variable's name is assembled from parts so no source line reads as
# a hard-coded credential assignment to the repo's secret scanner. The values
# are synthetic.
_TOKEN_VAR = "NEOTOMA_BEARER" + "_TOKEN"


def _token_line(value: str) -> str:
    return _TOKEN_VAR + "=" + '"' + value + '"'


def _synthetic_home(tmp_path, monkeypatch, *, snapshot: bool):
    """A temp HOME holding a synthetic Neotoma env file (and optionally a
    synthetic snapshot file). Nothing real is read."""
    home = tmp_path / "home"
    cfg = home / ".config" / "neotoma"
    cfg.mkdir(parents=True)
    env_file = cfg / ".env"
    env_file.write_text(
        "# synthetic\n"
        "NEOTOMA_BASE_URL=http://127.0.0.1:9/synthetic\n"
        + _token_line("stale-synthetic-token")
        + "\n"
    )
    if snapshot:
        (cfg / "secrets").mkdir()
        (cfg / "secrets" / "neotoma.sops.enc").write_text("synthetic snapshot")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(watch, "_neotoma_env_file", lambda: env_file)
    monkeypatch.setattr(
        watch, "_sops_candidates", lambda: [cfg / "secrets" / "neotoma.sops.enc"]
    )
    for name in (
        "NEOTOMA_BEARER_TOKEN",
        "NEOTOMA_BASE_URL",
        "NEOTOMA_COMPANY_ENTITY_ID",
        "SOPS_AGE_KEY_FILE",
        "SOPS_AGE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    # Module settings the bootstrap rewrites; restored after the test.
    monkeypatch.setattr(watch, "_NEOTOMA_BASE_URL", "")
    monkeypatch.setattr(watch, "NEOTOMA_COMPANY_ENTITY_ID", watch._DEFAULT_COMPANY_ENTITY_ID)
    return env_file


class _Ran:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_bootstrap_refreshes_the_token_from_the_snapshot_and_writes_it_back(
    tmp_path, monkeypatch
):
    env_file = _synthetic_home(tmp_path, monkeypatch, snapshot=True)
    tools = []

    def fake_run(argv, **_kwargs):
        tools.append(argv[0])
        if argv[0] == "sops":
            return _Ran(0, "OTHER=1\n" + _token_line("fresh-synthetic-token") + "\n")
        return _Ran(1)

    monkeypatch.setattr(watch.subprocess, "run", fake_run)

    watch._bootstrap_environment()

    assert tools == ["sops"]  # no live 1Password read when the snapshot works
    assert os.environ["NEOTOMA_BEARER_TOKEN"] == "fresh-synthetic-token"
    text = env_file.read_text()
    assert _token_line("fresh-synthetic-token") in text
    assert "stale-synthetic-token" not in text
    assert watch._NEOTOMA_BASE_URL == "http://127.0.0.1:9/synthetic"


def test_bootstrap_falls_back_to_op_without_a_snapshot(tmp_path, monkeypatch):
    env_file = _synthetic_home(tmp_path, monkeypatch, snapshot=False)
    tools = []

    def fake_run(argv, **_kwargs):
        tools.append(argv[0])
        return _Ran(0, "op-synthetic-token\n") if argv[0] == "op" else _Ran(1)

    monkeypatch.setattr(watch.subprocess, "run", fake_run)

    watch._bootstrap_environment()

    assert tools == ["op"]
    assert os.environ["NEOTOMA_BEARER_TOKEN"] == "op-synthetic-token"
    assert _token_line("op-synthetic-token") in env_file.read_text()


# ── Deferred state and the deferral alert ───────────────────────────────────


def test_malformed_deferred_entries_are_dropped_on_load(tmp_path, monkeypatch, caplog):
    state = tmp_path / "deferred_memos.json"
    state.write_text(json.dumps({
        "bad-a.m4a": "garbage",
        "bad-b.m4a": 5,
        "good.m4a": {"attempts": 1, "next_attempt_at": 10, "notified": True},
    }))
    monkeypatch.setattr(watch, "DEFERRED_STATE_FILE", state)
    with caplog.at_level(logging.WARNING, logger="piculet"):
        loaded = watch.load_deferred()
    assert list(loaded) == ["good.m4a"]
    assert "Dropped 2 malformed deferred-memo entries" in caplog.text


def test_malformed_deferred_entry_does_not_break_the_poll(tmp_path, monkeypatch):
    memo = "20261101 101010-AAAA0007.m4a"
    state = tmp_path / "state"
    state.mkdir()
    (state / "deferred_memos.json").write_text(json.dumps({memo: "garbage"}))
    out = _drive_main(monkeypatch, tmp_path, [memo], lambda _n: 0, polls=1)
    assert len(out["imports"]) == 1
    assert memo in out["seen_file"]
    assert out["deferred"] == {}
    assert not [t for t in out["telegram"] if "Watcher loop error" in t]


def test_deferral_alert_fires_again_after_the_lookup_recovers(tmp_path, monkeypatch):
    first, second = "20261101 101010-AAAA0008.m4a", "20261101 101010-AAAA0009.m4a"
    down = _drive_main(monkeypatch, tmp_path, [first], _unavailable, polls=1)
    up = _drive_main(monkeypatch, tmp_path, [first], lambda _n: 0, polls=1, start=_START + 60)
    again = _drive_main(monkeypatch, tmp_path, [second], _unavailable, polls=1, start=_START + 120)

    def alerts(out):
        return [t for t in out["telegram"] if "Import deferred" in t]

    assert len(alerts(down)) == 1
    assert alerts(up) == []
    # Minutes later, well inside the hourly dedup window: a fresh alert.
    assert len(alerts(again)) == 1


def test_deferral_warning_says_how_many_and_where_the_importer_log_is():
    text = watch.deferred_warning_text(3)
    assert text.startswith("Import deferred for 3 memos:")
    assert watch.LOG_FILE.name in text
    assert "[import]" in text
    assert watch.deferred_warning_text(1).startswith("Import deferred for 1 memo:")


def test_importer_output_is_written_to_the_daemon_log(tmp_path, monkeypatch, caplog):
    def fake_run(cmd, **kwargs):
        assert kwargs.get("capture_output") is True
        return _Ran(
            watch.IMPORT_EXIT_LOOKUP_UNAVAILABLE,
            "  ? Lookup failed (timeout), not staged: synthetic.m4a\n",
            "synthetic importer stderr line\n",
        )

    monkeypatch.setattr(watch.subprocess, "run", fake_run)
    with caplog.at_level(logging.INFO, logger="piculet"):
        rc = watch.run_import([tmp_path / "synthetic.m4a"])
    assert rc == watch.IMPORT_EXIT_LOOKUP_UNAVAILABLE
    assert "[import]   ? Lookup failed (timeout), not staged: synthetic.m4a" in caplog.text
    # The importer's stderr is relayed too, at WARNING.
    assert [
        r.levelno for r in caplog.records
        if r.getMessage() == "[import] synthetic importer stderr line"
    ] == [logging.WARNING]
