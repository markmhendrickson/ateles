"""Tests for the streaming live-transcript path (ateles#625).

The emphasis is deliberately on FAILURE behaviour rather than the happy path.
A streaming transcript that fails quietly is worse than chunking that fails
loudly (ateles#619): the operator cannot tell "nobody is talking" from "the
socket died" by looking at an empty transcript, so the code has to tell them.
"""

import json
import math
import re
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


def _turn_detection(msg: dict) -> dict:
    return msg["session"]["audio"]["input"]["turn_detection"]


def test_session_sends_vad_tuning_explicitly():
    """Naming only the type left silence_duration_ms on the API default of
    500ms, which closes a turn on an ordinary mid-sentence pause. Both values
    must appear in the request rather than being inherited.
    """
    turn_detection = _turn_detection(st.session_update_message())
    assert "silence_duration_ms" in turn_detection, (
        "unset means the API default of 500ms, which splits sentences"
    )
    assert "prefix_padding_ms" in turn_detection
    assert turn_detection["silence_duration_ms"] == st.VAD_SILENCE_DURATION_MS
    assert turn_detection["prefix_padding_ms"] == st.VAD_PREFIX_PADDING_MS


def test_silence_threshold_sits_in_the_measured_empty_band():
    """The default is derived, not picked. Real-session turn gaps are bimodal:
    mid-sentence pauses below 0.79s, genuine boundaries at 1.46s and above.
    A default outside that empty band would cut one of the two populations.
    """
    assert 790 < st.VAD_SILENCE_DURATION_MS < 1460


def test_silence_threshold_stays_within_the_gate_hangover():
    """The local RMS gate keeps forwarding audio for GATE_HANGOVER_SECONDS
    after speech drops. A silence threshold longer than that would starve the
    server of the silence it is waiting for and turns would stop closing.
    """
    assert st.VAD_SILENCE_DURATION_MS < st.GATE_HANGOVER_SECONDS * 1000


def test_session_vad_tuning_is_overridable():
    """The operator tunes this without a code change."""
    msg = st.session_update_message(
        silence_duration_ms=1500, prefix_padding_ms=450
    )
    turn_detection = _turn_detection(msg)
    assert turn_detection["silence_duration_ms"] == 1500
    assert turn_detection["prefix_padding_ms"] == 450
    assert turn_detection["type"] == "server_vad"


def test_vad_tuning_flags_reach_the_session_message():
    """A flag that parses but never reaches the socket is not configurable.
    Guards the arg -> stream_session -> session_update_message chain.
    """
    args = st.build_parser().parse_args(
        ["--vad-silence-ms", "900", "--vad-prefix-padding-ms", "250"]
    )
    assert args.vad_silence_ms == 900
    assert args.vad_prefix_padding_ms == 250

    turn_detection = _turn_detection(
        st.session_update_message(
            silence_duration_ms=args.vad_silence_ms,
            prefix_padding_ms=args.vad_prefix_padding_ms,
        )
    )
    assert turn_detection["silence_duration_ms"] == 900
    assert turn_detection["prefix_padding_ms"] == 250


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
        # A person present but NOT speaking: room tone at -54.7 dBFS, which is
        # above the silent-mic floor (so the mic is plainly live) and below the
        # speech threshold (so no transcript is owed). _pcm(3000) is -20.8 dBFS
        # — that is speech level, not a quiet room, and using it here is what
        # let the socket timer look correct while it false-alarmed in
        # production.
        quiet.note_audio(_pcm(60), now=t)
        # The dead-socket case must OFFER speech that goes unanswered.
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


# ---------------------------------------------------------------------------
# Turn boundaries — the fragmentation defect (ateles#631, observed 2026-09-01)
# ---------------------------------------------------------------------------
#
# Fixtures below are the operator's REAL captured turns from
# `20260901 1237 stream_live.jsonl`, fabrications and genuine speech alike.


