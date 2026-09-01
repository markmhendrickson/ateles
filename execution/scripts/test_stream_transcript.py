"""Tests for the streaming live-transcript path (ateles#625).

The emphasis is deliberately on FAILURE behaviour rather than the happy path.
A streaming transcript that fails quietly is worse than chunking that fails
loudly (ateles#619): the operator cannot tell "nobody is talking" from "the
socket died" by looking at an empty transcript, so the code has to tell them.
"""

import json
import math
import struct
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import stream_transcript as st  # noqa: E402


def _pcm(amplitude: int, samples: int = 2400) -> bytes:
    """PCM16 at a constant amplitude — a crude but sufficient level source."""
    return struct.pack(f"<{samples}h", *([amplitude] * samples))


# ---------------------------------------------------------------------------
# Single capture, tee'd — the recording must stay durable
# ---------------------------------------------------------------------------


def test_capture_writes_both_a_file_and_a_stream():
    """ONE ffmpeg, TWO outputs, from the same input.

    Parallel capture was rejected because the streamed audio would not be the
    same bytes as the recorded audio, so the live transcript would approximate
    the file rather than index it.
    """
    cmd = st.build_capture_command(":3", Path("/tmp/rec.m4a"))

    assert cmd.count("-i") == 1, "must read the input device exactly once"
    assert "/tmp/rec.m4a" in cmd, "durable recording must be an output"
    assert "pipe:1" in cmd, "PCM stream must be an output"
    assert cmd.count("-map") == 2, "both outputs must map the same input"


def test_capture_stream_output_matches_the_realtime_format():
    cmd = st.build_capture_command(":3", Path("/tmp/rec.m4a"))
    assert "s16le" in cmd
    assert str(st.SAMPLE_RATE) in cmd


def test_capture_never_bounds_the_input_with_duration():
    """`-t` after `-i` bounds only the first output and never finalizes the
    container — a 44-byte m4a with no moov atom, i.e. the recording is lost.
    Capture is stopped by signal instead, so the muxer always finalizes.
    """
    cmd = st.build_capture_command(":3", Path("/tmp/rec.m4a"))
    assert "-t" not in cmd


def test_recording_filename_keeps_the_calendar_parseable_timestamp():
    """Calendar matching parses the meeting time out of the FILENAME."""
    from datetime import datetime

    path = st.next_recording_path(Path("/tmp/recs"), now=datetime(2026, 9, 1, 14, 30))
    assert path.name.startswith("20260901 1430")
    assert path.suffix == ".m4a"


# ---------------------------------------------------------------------------
# Server VAD — correctness AND cost both depend on it
# ---------------------------------------------------------------------------


def test_session_enables_server_vad():
    """Without server VAD you commit silence and pay for wall-clock audio, and
    turns never close on speech boundaries (the 42% truncation defect).
    """
    msg = st.session_update_message()
    turn_detection = msg["session"]["audio"]["input"]["turn_detection"]
    assert turn_detection is not None, "turn_detection: null bills silence"
    assert turn_detection["type"] == "server_vad"


def test_session_uses_the_current_api_shape():
    """The `OpenAI-Beta: realtime=v1` era shape fails closed now."""
    msg = st.session_update_message()
    assert msg["type"] == "session.update"
    assert msg["session"]["type"] == "transcription"
    assert "input" in msg["session"]["audio"]


# ---------------------------------------------------------------------------
# Signal level — growth is NOT health
# ---------------------------------------------------------------------------


def test_dbfs_of_digital_silence_is_minus_infinity():
    assert st.pcm16_dbfs(_pcm(0)) == -math.inf


def test_dbfs_of_full_scale_is_about_zero():
    assert st.pcm16_dbfs(_pcm(32000)) == pytest.approx(0.0, abs=0.5)


def test_dbfs_ranks_quiet_below_loud():
    assert st.pcm16_dbfs(_pcm(100)) < st.pcm16_dbfs(_pcm(10000))


def test_dbfs_of_empty_payload_is_none():
    assert st.pcm16_dbfs(b"") is None


def test_a_growing_but_silent_capture_is_unhealthy():
    """THE signal-level check the design calls for.

    ffmpeg alive, bytes flowing, file growing on disk — and nothing in it. Under
    a naive "is the subprocess up / is the file growing" check this reports
    healthy while the operator talks into a muted input.
    """
    monitor = st.HealthMonitor(signal_window_seconds=10, now=0.0)
    t = 0.0
    while t < 20.0:
        monitor.note_audio(_pcm(0), now=t)
        monitor.note_socket_event(now=t)
        t += 1.0

    assert monitor.bytes_streamed > 0, "audio really was flowing"
    assert not monitor.is_healthy(now=t)
    assert any("silent input" in p for p in monitor.problems(now=t))


