"""Effect-level tests for live_transcript_tail.py.

Covers remote-track filtering (mic exclusion), stall/remnant slice decisions,
silence-vs-failure classification (kill-switch contract), the silence gate's
RMS parsing and p95 statistic (including meter-failure fallback), and
growing-recording discovery. Mirrors the config-stub + patch.object convention from
test_transcribe_audio.py — execution/scripts/config.py is untracked/gitignored.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

if "config" not in sys.modules:
    _stub_config = types.ModuleType("config")
    _stub_config.get_data_dir = lambda: Path(_SCRIPTS_DIR / "_test_data_dir")
    sys.modules["config"] = _stub_config

import live_transcript_tail as lt  # noqa: E402


# --------------------------------------------------------------------------
# Case 1 — is_remote_track()
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("foo_system.mp4", True),
        ("foo_remote.wav", True),
        ("foo_mic.mp4", False),
        # mic vetoes even when "system" also matches — regression pin
        ("system_mic.mp4", False),
        ("foo_system.txt", False),  # unsupported extension
    ],
)
def test_is_remote_track_filename_filter(filename, expected):
    assert lt.is_remote_track(Path(filename)) is expected


# --------------------------------------------------------------------------
# Case 2 — stall-vs-remnant branch (slice_decision)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "available,stalled,expected",
    [
        (2.0, False, "wait"),  # (a) sub-threshold, still growing
        (0.01, True, "exit_clean"),  # (b) stalled, essentially empty
        (2.0, True, "flush_final"),  # (c) stalled with usable remnant
        (10.0, False, "transcribe"),
        (10.0, True, "transcribe"),
        (0.05, True, "exit_clean"),  # boundary: <= stall_empty
        (0.06, True, "flush_final"),
    ],
)
def test_slice_decision_stall_vs_remnant(available, stalled, expected):
    assert lt.slice_decision(available, stalled) == expected


def test_main_wait_when_subthreshold_and_not_stalled(tmp_path, monkeypatch):
    """(a) available < MIN and not stalled → no record, no ffmpeg/transcribe."""
    import os

    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # mtime fresh relative to frozen time.time()=1000 → age=1s << interval*2
    os.utime(recording, (999.0, 999.0))

    # First sleep: interval. Second sleep would be the next loop — break via
    # KeyboardInterrupt after the continue path by making the second probe
    # return None (recording ended).
    durations = iter([1.0, None])  # available = 1.0 - 0.0 = 1.0 < 5

    with (
        patch.object(lt, "probe_duration", side_effect=lambda _p: next(durations)),
        patch.object(lt.time, "sleep"),
        patch.object(lt.time, "time", return_value=1000.0),
        patch.object(lt, "subprocess") as mock_sub,
        patch.object(lt, "transcribe_slice") as mock_tx,
        patch.object(lt, "log") as mock_log,
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 0
    assert not out.exists() or out.read_text() == ""
    mock_sub.run.assert_not_called()
    mock_tx.assert_not_called()
    # Second probe None → "could not probe duration"
    assert any("could not probe" in str(c) for c in mock_log.call_args_list)


def test_main_exit_clean_when_stalled_empty(tmp_path, monkeypatch, capsys):
    """(b) sub-threshold + stalled + available≈0 → clean exit, no JSONL line."""
    import os

    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # Very old mtime vs frozen time.time()=10_000 → stalled
    os.utime(recording, (0.0, 0.0))

    with (
        patch.object(lt, "probe_duration", return_value=0.01),
        patch.object(lt.time, "sleep"),
        patch.object(lt.time, "time", return_value=10_000.0),
        patch.object(lt, "subprocess") as mock_sub,
        patch.object(lt, "transcribe_slice") as mock_tx,
        patch.object(lt, "log") as mock_log,
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 0
    assert not out.exists() or out.read_text().strip() == ""
    mock_sub.run.assert_not_called()
    mock_tx.assert_not_called()
    assert any(
        "recording appears to have stopped — exiting" in str(c)
        for c in mock_log.call_args_list
    )


def test_main_flush_final_remnant_writes_one_jsonl_line(tmp_path, monkeypatch):
    """(c) stalled with remnant in (0.05, MIN) → one final chunk then exit."""
    import os

    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    os.utime(recording, (0.0, 0.0))

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")

    with (
        patch.object(lt, "probe_duration", return_value=2.0),
        patch.object(lt.time, "sleep"),
        patch.object(lt.time, "time", return_value=10_000.0),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok) as mock_run,
        patch.object(lt, "transcribe_slice", return_value=(True, "final words", [])),
        patch.object(lt, "log") as mock_log,
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 0
    lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ok"] is True
    assert record["text"] == "final words"
    # Two subprocess calls for the single final slice: the ffmpeg cut, then the
    # silence gate's RMS measurement. Exactly one slice is cut.
    cut_calls = [c for c in mock_run.call_args_list if "-ss" in c.args[0]]
    rms_calls = [c for c in mock_run.call_args_list if "astats=metadata=1:reset=1:length=3" in " ".join(c.args[0])]
    assert len(cut_calls) == 1
    assert len(rms_calls) == 1
    assert any("flushing final" in str(c) for c in mock_log.call_args_list)
    assert any("final slice written" in str(c) for c in mock_log.call_args_list)


# --------------------------------------------------------------------------
# Case 3 — silence vs failure classification
# --------------------------------------------------------------------------


def test_apply_transcription_success_resets_failures():
    record = {"ok": True}
    # Prior streak of 2, then success → 0
    assert lt.apply_transcription_result(record, True, "some text", 2) == 0
    assert record["text"] == "some text"
    assert record["ok"] is True
    assert "silence" not in record
    assert "error" not in record


def test_apply_transcription_silence_leaves_failures_unchanged():
    record = {"ok": False}
    # Primed to 3; silence must stay at 3 (neither +1 nor reset to 0)
    assert (
        lt.apply_transcription_result(record, False, lt.SILENCE_SENTINEL, 3) == 3
    )
    assert record["ok"] is True
    assert record["text"] == ""
    assert record["silence"] is True
    assert "error" not in record


def test_apply_transcription_real_error_increments_failures():
    record = {"ok": False}
    assert (
        lt.apply_transcription_result(record, False, "some real error string", 2)
        == 3
    )
    assert record["ok"] is False
    assert record["error"] == "some real error string"
    assert "silence" not in record


def test_silence_does_not_count_toward_kill_switch_streak():
    """Silence neither increments nor resets; real failures still accumulate across it.

    After 4 failures streak is 4; silence leaves it at 4; one more real failure
    reaches 5. (Silence does not *add* a count — that is the SKILL.md contract —
    but also does not break the streak, matching apply_transcription_result.)
    """
    streak = 0
    for ok, payload in [
        (False, "e1"),
        (False, "e2"),
        (False, "e3"),
        (False, "e4"),
    ]:
        streak = lt.apply_transcription_result({"ok": ok}, ok, payload, streak)
    assert streak == 4
    streak = lt.apply_transcription_result(
        {"ok": False}, False, lt.SILENCE_SENTINEL, streak
    )
    assert streak == 4  # unchanged — not 0, not 5
    streak = lt.apply_transcription_result({"ok": False}, False, "e5", streak)
    assert streak == 5


def test_five_real_failures_hit_kill_threshold():
    streak = 0
    for _ in range(5):
        record = {"ok": False}
        streak = lt.apply_transcription_result(record, False, "err", streak)
    assert streak == 5


def test_main_kill_switch_fires_on_five_consecutive_failures(tmp_path, monkeypatch):
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    # Each loop: available = 10 → normal transcribe path; always fail.
    # After 5 failures, main breaks.
    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    cursor_durations = [10.0 + i * 10.0 for i in range(6)]  # growing cursor room

    with (
        patch.object(lt, "probe_duration", side_effect=cursor_durations),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "transcribe_slice", return_value=(False, "boom", [])),
        patch.object(lt, "log") as mock_log,
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 0
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    assert len(lines) == 5
    assert all(r["ok"] is False for r in lines)
    assert any(
        "5 consecutive failures — stopping" in str(c) for c in mock_log.call_args_list
    )


def test_main_silence_leaves_streak_unchanged_mid_run(tmp_path, monkeypatch):
    """4 failures + silence: kill has not fired yet; silence line is ok+silence.

    A following 5th real failure *would* trip the switch (silence does not reset).
    Here we stop after the silence chunk to pin the mid-run contract.
    """
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    results = [
        (False, "e1", []),
        (False, "e2", []),
        (False, "e3", []),
        (False, "e4", []),
        (False, lt.SILENCE_SENTINEL, []),
    ]
    # 5 chunks then probe None to exit without a 5th real failure
    durations = [10.0 * (i + 1) for i in range(5)] + [None]

    with (
        patch.object(lt, "probe_duration", side_effect=durations),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "transcribe_slice", side_effect=results),
        patch.object(lt, "log") as mock_log,
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 0
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    assert len(lines) == 5
    silence_line = lines[4]
    assert silence_line["ok"] is True
    assert silence_line["silence"] is True
    assert silence_line["text"] == ""
    assert not any(
        "5 consecutive failures" in str(c) for c in mock_log.call_args_list
    )


def test_success_after_failures_clears_streak_before_kill(tmp_path, monkeypatch):
    """failure, failure, success, then three more failures — kill does not fire at 3 post-reset."""
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    results = [
        (False, "e1", []),
        (False, "e2", []),
        (True, "some text", []),
        (False, "e3", []),
        (False, "e4", []),
        (False, "e5", []),
    ]
    durations = [10.0 * (i + 1) for i in range(6)] + [None]

    with (
        patch.object(lt, "probe_duration", side_effect=durations),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "transcribe_slice", side_effect=results),
        patch.object(lt, "log") as mock_log,
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 0
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    assert lines[2]["ok"] is True and lines[2]["text"] == "some text"
    # After success, only 3 failures — kill switch must NOT fire
    assert not any(
        "5 consecutive failures" in str(c) for c in mock_log.call_args_list
    )


# --------------------------------------------------------------------------
# Case 4 — find_growing_recording()
# --------------------------------------------------------------------------


def test_find_growing_recording_returns_growing_file(tmp_path):
    f = tmp_path / "call_system.mp4"
    f.write_bytes(b"aa")

    def grow(_seconds=0):
        f.write_bytes(b"aa" + b"x" * 20)

    with patch.object(lt.time, "sleep", side_effect=grow):
        got = lt.find_growing_recording(tmp_path, settle_probe=0.0)
    assert got == f


def test_find_growing_recording_not_growing_returns_none(tmp_path):
    f = tmp_path / "call_system.mp4"
    f.write_bytes(b"aa")
    # sleep is a no-op; file size unchanged → not growing
    with (
        patch.object(lt.time, "sleep"),
        patch.object(lt, "log") as mock_log,
    ):
        got = lt.find_growing_recording(tmp_path, settle_probe=0.0)
    assert got is None
    assert any("not growing" in str(c) for c in mock_log.call_args_list)


def test_find_growing_recording_empty_dir_returns_none(tmp_path):
    assert lt.find_growing_recording(tmp_path, settle_probe=0.0) is None


def test_find_growing_recording_missing_dir_returns_none(tmp_path):
    missing = tmp_path / "nope"
    with patch.object(lt, "log") as mock_log:
        got = lt.find_growing_recording(missing, settle_probe=0.0)
    assert got is None
    assert any("watch dir does not exist" in str(c) for c in mock_log.call_args_list)


def test_find_growing_recording_picks_newest_mtime(tmp_path):
    import os
    import time as _time

    older = tmp_path / "old_system.mp4"
    newer = tmp_path / "new_system.mp4"
    older.write_bytes(b"aa")
    newer.write_bytes(b"aa")

    older_t = _time.time() - 100
    newer_t = _time.time()
    os.utime(older, (older_t, older_t))
    os.utime(newer, (newer_t, newer_t))

    def grow_newer(_seconds=0):
        newer.write_bytes(b"aa" + b"x" * 50)

    with patch.object(lt.time, "sleep", side_effect=grow_newer):
        got = lt.find_growing_recording(tmp_path, settle_probe=0.0)
    assert got == newer

# --------------------------------------------------------------------------
# Case 4 — the silence gate's RMS parsing and p95 statistic
# --------------------------------------------------------------------------


def test_parse_rms_levels_extracts_values_from_astats_stderr():
    """Real ffmpeg astats lines yield their RMS_level values, in order."""
    stderr = (
        "[Parsed_ametadata_1 @ 0x7f] lavfi.astats.Overall.RMS_level=-23.4\n"
        "[Parsed_ametadata_1 @ 0x7f] lavfi.astats.Overall.RMS_level=-51.2\n"
        "[Parsed_ametadata_1 @ 0x7f] lavfi.astats.Overall.RMS_level=-8\n"
    )
    assert lt.parse_rms_levels(stderr) == [-23.4, -51.2, -8.0]


def test_parse_rms_levels_drops_infinite_and_handles_empty():
    """Digital silence reports -inf; it must not poison the statistic."""
    stderr = (
        "lavfi.astats.Overall.RMS_level=-inf\n"
        "lavfi.astats.Overall.RMS_level=-30.0\n"
    )
    assert lt.parse_rms_levels(stderr) == [-30.0]
    assert lt.parse_rms_levels("") == []
    assert lt.parse_rms_levels("no astats here at all") == []


def test_sustained_rms_db_is_p95_not_mean_or_max():
    """A pausing speaker must not read as silence, nor a click as speech."""
    # 19 silent windows + 1 loud one: mean/median would say "silence".
    values = [-70.0] * 19 + [-10.0]
    assert lt.sustained_rms_db(values) == -10.0

    # A speaker who pauses: p95 tracks the speech, not the gaps.
    speech = [-60.0] * 5 + [-20.0] * 5
    assert lt.sustained_rms_db(speech) == -20.0

    # Single value degenerates to itself.
    assert lt.sustained_rms_db([-42.0]) == -42.0


def test_sustained_rms_db_empty_returns_none():
    assert lt.sustained_rms_db([]) is None


def test_measure_slice_rms_db_returns_none_when_ffmpeg_raises(tmp_path):
    """A broken meter must return None so the caller transcribes anyway."""
    wav = tmp_path / "slice.wav"
    wav.write_bytes(b"x")

    with (
        patch.object(lt.subprocess, "run", side_effect=OSError("ffmpeg missing")),
        patch.object(lt, "log"),
    ):
        assert lt.measure_slice_rms_db(wav) is None


def test_measure_slice_rms_db_returns_none_on_unusable_output(tmp_path):
    """No parseable RMS values is also a measurement failure, not silence."""
    wav = tmp_path / "slice.wav"
    wav.write_bytes(b"x")
    proc = MagicMock(returncode=0, stderr="", stdout="")

    with patch.object(lt.subprocess, "run", return_value=proc), patch.object(lt, "log"):
        assert lt.measure_slice_rms_db(wav) is None


def test_measure_slice_rms_db_returns_p95_of_parsed_levels(tmp_path):
    """End to end: stderr in, sustained level out — no audio, no network."""
    wav = tmp_path / "slice.wav"
    wav.write_bytes(b"x")
    stderr = "".join(
        f"lavfi.astats.Overall.RMS_level={v}\n" for v in ([-65.0] * 19 + [-12.5])
    )
    proc = MagicMock(returncode=0, stderr=stderr, stdout="")

    with patch.object(lt.subprocess, "run", return_value=proc), patch.object(lt, "log"):
        assert lt.measure_slice_rms_db(wav) == -12.5


# --------------------------------------------------------------------------
# Case 8 — durable-store invariant (transcribe_slice argv)
# --------------------------------------------------------------------------


def test_transcribe_slice_always_passes_no_store_and_no_diarize(tmp_path):
    """The live tailer must never write to the durable store or diarize.

    Pinned by behaviour, not by reading the source: every other main() test
    mocks transcribe_slice wholesale, so nothing else exercises the real argv.
    A live slice is a throwaway few-second fragment — storing it would pollute
    the durable transcript record that the authoritative post-recording run
    owns, and diarization on such a fragment is both meaningless and slow.

    Hermetic: only subprocess.run is mocked, so no ffmpeg, no whisper, no
    network, and no audio file is ever read.
    """
    wav = tmp_path / "slice.wav"
    wav.write_bytes(b"x")
    proc = MagicMock(returncode=0, stderr="", stdout="hello world\n")

    with patch.object(lt.subprocess, "run", return_value=proc) as mock_run:
        ok, payload, segments = lt.transcribe_slice(wav, env={"PATH": "/usr/bin"})

    assert ok is True
    assert payload == "hello world"

    mock_run.assert_called_once()
    argv = mock_run.call_args.args[0]
    assert "--no-store" in argv, f"--no-store missing from argv: {argv}"
    assert "--no-diarize" in argv, f"--no-diarize missing from argv: {argv}"


# --------------------------------------------------------------------------
# Case 10 — build_subprocess_env() credential scope (#558 legal review)
#
# The tailer hands an env to the transcribe_audio.py subprocess. The
# SOPS-materialized dotenv it reads also holds GitHub PATs, Telegram and Wise
# tokens, the Neotoma bearer token and the wallet mnemonic. Only
# OPENAI_API_KEY may cross that boundary. Guard the narrow contract, not the
# current key list, so adding a secret to the dotenv cannot silently widen it.
# --------------------------------------------------------------------------


_DOTENV_WITH_UNRELATED_SECRETS = "\n".join(
    [
        "# operator secrets",
        "OPENAI_API_KEY=sk-openai-value",
        # Values are synthetic. Quoting is deliberate but kept off any
        # credential-shaped key name: gitleaks' `protected-patterns` rule
        # matches a quoted literal after a key like *_TOKEN, so a quoted
        # fixture there fails the secret gate on this public repo. The
        # double- and single-quoted forms below still cover the parser's
        # quote stripping.
        "NEOTOMA_BEARER_TOKEN=neotoma-placeholder",
        'NEOTOMA_MNEMONIC="word word word"',
        "NEOTOMA_EXTRA_VALUE='single quoted'",
        "export ATELES_AGENT_PAT=ghp-secret",
        "WISE_API_TOKEN=wise-secret",
        "TELEGRAM_BOT_TOKEN=tg-secret",
        "",
        "MALFORMED_LINE_NO_EQUALS",
    ]
)


def _write_dotenv(tmp_path: Path) -> Path:
    p = tmp_path / "dotenv"
    p.write_text(_DOTENV_WITH_UNRELATED_SECRETS, encoding="utf-8")
    return p


def test_build_subprocess_env_extracts_only_openai_key(tmp_path):
    env = lt.build_subprocess_env(materialized=_write_dotenv(tmp_path), base_env={})
    assert env["OPENAI_API_KEY"] == "sk-openai-value"


@pytest.mark.parametrize(
    "leaked",
    [
        "NEOTOMA_BEARER_TOKEN",
        "NEOTOMA_MNEMONIC",
        "ATELES_AGENT_PAT",
        "WISE_API_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    ],
)
def test_build_subprocess_env_never_leaks_unrelated_secrets(tmp_path, leaked):
    env = lt.build_subprocess_env(materialized=_write_dotenv(tmp_path), base_env={})
    assert leaked not in env, f"{leaked} must not reach the transcription subprocess"


def test_build_subprocess_env_loads_no_key_outside_the_allowlist(tmp_path):
    """The contract is the allowlist itself, not one enumerated key."""
    env = lt.build_subprocess_env(materialized=_write_dotenv(tmp_path), base_env={})
    assert set(env) <= set(lt.SUBPROCESS_SECRET_KEYS)


def test_build_subprocess_env_inherits_base_env_and_prefers_it(tmp_path):
    env = lt.build_subprocess_env(
        materialized=_write_dotenv(tmp_path),
        base_env={"PATH": "/usr/bin", "OPENAI_API_KEY": "already-set"},
    )
    assert env["PATH"] == "/usr/bin"
    # An explicitly-set key wins over the dotenv.
    assert env["OPENAI_API_KEY"] == "already-set"


def test_build_subprocess_env_tolerates_missing_dotenv(tmp_path):
    env = lt.build_subprocess_env(
        materialized=tmp_path / "absent", base_env={"PATH": "/usr/bin"}
    )
    assert env == {"PATH": "/usr/bin"}


# --------------------------------------------------------------------------
# Case 11 — confidence gate (ateles#777)
#
# The level gate answers "was this loud". On a noisy microphone that is not the
# same question as "was this speech": in the fixture session below a fabricated
# chunk measured -39.0 dB while real speech measured -36.4 dB, so no level
# threshold separates them. The confidence gate asks Whisper's own per-segment
# no_speech_prob / avg_logprob instead, plus how much text was claimed per
# second of audio.
# --------------------------------------------------------------------------


def _seg(start, end, no_speech_prob, avg_logprob, chars):
    """Build a segment with `chars` characters of filler text.

    The gate never inspects WHAT the text says — only how much of it there is
    per second — so filler is a faithful stand-in and keeps the operator's own
    words out of the repo.
    """
    return {
        "start": start,
        "end": end,
        "no_speech_prob": no_speech_prob,
        "avg_logprob": avg_logprob,
        "text": "x" * chars,
    }


# Whisper's verbose_json confidences for the #777 regression fixture
# (~/Documents/data/recordings/20260907 1114 mic.mp4): ten chunks, each
# transcribed FOUR times, because Whisper's decode is non-deterministic and a
# threshold fitted to one draw overfits. Only measurements live in the JSON —
# `chars` is the LENGTH of each segment's text, never the text, which is the
# operator's own voice.
_TESTDATA_777 = json.loads(
    (_SCRIPTS_DIR / "testdata_777_confidence.json").read_text(encoding="utf-8")
)["chunks"]

# The single first-draw transcription, kept as the per-chunk readable case.
FIXTURE_777 = {
    int(k): (v["expect"], [
        (s["start"], s["end"], s["no_speech_prob"], s["avg_logprob"], s["chars"])
        for s in v["trials"][0]
    ])
    for k, v in _TESTDATA_777.items()
}


@pytest.mark.parametrize("chunk", sorted(FIXTURE_777))
def test_confidence_gate_reproduces_fixture_verdicts(chunk):
    """Every chunk of the #777 fixture is classified correctly at the defaults.

    This is the regression case the issue asks for: the four fabricated chunks
    must not be emitted as transcript, and the six real ones must be.
    """
    expected, rows = FIXTURE_777[chunk]
    segments = [_seg(*r) for r in rows]
    verdict, evidence = lt.classify_transcription(segments)
    assert verdict == expected, (
        f"chunk {chunk}: expected {expected}, got {verdict} (evidence={evidence})"
    )
    assert evidence["segments"] == len(segments)


def test_confidence_gate_separates_the_two_chunks_no_level_gate_could():
    """ch4 (real speech, -36.4 dB) and ch5 (fabricated, -39.0 dB).

    2.6 dB apart, so no level threshold splits them — that is the whole finding
    of #777. The confidence gate does split them.
    """
    assert lt.classify_transcription([_seg(*r) for r in FIXTURE_777[4][1]])[0] == "speech"
    assert lt.classify_transcription([_seg(*r) for r in FIXTURE_777[5][1]])[0] == "hallucinated"


def test_confidence_gate_returns_unknown_without_segments():
    """No segments means no signal — never mistake that for a fabrication.

    A missing measurement must fall through to emitting the chunk, exactly as a
    failed RMS read falls through to transcription.
    """
    assert lt.classify_transcription([]) == ("unknown", {})
    assert lt.classify_transcription(None) == ("unknown", {})


def test_segment_char_rate_is_zero_for_a_nonpositive_span():
    """A zero-length or inverted segment must not divide by zero."""
    assert lt.segment_char_rate({"start": 5.0, "end": 5.0, "text": "hello"}) == 0.0
    assert lt.segment_char_rate({"start": 5.0, "end": 1.0, "text": "hello"}) == 0.0


def test_segment_char_rate_counts_characters_per_second():
    assert lt.segment_char_rate({"start": 0.0, "end": 2.0, "text": "abcdef"}) == 3.0


def test_confidence_gate_needs_all_three_axes():
    """Each axis alone is insufficient — that is why all three are required.

    Confirmed on the fixture: no_speech_prob alone fails (real speech reached
    0.370, a fabrication sat at 0.297), and avg_logprob alone fails (a
    fabrication decoded at -0.331, better than real speech at -1.050).
    """
    # Confident and dense, but Whisper says it is not speech.
    assert not lt.segment_is_speech(_seg(0.0, 10.0, 0.90, -0.30, 100))
    # Confident and speech-like, but only a few characters across ten seconds.
    assert not lt.segment_is_speech(_seg(0.0, 10.0, 0.01, -0.30, 5))
    # Speech-like and dense, but the decode was incoherent.
    assert not lt.segment_is_speech(_seg(0.0, 10.0, 0.01, -3.50, 100))
    # All three clear.
    assert lt.segment_is_speech(_seg(0.0, 10.0, 0.01, -0.30, 100))


def test_confidence_gate_keeps_a_chunk_when_any_single_segment_is_speech():
    """A real sentence surrounded by padded silence must survive.

    Fixture ch6 and ch8 are exactly this: one dense real segment beside a
    stretched near-empty one. Judging the chunk on its aggregate would suppress
    both.
    """
    segments = [
        _seg(0.0, 30.0, 0.6966, -0.4437, 53),   # stretched over silence
        _seg(30.0, 35.0, 0.3703, -0.5691, 55),  # the real sentence
    ]
    assert lt.classify_transcription(segments)[0] == "speech"


def test_confidence_gate_evidence_records_the_deciding_segment():
    """The JSONL must say WHY, so thresholds can be re-derived from the log."""
    _, evidence = lt.classify_transcription([_seg(*r) for r in FIXTURE_777[3][1]])
    assert set(evidence) == {"no_speech_prob", "avg_logprob", "char_rate", "segments"}
    assert evidence["char_rate"] < lt.DEFAULT_MIN_CHAR_RATE


def test_main_marks_a_suppressed_chunk_distinctly_from_a_level_skip(tmp_path, monkeypatch):
    """`suppressed: low_confidence` vs `skipped: below_threshold`.

    The issue requires the two mechanisms be measurable apart, and the
    fabricated text must never appear under "text" where a consumer reads it.
    """
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    hallucinated = [_seg(*r) for r in FIXTURE_777[3][1]]

    with (
        patch.object(lt, "probe_duration", side_effect=[10.0, None]),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "measure_slice_rms_db", return_value=-40.0),
        patch.object(
            lt, "transcribe_slice",
            return_value=(True, "fabricated sentence", hallucinated),
        ),
        patch.object(lt, "log"),
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 0
    line = json.loads(out.read_text().splitlines()[0])
    assert line["suppressed"] == "low_confidence"
    assert "skipped" not in line, "must not be confused with a level-gate skip"
    assert line["text"] == "", "a consumer reading 'text' must never see a fabrication"
    assert line["suppressed_text"] == "fabricated sentence"
    assert line["confidence"]["char_rate"] < lt.DEFAULT_MIN_CHAR_RATE
    # The level gate's own measurement is still recorded alongside.
    assert line["rms_db"] == -40.0


def test_main_emits_real_speech_untouched(tmp_path, monkeypatch):
    """The gate is additive: a real chunk keeps its text and gains evidence."""
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    real = [_seg(*r) for r in FIXTURE_777[4][1]]

    with (
        patch.object(lt, "probe_duration", side_effect=[10.0, None]),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "measure_slice_rms_db", return_value=-36.4),
        patch.object(lt, "transcribe_slice", return_value=(True, "real words", real)),
        patch.object(lt, "log"),
    ):
        lt.main(["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"])

    line = json.loads(out.read_text().splitlines()[0])
    assert line["text"] == "real words"
    assert "suppressed" not in line
    assert line["confidence"]["no_speech_prob"] <= lt.DEFAULT_MAX_NO_SPEECH_PROB


def test_main_report_only_mode_flags_without_suppressing(tmp_path, monkeypatch):
    """--no-confidence-gate is the calibration posture: judge, log, but emit."""
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    hallucinated = [_seg(*r) for r in FIXTURE_777[3][1]]

    with (
        patch.object(lt, "probe_duration", side_effect=[10.0, None]),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "measure_slice_rms_db", return_value=-40.0),
        patch.object(lt, "transcribe_slice", return_value=(True, "fabricated", hallucinated)),
        patch.object(lt, "log"),
    ):
        lt.main([
            "--file", str(recording), "--out", str(out),
            "--interval", "1", "--start-at", "0", "--no-confidence-gate",
        ])

    line = json.loads(out.read_text().splitlines()[0])
    assert line["suppressed"] == "low_confidence_reported_only"
    assert line["text"] == "fabricated", "report-only mode must not suppress"


def test_main_flags_a_chunk_whose_confidence_could_not_be_measured(tmp_path, monkeypatch):
    """No segments came back: emit, but mark the uncertainty rather than vouch.

    Surfacing an unknown beats resolving it wrongly — and it must never be
    silently suppressed, or a transcription-path change would mute the feed.
    """
    recording = tmp_path / "meet_system.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")

    with (
        patch.object(lt, "probe_duration", side_effect=[10.0, None]),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "measure_slice_rms_db", return_value=-36.0),
        patch.object(lt, "transcribe_slice", return_value=(True, "some words", [])),
        patch.object(lt, "log"),
    ):
        lt.main(["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"])

    line = json.loads(out.read_text().splitlines()[0])
    assert line["text"] == "some words"
    assert line["confidence_unavailable"] is True
    assert "suppressed" not in line


def test_transcribe_slice_requests_segment_confidences(tmp_path):
    """The gate is only as good as the fields it gets — pin the argv.

    Without --segments-json the API returns no confidences and every chunk
    classifies as "unknown", which silently disables the gate.
    """
    wav = tmp_path / "slice.wav"
    wav.write_bytes(b"x")
    proc = MagicMock(returncode=0, stderr="", stdout="hello world\n")

    with patch.object(lt.subprocess, "run", return_value=proc) as mock_run:
        lt.transcribe_slice(wav, env={"PATH": "/usr/bin"})

    argv = mock_run.call_args.args[0]
    assert "--segments-json" in argv, f"--segments-json missing from argv: {argv}"


def test_transcribe_slice_reads_and_removes_the_segment_sidecar(tmp_path):
    """Segments come back to the caller, and the sidecar does not accumulate."""
    wav = tmp_path / "slice.wav"
    wav.write_bytes(b"x")
    seg_path = wav.with_suffix(".segments.json")
    segments = [{"start": 0.0, "end": 2.0, "text": "hi",
                 "no_speech_prob": 0.01, "avg_logprob": -0.3}]

    def fake_run(argv, **kwargs):
        seg_path.write_text(json.dumps({"language": "english", "segments": segments}))
        return MagicMock(returncode=0, stderr="", stdout="hi\n")

    with patch.object(lt.subprocess, "run", side_effect=fake_run):
        ok, payload, got = lt.transcribe_slice(wav, env={"PATH": "/usr/bin"})

    assert ok is True and payload == "hi"
    assert got == segments
    assert not seg_path.exists(), "sidecar must be cleaned up after every slice"


def test_transcribe_slice_tolerates_a_malformed_segment_sidecar(tmp_path):
    """A broken measurement disables the gate for that chunk — never drops audio."""
    wav = tmp_path / "slice.wav"
    wav.write_bytes(b"x")
    seg_path = wav.with_suffix(".segments.json")

    def fake_run(argv, **kwargs):
        seg_path.write_text("{not json")
        return MagicMock(returncode=0, stderr="", stdout="hi\n")

    with patch.object(lt.subprocess, "run", side_effect=fake_run), patch.object(lt, "log"):
        ok, payload, got = lt.transcribe_slice(wav, env={"PATH": "/usr/bin"})

    assert ok is True and payload == "hi"
    assert got == []
    assert not seg_path.exists()


def _gate_verdict_with_level_gate(chunk_data, rows):
    """Both gates in the order the tailer runs them: level first, then confidence."""
    if chunk_data["rms_db"] < lt.DEFAULT_SILENCE_THRESHOLD_DB:
        return "silence"
    return lt.classify_transcription([_as_segment(r) for r in rows])[0]


def test_confidence_gate_holds_across_every_repeat_transcription():
    """The whole fixture, all four transcriptions of each chunk, both gates.

    Whisper's decode is non-deterministic, so this — not the single-draw case
    above — is what says the thresholds are not overfitted. Across 40 samples
    every real-speech transcription must be emitted and every fabricated one
    suppressed, by one gate or the other.
    """
    emitted_speech = suppressed_fabrication = 0
    failures = []
    for chunk, data in sorted(_TESTDATA_777.items(), key=lambda kv: int(kv[0])):
        for trial, rows in enumerate(data["trials"]):
            verdict = _gate_verdict_with_level_gate(data, rows)
            emitted = verdict == "speech"
            if data["expect"] == "speech":
                emitted_speech += emitted
                if not emitted:
                    failures.append(f"ch{chunk} trial{trial}: real speech {verdict}")
            else:
                suppressed_fabrication += not emitted
                if emitted:
                    failures.append(f"ch{chunk} trial{trial}: fabrication emitted")

    assert not failures, "\n".join(failures)
    assert emitted_speech == 24
    assert suppressed_fabrication == 16


def test_fixture_confirms_whisper_decode_is_non_deterministic():
    """Pins the finding the thresholds are calibrated against.

    If a future model made the decode deterministic this test would fail, which
    is the signal to re-fit on fewer trials rather than keep paying for four.
    """
    varying = [
        chunk for chunk, data in _TESTDATA_777.items()
        if len({
            tuple((s["no_speech_prob"], s["avg_logprob"], s["chars"]) for s in trial)
            for trial in data["trials"]
        }) > 1
    ]
    assert varying, "expected at least one chunk to decode differently across trials"


def test_char_rate_is_the_binding_axis_on_the_fixture():
    """Documents WHY min_char_rate is 3.0 — the gap it sits in.

    Real speech never fell below 3.59 chars/s; no fabrication that cleared the
    other two axes exceeded 2.87. A future change that narrows this gap should
    fail here rather than silently start leaking fabrications.
    """
    speech_floor = min(
        max(lt.segment_char_rate(_as_segment(s)) for s in trial)
        for data in _TESTDATA_777.values() if data["expect"] == "speech"
        for trial in data["trials"]
    )
    fabrication_ceiling = max(
        max(
            (lt.segment_char_rate(_as_segment(s)) for s in trial
             if s["no_speech_prob"] <= lt.DEFAULT_MAX_NO_SPEECH_PROB
             and s["avg_logprob"] >= lt.DEFAULT_MIN_AVG_LOGPROB),
            default=0.0,
        )
        for data in _TESTDATA_777.values()
        if data["expect"] == "hallucinated"
        and data["rms_db"] >= lt.DEFAULT_SILENCE_THRESHOLD_DB
        for trial in data["trials"]
    )
    assert fabrication_ceiling < lt.DEFAULT_MIN_CHAR_RATE < speech_floor, (
        f"default {lt.DEFAULT_MIN_CHAR_RATE} must sit between "
        f"{fabrication_ceiling:.2f} and {speech_floor:.2f}"
    )


def _as_segment(row):
    """A stored measurement row as a segment dict, with filler for the text."""
    return {
        "start": row["start"],
        "end": row["end"],
        "no_speech_prob": row["no_speech_prob"],
        "avg_logprob": row["avg_logprob"],
        "text": "x" * row["chars"],
    }