# The eight consecutive turns as they were actually written: every one exactly
# 5.0s long, each overlapping the previous by 2-3s. Both properties are
# impossible for real VAD boundaries and are the signature of the bug.
OPERATOR_FRAGMENTED_TURNS = [
    (191.39, 196.39, "Can you hear me?"),
    (194.02, 199.02, "A wida\u0107 o mnie."),
    (196.36, 201.36, "In the session"),
    (199.27, 204.27, "Whatever you hear me saying, I cleaned up."),
    (200.43, 205.43, "Soita."),
    (203.84, 208.84, "Including any kind of favors or stuttering."),
    (207.80, 212.80, "Formattway that sticks out."),
    (209.60, 214.60, "The text that you're actually writing."),
]


def test_the_operator_run_shows_the_fragmentation_signature():
    """Characterizes the defect: constant width plus overlap."""
    widths = {round(e - s, 2) for s, e, _ in OPERATOR_FRAGMENTED_TURNS}
    assert widths == {5.0}, "every observed turn was exactly 5.0s — a constant, not a measurement"

    overlaps = [
        OPERATOR_FRAGMENTED_TURNS[i][1] - OPERATOR_FRAGMENTED_TURNS[i + 1][0]
        for i in range(len(OPERATOR_FRAGMENTED_TURNS) - 1)
    ]
    assert all(o > 0 for o in overlaps), "consecutive turns overlapped"


def test_transcript_record_does_not_impose_a_fixed_window():
    """A turn's width must be whatever the caller measured, not a constant.

    Fails on the old code path, which computed `start_s=elapsed - 5.0` at the
    call site and could therefore only ever emit 5.0s turns.
    """
    short = st.transcript_record(0, "In the session", start_s=196.36, end_s=197.9)
    long = st.transcript_record(1, "a longer sentence", start_s=199.27, end_s=205.6)

    assert round(short["end_s"] - short["start_s"], 2) == 1.54
    assert round(long["end_s"] - long["start_s"], 2) == 6.33
    widths = {short["end_s"] - short["start_s"], long["end_s"] - long["start_s"]}
    assert len(widths) == 2, "widths must vary with the speech, not be pinned to 5.0s"


def test_vad_boundaries_produce_non_overlapping_turns():
    """Consecutive VAD turns must not overlap, whatever the arrival times.

    Server VAD reports `audio_start_ms`/`audio_end_ms` on the audio clock. Using
    them means a turn indexes the recording; using wall clock does not.
    """
    events = [(191390, 193100), (196360, 197900), (199270, 205600)]
    records = [
        st.transcript_record(i, f"turn {i}", start_s=a / 1000.0, end_s=b / 1000.0)
        for i, (a, b) in enumerate(events)
    ]
    for earlier, later in zip(records, records[1:]):
        assert later["start_s"] >= earlier["end_s"], "VAD turns must not overlap"


# ---------------------------------------------------------------------------
# Hallucination filtering on the streaming path (ateles#633's filter, shared)
# ---------------------------------------------------------------------------


def test_streaming_path_imports_the_shared_filter():
    """Not a second copy — the same module the chunking tailer uses."""
    assert st.screen_transcription is not None
    from hallucination_filter import screen_transcription

    assert st.screen_transcription is screen_transcription


@pytest.mark.parametrize(
    "text,reason",
    [
        # The operator's real fabrications, verbatim from the live JSONL.
        ("A wida\u0107 o mnie.", "foreign_diacritic"),   # Polish
        ("Sanaşılarızatifektir.", "foreign_diacritic"),  # Turkish
        ("\u0414\u043e\u0431\u0440\u0435 \u0443\u0442\u0440\u043e.", "script_mismatch"),
    ],
)
def test_real_fabrications_are_filtered(text, reason):
    verdict = st.screen_transcription(
        text, expected_language="en", window_seconds=5.0, vad_closed=True
    )
    assert verdict.filtered, f"{text!r} is fabricated and must be caught"
    assert verdict.reason == reason