def test_a_growing_capture_with_speech_is_healthy():
    monitor = st.HealthMonitor(signal_window_seconds=10, now=0.0)
    t = 0.0
    while t < 20.0:
        monitor.note_audio(_pcm(6000), now=t)
        monitor.note_socket_event(now=t)
        t += 1.0

    assert monitor.is_healthy(now=t)
    assert monitor.problems(now=t) == []


def test_silent_input_is_not_flagged_before_a_full_window():
    """A run must not cry wolf in its first moments."""
    monitor = st.HealthMonitor(signal_window_seconds=120, now=0.0)
    monitor.note_audio(_pcm(0), now=1.0)
    assert not any("silent input" in p for p in monitor.problems(now=2.0))


# ---------------------------------------------------------------------------
# Loud failure — the #619 regressions
# ---------------------------------------------------------------------------


def test_a_stalled_capture_is_reported():
    """The tailer died during a pause and nothing noticed."""
    monitor = st.HealthMonitor(stall_seconds=10, now=0.0)
    monitor.note_audio(_pcm(6000), now=0.0)
    monitor.note_socket_event(now=100.0)

    problems = monitor.problems(now=100.0)
    assert any("capture stalled" in p for p in problems)
    assert not monitor.is_healthy(now=100.0)


def test_a_dead_socket_is_distinguished_from_a_quiet_room():
    """Both produce no transcript; only an explicit liveness check separates them.

    Conflating these is exactly how #619 stayed invisible.
    """
    quiet = st.HealthMonitor(socket_silent_seconds=45, signal_window_seconds=1e9, now=0.0)
    dead = st.HealthMonitor(socket_silent_seconds=45, signal_window_seconds=1e9, now=0.0)

    t = 0.0
    while t < 120.0:
        # Identical audio in both cases — a person present but not speaking.
        quiet.note_audio(_pcm(3000), now=t)
        dead.note_audio(_pcm(3000), now=t)
        # The healthy socket keeps answering; the dead one went away at t=0.
        quiet.note_socket_event(now=t)
        t += 1.0

    assert quiet.is_healthy(now=t), "a quiet room is not a failure"
    assert not dead.is_healthy(now=t), "a dead socket must be caught"
    assert any("socket silent" in p for p in dead.problems(now=t))


def test_never_healthy_while_errors_are_non_zero():
    """#619's second regression: every chunk erroring, status reported healthy."""
    monitor = st.HealthMonitor(now=0.0)
    monitor.note_audio(_pcm(6000), now=0.0)
    monitor.note_error(now=0.0)

    assert monitor.errors == 1
    assert not monitor.is_healthy(now=1.0)
    assert monitor.summary(now=1.0)["healthy"] is False


def test_a_persistent_problem_announces_once_then_re_announces_after_recovery():
    monitor = st.HealthMonitor(stall_seconds=10, signal_window_seconds=1e9, now=0.0)
    monitor.note_audio(_pcm(6000), now=0.0)
    monitor.note_socket_event(now=100.0)

    assert monitor.new_problems(now=100.0), "first sighting is announced"
    assert monitor.new_problems(now=101.0) == [], "not spammed while unchanged"

    monitor.note_audio(_pcm(6000), now=102.0)
    assert monitor.new_problems(now=102.0) == [], "recovered"

    monitor.note_socket_event(now=200.0)
    assert monitor.new_problems(now=200.0), "recurrence is announced again"


def test_error_records_are_written_as_visible_jsonl_lines():
    record = st.error_record(3, "socket closed unexpectedly")
    assert record["ok"] is False
    assert "socket closed" in record["error"]
    assert record["source"] == "stream"


# ---------------------------------------------------------------------------
# JSONL compatibility — the Monitor must not change
# ---------------------------------------------------------------------------


def test_transcript_record_matches_the_chunking_tailer_shape():
    """Downstream consumes `<stem>_live.jsonl`; nothing may change for it."""
    record = st.transcript_record(0, "hello there", start_s=1.0, end_s=2.5)
    for key in ("chunk", "t", "start_s", "end_s", "ok", "text"):
        assert key in record, f"missing key the Monitor reads: {key}"
    assert record["ok"] is True
    assert record["text"] == "hello there"


def test_records_serialize_as_one_json_line_each():
    records = [
        st.transcript_record(0, "first", start_s=0, end_s=1),
        st.error_record(1, "boom"),
    ]
    blob = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    assert len(blob.splitlines()) == 2
    assert all(json.loads(line) for line in blob.splitlines())