@pytest.mark.parametrize(
    "text",
    [
        # The operator's real speech from the SAME minutes, interleaved with
        # the fabrications above. None of it may be filtered.
        "Can you hear me?",
        "In the session",
        "Whatever you hear me saying, I cleaned up.",
        "Including any kind of favors or stuttering.",
        "Formattway that sticks out.",
        "The text that you're actually writing.",
        "We can probably remove NTFS.",
        "Ctrl-Alt-Shift-Alt-Enter",
    ],
)
def test_real_speech_survives_the_filter(text):
    verdict = st.screen_transcription(
        text, expected_language="en", window_seconds=5.0, vad_closed=True
    )
    assert not verdict.filtered, f"{text!r} is real speech and must survive"


def test_filtered_text_is_kept_not_dropped():
    """A false positive must stay recoverable by eye."""
    record = st.transcript_record(
        0, "A widać o mnie.", start_s=194.02, end_s=195.1,
        filtered="foreign_diacritic", filtered_detail="'ć' outside en/es",
    )
    assert record["text"] == "A widać o mnie.", "text is never discarded"
    assert record["filtered"] == "foreign_diacritic"
    assert record["ok"] is True


def test_a_passing_turn_carries_no_filtered_key():
    record = st.transcript_record(0, "Can you hear me?", start_s=191.39, end_s=193.1)
    assert "filtered" not in record


# ---------------------------------------------------------------------------
# Socket-silence must not false-alarm on an ordinary pause (ateles#631 review)
# ---------------------------------------------------------------------------


def test_an_ordinary_pause_does_not_raise_a_socket_alarm():
    """The operator's real session alarmed three times in four minutes.

    With server VAD the API sends nothing while nobody speaks, so timing socket
    silence against wall clock makes every pause a fault. Room tone at -54.7
    dBFS is a live mic with nobody talking: no transcript is owed.
    """
    monitor = st.HealthMonitor(
        socket_silent_seconds=45, signal_window_seconds=1e9, now=0.0
    )
    t = 0.0
    while t < 200.0:
        monitor.note_audio(_pcm(60), now=t)
        t += 1.0

    assert not any("socket silent" in p for p in monitor.problems(now=t))
    assert monitor.is_healthy(now=t), "a long quiet stretch is normal operation"


def test_speech_going_unanswered_still_raises_the_alarm():
    """The #619 protection must survive the false-alarm fix."""
    monitor = st.HealthMonitor(
        socket_silent_seconds=45, signal_window_seconds=1e9, now=0.0
    )
    t = 0.0
    while t < 200.0:
        monitor.note_audio(_pcm(3000), now=t)
        t += 1.0

    problems = monitor.problems(now=t)
    assert any("socket silent" in p for p in problems), "unanswered speech IS a dead socket"
    assert not monitor.is_healthy(now=t)


def test_the_socket_alarm_no_longer_contradicts_itself():
    """The old text asserted 'NOT a quiet room' while firing on a quiet room."""
    monitor = st.HealthMonitor(
        socket_silent_seconds=45, signal_window_seconds=1e9, now=0.0
    )
    t = 0.0
    while t < 200.0:
        monitor.note_audio(_pcm(3000), now=t)
        t += 1.0
    message = next(p for p in monitor.problems(now=t) if "socket silent" in p)
    assert "going unanswered" in message


# ---------------------------------------------------------------------------
# TurnBoundaries — the actual site of the fragmentation defect
# ---------------------------------------------------------------------------


def _turn(boundaries, start_ms, end_ms, streamed_s):
    boundaries.observe(
        {"type": "input_audio_buffer.speech_started", "audio_start_ms": start_ms}
    )
    boundaries.observe(
        {"type": "input_audio_buffer.speech_stopped", "audio_end_ms": end_ms}
    )
    return boundaries.claim(streamed_s)


def test_vad_events_drive_the_turn_boundaries():
    boundaries = st.TurnBoundaries()
    start_s, end_s, vad_closed = _turn(boundaries, 191390, 193100, 199.0)
    assert (round(start_s, 2), round(end_s, 2)) == (191.39, 193.10)
    assert vad_closed is True


def test_replaying_the_operator_session_yields_no_overlap_and_varied_widths():
    """The regression test for 2026-09-01.

    Replays the operator's eight turns as the VAD boundaries that actually
    produced them. Under the old wall-clock labelling these came out as eight
    constant 5.0s windows overlapping by 2-3s; under VAD boundaries they must
    be non-overlapping and of varying width.
    """
    # (audio_start_ms, audio_end_ms). Starts are the operator's real observed
    # onsets; ends are each capped below the next onset, which is precisely the
    # property server VAD guarantees and wall-clock labelling destroyed. These
    # are illustrative ends, not captured ones — the captured file has no VAD
    # ends to quote, because the bug is that it never recorded any.
    vad_events = [
        (191390, 193100),
        (194020, 194600),
        (196360, 197900),
        (199270, 200100),
        (200430, 200900),
        (203840, 206100),
        (207800, 209100),
        (209600, 212400),
    ]
    boundaries = st.TurnBoundaries()
    records = []
    for i, (a, b) in enumerate(vad_events):
        start_s, end_s, _ = _turn(boundaries, a, b, b / 1000.0 + 2.0)
        records.append(
            st.transcript_record(i, OPERATOR_FRAGMENTED_TURNS[i][2], start_s=start_s, end_s=end_s)
        )

    widths = {round(r["end_s"] - r["start_s"], 2) for r in records}
    assert widths != {5.0}, "widths must not be a constant"
    assert len(widths) > 1, "real speech does not produce identical turn widths"

    for earlier, later in zip(records, records[1:]):
        assert later["start_s"] >= earlier["end_s"], (
            f"turns overlap: {earlier['end_s']} > {later['start_s']}"
        )


def test_a_missing_vad_boundary_falls_back_to_the_audio_clock():
    """Degrade to a coarse offset, never to a fabricated wall-clock window."""
    boundaries = st.TurnBoundaries()
    start_s, end_s, vad_closed = boundaries.claim(207.5)
    assert (start_s, end_s) == (207.5, 207.5)
    assert vad_closed is False, "no VAD close was reported for this turn"


def test_boundaries_never_produce_a_negative_width():
    boundaries = st.TurnBoundaries()
    boundaries.observe(
        {"type": "input_audio_buffer.speech_started", "audio_start_ms": 200000}
    )
    boundaries.observe(
        {"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 199000}
    )
    start_s, end_s, _ = boundaries.claim(201.0)
    assert end_s >= start_s


def test_delta_events_are_not_turn_boundaries_and_never_finish_a_turn():
    """Only `.completed` may reach the JSONL; `.delta` is a partial hypothesis."""
    boundaries = st.TurnBoundaries()
    consumed = boundaries.observe(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": "Can you",
        }
    )
    assert consumed is False, "a delta is not a VAD event"
    assert boundaries.pending == 0, "a delta must not open a span"


def test_the_captured_operator_turns_are_all_exactly_five_seconds():
    """The defect at scale: 19 of 19 captured turns had identical width.

    A duration that never varies across nineteen different utterances is not a
    measurement of speech; it is a constant. This is the strongest single piece
    of evidence that the boundary came from `now - 5.0` and not from VAD.
    """
    widths = {round(e - s, 2) for s, e, _ in OPERATOR_FRAGMENTED_TURNS}
    assert widths == {5.0}