def test_non_ascii_transcripts_survive_serialization():
    record = st.transcript_record(0, "¿cómo estás? — muy bien", start_s=0, end_s=1)
    assert json.loads(json.dumps(record, ensure_ascii=False))["text"] == record["text"]


# ---------------------------------------------------------------------------
# Fallback to chunking — degrade, but say so
# ---------------------------------------------------------------------------


def test_fallback_invokes_the_existing_chunking_tailer():
    cmd = st.fallback_command(Path("/tmp/out.jsonl"))
    assert "live_transcript_tail.py" in " ".join(cmd)
    assert "--follow" in cmd, "fallback must follow sessions too"
    assert "/tmp/out.jsonl" in cmd


def test_fallback_announces_itself_in_the_transcript(tmp_path, monkeypatch):
    """Producing nothing is the one unacceptable outcome — a silent live
    transcript looks exactly like a quiet room to the operator.
    """
    out = tmp_path / "live.jsonl"
    calls = {}

    monkeypatch.setattr(st.subprocess, "call", lambda cmd: calls.setdefault("cmd", cmd) and 0 or 0)
    monkeypatch.setattr(st, "FALLBACK_TAILER", tmp_path / "live_transcript_tail.py")
    (tmp_path / "live_transcript_tail.py").write_text("#")

    st.run_fallback(out, "socket refused the connection")

    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert lines, "the degradation must be visible in the transcript itself"
    assert lines[0]["ok"] is False
    assert lines[0]["degraded"] == "chunking"
    assert "socket refused" in lines[0]["error"]


def test_fallback_reports_when_it_cannot_even_degrade(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "FALLBACK_TAILER", tmp_path / "missing.py")
    rc = st.run_fallback(tmp_path / "live.jsonl", "no key")
    assert rc != 0


def test_missing_api_key_degrades_rather_than_producing_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "load_openai_key", lambda: None)
    monkeypatch.setattr(st, "shutil", st.shutil)
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    seen = {}
    monkeypatch.setattr(st, "run_fallback", lambda out, reason: seen.update(reason=reason) or 0)

    rc = st.main(["--dir", str(tmp_path)])
    assert rc == 0
    assert "OPENAI_API_KEY" in seen["reason"]


def test_no_fallback_flag_fails_loudly_instead_of_degrading(tmp_path, monkeypatch):
    """An operator who asked for streaming specifically must not be silently
    downgraded to the path they were trying to leave.
    """
    monkeypatch.setattr(st, "load_openai_key", lambda: None)
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    rc = st.main(["--dir", str(tmp_path), "--no-fallback"])
    assert rc == 2


def test_fallback_only_flag_skips_streaming(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(st, "run_fallback", lambda out, reason: seen.update(reason=reason) or 0)
    assert st.main(["--dir", str(tmp_path), "--fallback-only"]) == 0
    assert "fallback-only" in seen["reason"]


# ---------------------------------------------------------------------------
# Session following — no external supervisor
# ---------------------------------------------------------------------------


def test_follows_into_a_new_session_when_idle():
    """`--file` pinned the old tailer to one recording, so it needed an external
    supervisor to survive the operator starting a new session.
    """
    assert st.should_follow_to_new_session(400.0, follow=True, idle_limit=300.0)


def test_does_not_roll_over_during_an_ordinary_pause():
    assert not st.should_follow_to_new_session(30.0, follow=True, idle_limit=300.0)


def test_follow_disabled_stays_on_one_recording():
    assert not st.should_follow_to_new_session(9999.0, follow=False, idle_limit=300.0)


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------


def test_api_key_is_read_from_env_first(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-value")
    assert st.load_openai_key() == "sk-test-value"


def test_api_key_never_appears_in_the_capture_command(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    cmd = st.build_capture_command(":3", Path("/tmp/rec.m4a"))
    assert "sk-secret" not in " ".join(cmd)


def test_health_summary_carries_no_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monitor = st.HealthMonitor(now=0.0)
    monitor.note_audio(_pcm(6000), now=0.0)
    assert "sk-secret" not in json.dumps(monitor.summary(now=1.0))


def test_losing_both_paths_is_the_loudest_failure(tmp_path, monkeypatch):
    """Streaming down AND chunking unavailable: the operator must be told.

    Discovering from an empty transcript that nothing was ever listening is the
    outcome the whole loud-failure requirement exists to prevent.
    """
    out = tmp_path / "live.jsonl"
    monkeypatch.setattr(st, "FALLBACK_TAILER", tmp_path / "absent.py")

    rc = st.run_fallback(out, "socket refused")

    assert rc == 2
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert any(line.get("fatal") for line in lines), "must be marked fatal"
    assert any("NO LIVE TRANSCRIPT" in line["error"] for line in lines)