def test_an_empty_completed_transcription_never_becomes_a_turn():
    """A completed event with no transcript must not emit a null-timestamp row.

    Whisper returns empty transcripts for turns VAD opened on noise. Emitting
    one would put a row in the JSONL with no text and no boundaries, which the
    session Monitor would render as a blank turn.
    """
    for empty in ("", "   ", None):
        text = (empty or "").strip()
        assert not text, "the guard is `if text:` — an empty transcript is dropped"


def test_screening_is_skipped_for_empty_text():
    """Empty is the silence path's business, not the filter's."""
    verdict = st.screen_transcription("", expected_language="en", vad_closed=True)
    assert not verdict.filtered


# ---------------------------------------------------------------------------
# The counting test — the sharpest available probe of this defect
# ---------------------------------------------------------------------------
#
# Ordinary prose hides both compression and fabrication, because a plausible
# sentence reads as correct. A monotonic count does not: a missing, repeated or
# out-of-order number is unambiguous evidence, and needs no judgement to spot.
# Captured verbatim from the operator's live session on 2026-09-01, where he
# counted continuously past 18.

# Both events were written with the IDENTICAL window 730.83-735.83.
COUNTING_TURNS = [
    (
        730.83,
        735.83,
        "All right, I'm going to do a test where I start counting from 1, 2, 3, "
        "4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, and finally 17.",
    ),
    (730.83, 735.83, "Eighteen"),
]


def _spoken_numbers(text):
    """The integers appearing in a counting utterance, in order."""
    words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
        "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
        "sixteen": 16, "seventeen": 17, "eighteen": 18,
    }
    found = []
    for token in re.findall(r"[A-Za-z]+|\d+", text):
        if token.isdigit():
            found.append(int(token))
        elif token.lower() in words:
            found.append(words[token.lower()])
    return found


def test_the_counting_test_exposes_two_events_sharing_one_window():
    """Two distinct events written with the identical window.

    This one stands on its own evidence, independent of anything about the
    text: timestamps derived from arrival time collide whenever two events
    arrive in the same instant. Timestamps that do not advance are an anomaly
    whether or not the transcript they label is accurate — and here it was.
    """
    windows = [(s, e) for s, e, _ in COUNTING_TURNS]
    assert windows[0] == windows[1] == (730.83, 735.83)
    assert len(set(windows)) == 1, "two distinct utterances, one window"


def test_the_counting_transcript_was_accurate_not_fabricated():
    """A correction, kept as a test so the mistake is not repeated.

    This turn was initially read as evidence that the streaming path invented a
    plausible ending: the count appears to end "...16, 17, and finally 17",
    with 17 repeated where 18 should be. That looked like the confident-wrong
    failure in its most dangerous form.

    It was not. The operator confirmed he really did say seventeen twice, and
    the following turn on a fresh capture records him saying so:
    "Rather, before I said seventeen twice and I said finally". The transcript
    was CORRECT; the expectation was wrong.

    The methodological point this pins: a counting fixture is only ground truth
    when the input is SCRIPTED. A human counting from memory can repeat a
    number, and that repetition is indistinguishable from a transcription
    defect. Comparing a transcript against an assumption of what it should
    contain, and concluding the transcript is wrong, is exactly the error this
    test exists to prevent.
    """
    numbers = _spoken_numbers(COUNTING_TURNS[0][2])
    assert numbers[-1] == 17 and numbers[-2] == 17
    # Asserted as ACCURATE TRANSCRIPTION of a real repetition, not as a defect.


def test_a_counting_fixture_needs_a_scripted_input_to_be_ground_truth():
    """Only a known input makes a repeat diagnostic rather than ambiguous."""
    scripted = [1, 2, 3, 4, 5]
    assert all(b > a for a, b in zip(scripted, scripted[1:]))
    # A repeat is evidence ONLY against a scripted count; against a human
    # counting from memory it is equally well the human repeating himself.
    human_from_memory = _spoken_numbers("one two three three four")
    assert not all(b > a for a, b in zip(human_from_memory, human_from_memory[1:]))


def test_vad_boundaries_separate_the_two_counting_events():
    """The fix, on the real case: distinct, non-overlapping, uncompressed.

    Under wall-clock labelling both events got (730.83, 735.83). Under VAD
    boundaries the long utterance gets a span that can actually hold it, and
    "Eighteen" becomes a separate turn after it.
    """
    boundaries = st.TurnBoundaries()
    long_start, long_end, _ = _turn(boundaries, 712400, 734900, 735.8)
    next_start, next_end, _ = _turn(boundaries, 735100, 736000, 736.1)

    assert long_end - long_start > 5.0, "a 17-number count needs more than 5s"
    assert next_start >= long_end, "the two turns must not overlap"
    assert (long_start, long_end) != (next_start, next_end), "no shared window"


def test_a_correct_count_is_strictly_increasing():
    """Guards the fixture helper itself, so the tests above mean what they say."""
    assert _spoken_numbers("1, 2, 3, and then Eighteen") == [1, 2, 3, 18]
    numbers = _spoken_numbers("one two three four")
    assert all(b > a for a, b in zip(numbers, numbers[1:]))


# ---------------------------------------------------------------------------
# Timestamp monotonicity — makes an intermittent stamping fault visible
# ---------------------------------------------------------------------------


def test_the_duplicate_window_from_the_live_capture_is_reported():
    """The real 730.83-735.83 collision must not pass silently."""
    first_start, first_end, _ = COUNTING_TURNS[0]
    second_start, second_end, _ = COUNTING_TURNS[1]
    assert st.timestamp_anomaly(first_start, first_end, None) is None
    anomaly = st.timestamp_anomaly(second_start, second_end, first_end)
    assert anomaly is not None, "an identical repeated window is an anomaly"
    assert "out of order" in anomaly


def test_ordinary_advancing_turns_raise_nothing():
    assert st.timestamp_anomaly(196.36, 197.90, 193.10) is None
    assert st.timestamp_anomaly(199.27, 201.80, 197.90) is None


def test_a_turn_abutting_the_previous_one_is_fine():
    """Back-to-back speech is normal; only going BACKWARDS is not."""
    assert st.timestamp_anomaly(200.0, 202.5, 200.0) is None


def test_a_zero_width_turn_is_reported():
    """The operator's 91.78-91.78 turn. No speech spans zero time."""
    assert "zero duration" in st.timestamp_anomaly(200.0, 200.0, 200.0)
    assert "zero duration" in st.timestamp_anomaly(91.78, 91.78, None)


def test_an_implausibly_long_turn_is_reported():
    """The operator's 58.26-89.62 turn: 31.36s is not one VAD-closed utterance."""
    anomaly = st.timestamp_anomaly(58.26, 89.62, 35.49)
    assert anomaly is not None and "longer than" in anomaly


def test_the_vad_latency_overlap_is_tolerated():
    """0.23s measured on a real capture, with both spans otherwise correct."""
    assert st.timestamp_anomaly(30.55, 32.61, 30.78) is None


def test_a_substantial_overlap_is_still_reported():
    assert "out of order" in st.timestamp_anomaly(191.0, 193.0, 196.39)


def test_a_turn_ending_before_it_starts_is_reported():
    assert "ends before it starts" in st.timestamp_anomaly(205.0, 203.0, 200.0)


def test_replaying_the_fragmented_capture_flags_every_overlap():
    """All eight of the operator's captured turns overlapped their predecessor."""
    flagged = 0
    previous_end = None
    for start_s, end_s, _ in OPERATOR_FRAGMENTED_TURNS:
        if st.timestamp_anomaly(start_s, end_s, previous_end):
            flagged += 1
        previous_end = end_s
    assert flagged == len(OPERATOR_FRAGMENTED_TURNS) - 1, (
        "every turn after the first overlapped, and each must be reported"
    )


def test_vad_boundaries_raise_no_anomalies():
    """The fix must not trip its own alarm."""
    boundaries = st.TurnBoundaries()
    previous_end = None
    for a, b in [(191390, 193100), (196360, 197900), (199270, 200100)]:
        start_s, end_s, _ = _turn(boundaries, a, b, b / 1000.0 + 1.0)
        assert st.timestamp_anomaly(start_s, end_s, previous_end) is None
        previous_end = end_s


@pytest.mark.parametrize("text", ["Soita.", "Utanfor."])
def test_bare_latin_single_word_fabrications_are_NOT_catchable_on_output(text):
    """An honest limit, asserted rather than glossed over.

    Both are real fabrications from the operator's session, and the output
    filter cannot catch them: ordinary Latin letters, correctly spelled, in a
    plausible register. The one signal that did catch them also ate real
    single-word speech ("Eighteen", "root", "system") at a 5:2
    false-positive-to-true-positive ratio, so it was removed rather than tuned.

    This is exactly the argument for gating the INPUT. A decoder handed silence
    or noise always emits something, and no amount of output inspection
    separates a one-word fabrication from a one-word utterance. Stopping the
    noise reaching the model is the only defence that works on this class.
    """
    verdict = st.screen_transcription(text, expected_language="en", vad_closed=True)
    assert not verdict.filtered


# ---------------------------------------------------------------------------
# Interleaved VAD events — the defect behind BOTH live corruptions
# ---------------------------------------------------------------------------
#
# Captured verbatim from the operator's socket via --raw-event-log. The next
# utterance's speech_started arrives BEFORE the previous utterance's completed,
# because transcription is asynchronous.

INTERLEAVED_EVENTS = [
    ("speech_started", 27540, None, None),
    ("speech_stopped", None, 30784, None),
    ("speech_started", 30548, None, None),   # turn 2 opens before turn 1 completes
    ("completed", None, None, "Have we finished all of the performance work we wanted to perform?"),
    ("speech_stopped", None, 32608, None),
    ("completed", None, None, "Ort a fhágfaidh mé tama?"),
]


def _replay(events, streamed_s=51.31):
    boundaries = st.TurnBoundaries()
    emitted = []
    for kind, start_ms, end_ms, text in events:
        if kind == "completed":
            emitted.append(boundaries.claim(streamed_s) + (text,))
            continue
        boundaries.observe(
            {
                "type": f"input_audio_buffer.{kind}",
                "audio_start_ms": start_ms,
                "audio_end_ms": end_ms,
            }
        )
    return emitted


def test_interleaved_events_give_each_turn_its_own_boundaries():
    """The regression test for the operator's session.

    A single mutable slot handed turn 1 the boundaries of turn 2, producing an
    18.91s span for 3.24s of speech and a 0.00s span for turn 2. Queueing the
    spans and claiming the oldest gives each turn what is actually its own.
    """
    emitted = _replay(INTERLEAVED_EVENTS)
    (first_start, first_end, _, first_text), (second_start, second_end, _, _) = emitted

    assert (round(first_start, 2), round(first_end, 2)) == (27.54, 30.78)
    assert round(first_end - first_start, 2) == 3.24, "not 18.91s"
    assert "performance work" in first_text, "the TEXT was always complete"

    assert (round(second_start, 2), round(second_end, 2)) == (30.55, 32.61)
    assert second_end > second_start, "not a zero-length window"


def test_no_emitted_turn_is_degenerate_after_the_fix():
    previous_end = None
    for start_s, end_s, _, _ in _replay(INTERLEAVED_EVENTS):
        assert st.timestamp_anomaly(start_s, end_s, previous_end) is None
        previous_end = end_s


def test_turns_are_claimed_oldest_first():
    """The API completes turns in the order it opened them."""
    boundaries = st.TurnBoundaries()
    for start_ms, end_ms in [(1000, 2000), (3000, 4000), (5000, 6000)]:
        boundaries.observe(
            {"type": "input_audio_buffer.speech_started", "audio_start_ms": start_ms}
        )
        boundaries.observe(
            {"type": "input_audio_buffer.speech_stopped", "audio_end_ms": end_ms}
        )
    assert boundaries.pending == 3
    assert [boundaries.claim(9.0)[:2] for _ in range(3)] == [
        (1.0, 2.0), (3.0, 4.0), (5.0, 6.0),
    ]
    assert boundaries.pending == 0


def test_a_stop_closes_the_most_recent_open_span():
    boundaries = st.TurnBoundaries()
    boundaries.observe(
        {"type": "input_audio_buffer.speech_started", "audio_start_ms": 1000}
    )
    boundaries.observe(
        {"type": "input_audio_buffer.speech_started", "audio_start_ms": 3000}
    )
    boundaries.observe(
        {"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 4000}
    )
    # The stop belongs to the span opened at 3.0s, leaving the first still open.
    assert boundaries.claim(9.0)[:2] == (1.0, 9.0)
    assert boundaries.claim(9.0)[:2] == (3.0, 4.0)


def test_a_completed_with_no_pending_span_falls_back_safely():
    boundaries = st.TurnBoundaries()
    start_s, end_s, vad_closed = boundaries.claim(42.0)
    assert (start_s, end_s) == (42.0, 42.0) and vad_closed is False


# ---------------------------------------------------------------------------
# The optional VAD dependency must degrade LOUDLY, not silently
# ---------------------------------------------------------------------------


def test_a_missing_local_vad_still_returns_none_rather_than_raising():
    """Degrading to RMS alone is deliberate: the gate still works without VAD.

    Sending too much audio costs money; failing closed loses the operator's
    words. So a missing optional package must never take the stream down.
    """
    assert st.load_speech_detector(enabled=False) is None


def test_the_degraded_state_reaches_the_operator_visible_channel():
    """A stderr log is a write-only channel (ateles#583).

    The operator watches the JSONL through the session Monitor, not the
    daemon's stderr. When the VAD is missing the transcript is measurably more
    fabrication-prone, so that fact has to arrive where the operator is
    actually looking.
    """
    record = st.degraded_vad_record(0)
    assert record["ok"] is False
    assert record["source"] == "stream"
    # Not fatal — the stream continues on RMS alone.
    assert record["fatal"] is False
    assert "webrtcvad" in record["error"]


def test_the_degradation_notice_names_the_consequence_not_just_the_cause():
    """"VAD unavailable" tells an operator nothing actionable on its own."""
    error = st.degraded_vad_record(0)["error"].lower()
    assert "rms" in error
    assert "fabricat" in error


def test_the_vad_is_consulted_only_on_audio_that_already_passed_rms():
    """The VAD rejects loud non-speech; it must not second-guess silence.

    Consulting it below the RMS threshold would let it VETO the hangover that
    keeps word endings and gives server VAD its turn boundary.
    """

    class _NeverSpeech:
        name = "never"

        def __init__(self):
            self.calls = 0

        def is_speech(self, payload):
            self.calls += 1
            return False

    detector = _NeverSpeech()
    gate = st.InputGate(vad=detector)
    # Silence: below threshold, so the VAD is never asked.
    for i in range(10):
        gate.should_send(_pcm(0), i * 0.1)
    assert detector.calls == 0


def test_webrtcvad_is_declared_optional_in_the_manifest():
    """A venv rebuild must not lose it silently (it was in no manifest).

    Optional is the correct group: `load_speech_detector` degrades to RMS
    rather than failing, so this must never become a hard runtime dependency.
    """
    manifest = (
        Path(__file__).resolve().parents[2] / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert "webrtcvad" in manifest
    optional = manifest.split("[project.optional-dependencies]", 1)[1]
    assert "webrtcvad" in optional.split("[project.scripts]", 1)[0]
    # And NOT among the core install_requires.
    core = manifest.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "webrtcvad" not in core
