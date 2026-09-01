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
import pathlib
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


@pytest.fixture(autouse=True)
def _stub_interpreter_resolution(request, monkeypatch):
    """Give every main() test a usable interpreter.

    main() now REFUSES TO START unless an interpreter passes an import
    preflight (issue #619). Tests that exercise the loop must not depend on
    whether the machine running them happens to have execution/venv, so the
    resolver is stubbed here.

    The ``test_resolver_*`` tests are ABOUT resolution and opt out by name —
    otherwise this fixture would stub out the very behaviour they assert, and
    they would pass against a codebase with no resolver at all.
    """
    if request.node.name.startswith("test_resolver_"):
        return
    monkeypatch.setattr(
        lt, "resolve_transcriber_python",
        lambda env: (pathlib.Path("/usr/bin/python3"), []),
    )


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
        patch.object(lt, "transcribe_slice", return_value=(True, "final words", "en")),
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


def test_main_kill_switch_fires_on_three_consecutive_failures(tmp_path, monkeypatch):
    """Three failed chunks stop the run, write a fatal event, and exit non-zero.

    The old threshold was 5 and the exit code was 0, so a supervisor could not
    tell a broken run from a finished one. Issue #619: every chunk failed with
    `ModuleNotFoundError: config` across four restarts while the tailer
    reported healthy.
    """
    recording = tmp_path / "meet_mic.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    cursor_durations = [10.0 + i * 10.0 for i in range(6)]

    with (
        patch.object(lt, "probe_duration", side_effect=cursor_durations),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "transcribe_slice", return_value=(False, "boom", None)),
        patch.object(lt, "log") as mock_log,
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 1, "a run that transcribed nothing must not exit 0"

    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    chunks = [r for r in lines if "chunk" in r]
    assert len(chunks) == 3, "must stop at 3, not run on forever"
    assert all(r["ok"] is False and "error" in r for r in chunks)

    fatal = [r for r in lines if r.get("event") == "fatal_transcription_failures"]
    assert len(fatal) == 1, "the JSONL must carry a machine-readable fatal marker"
    assert fatal[0]["ok"] is False
    assert fatal[0]["consecutive_failures"] == 3
    assert fatal[0]["last_error"] == "boom"
    assert "t" in fatal[0]

    assert any(
        "consecutive transcription failures" in str(c)
        for c in mock_log.call_args_list
    )


def test_max_consecutive_failures_is_configurable(tmp_path, monkeypatch):
    """--max-consecutive-failures moves the boundary; default stays 3."""
    recording = tmp_path / "meet_mic.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")

    with (
        patch.object(lt, "probe_duration", side_effect=[10.0 * (i + 1) for i in range(6)]),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "transcribe_slice", return_value=(False, "boom", None)),
        patch.object(lt, "log"),
    ):
        rc = lt.main([
            "--file", str(recording), "--out", str(out),
            "--interval", "1", "--start-at", "0",
            "--max-consecutive-failures", "2",
        ])

    assert rc == 1
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    assert len([r for r in lines if "chunk" in r]) == 2
    assert [r for r in lines if r.get("event") == "fatal_transcription_failures"]


def test_main_silence_leaves_streak_unchanged_mid_run(tmp_path, monkeypatch):
    """failure, failure, silence, failure — silence neither resets nor counts.

    The fatal fires on the third ACTUAL failure, with the silence chunk sitting
    between failures two and three. Silence is an ordinary meeting state; a
    tailer that stopped because someone paused would be useless.
    """
    recording = tmp_path / "meet_mic.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    results = [
        (False, "e1", None),
        (False, "e2", None),
        (False, lt.SILENCE_SENTINEL, None),
        (False, "e3", None),
    ]
    durations = [10.0 * (i + 1) for i in range(5)] + [None]

    with (
        patch.object(lt, "probe_duration", side_effect=durations),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "transcribe_slice", side_effect=results),
        patch.object(lt, "log"),
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 1, "the third real failure is still fatal"
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    chunks = [r for r in lines if "chunk" in r]
    assert len(chunks) == 4, "the silence chunk is written, not swallowed"

    silence = chunks[2]
    assert silence["ok"] is True
    assert silence["text"] == ""
    assert silence["silence"] is True
    assert "error" not in silence, "silence must be visibly distinct from failure"

    fatal = [r for r in lines if r.get("event") == "fatal_transcription_failures"]
    assert fatal and fatal[0]["consecutive_failures"] == 3


def test_success_after_failures_clears_streak_before_kill(tmp_path, monkeypatch):
    """failure, failure, success, failure, failure — no fatal before a third post-reset failure."""
    recording = tmp_path / "meet_mic.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    results = [
        (False, "e1", None),
        (False, "e2", None),
        (True, "some real spoken text about the swarm", "en"),
        (False, "e3", None),
        (False, "e4", None),
    ]
    durations = [10.0 * (i + 1) for i in range(5)] + [None]

    with (
        patch.object(lt, "probe_duration", side_effect=durations),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "transcribe_slice", side_effect=results),
        patch.object(lt, "log"),
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 0, "the success reset the streak; this is not a fatal run"
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    assert lines[2]["ok"] is True
    assert lines[2]["text"] == "some real spoken text about the swarm"
    assert "filtered" not in lines[2], "genuine speech must not be filtered"
    assert not [r for r in lines if r.get("event") == "fatal_transcription_failures"]


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
    assert any("is growing" in str(c) for c in mock_log.call_args_list)


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
        ok, payload, lang = lt.transcribe_slice(
            wav, env={"PATH": "/usr/bin"}, python_bin=pathlib.Path("/usr/bin/python3")
        )

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
        'NEOTOMA_BEARER_TOKEN="neotoma-secret"',
        "NEOTOMA_MNEMONIC='word word word'",
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
# Case 9 — issue #619: the silent failure modes
# --------------------------------------------------------------------------


def test_auto_detect_prefers_mic_when_both_tracks_grow(tmp_path):
    """Simultaneous mic + system tracks must select mic.

    Audio Hijack writes both at once. The old code sorted by mtime and took the
    newest, so on 2026-09-01 it selected `system` — the computer's OUTPUT — and
    would have transcribed the agent's own speech back as the operator's. That
    failure produces plausible chunks, so nothing about the output reveals it.

    The system track is given the NEWER mtime here, so passing this test
    requires the priority rule and not an accident of ordering.
    """
    import os

    mic = tmp_path / "20260901 1026 mic.mp4"
    system = tmp_path / "20260901 1026 system.mp4"
    mic.write_bytes(b"aa")
    system.write_bytes(b"aa")
    os.utime(mic, (1000.0, 1000.0))
    os.utime(system, (2000.0, 2000.0))  # system looks newer

    def grow(_seconds=0):
        mic.write_bytes(b"aa" + b"x" * 40)
        system.write_bytes(b"aa" + b"x" * 40)

    with patch.object(lt.time, "sleep", side_effect=grow):
        got = lt.find_growing_recording(tmp_path, settle_probe=0.0)

    assert got == mic, f"selected the wrong track: {got}"


def test_auto_detect_falls_back_to_system_when_only_system_grows(tmp_path):
    """Preference, not exclusion — a system-only recording is still tailable.

    A REGRESSION pin, not new behaviour: this passes before the fix too. It
    exists so the mic preference cannot be implemented by simply refusing to
    tail a system track, which would break every remote-only recording.
    """
    system = tmp_path / "20260901 1026 system.mp4"
    system.write_bytes(b"aa")

    def grow(_seconds=0):
        system.write_bytes(b"aa" + b"x" * 40)

    with patch.object(lt.time, "sleep", side_effect=grow):
        got = lt.find_growing_recording(tmp_path, settle_probe=0.0)

    assert got == system


def test_system_track_selection_warns_loudly(tmp_path):
    """A system/remote selection must be unmistakable on stderr."""
    system = tmp_path / "20260901 1026 system.mp4"
    system.write_bytes(b"x")

    with patch.object(lt, "log") as mock_log:
        lt.warn_if_not_mic(system)

    logged = " ".join(str(c) for c in mock_log.call_args_list)
    assert "WARNING" in logged
    assert "SYSTEM/REMOTE TRACK" in logged
    assert str(system) in logged


def test_mic_track_selection_does_not_warn(tmp_path):
    mic = tmp_path / "20260901 1026 mic.mp4"
    mic.write_bytes(b"x")

    with patch.object(lt, "log") as mock_log:
        lt.warn_if_not_mic(mic)

    logged = " ".join(str(c) for c in mock_log.call_args_list)
    assert "WARNING" not in logged
    assert "mic" in logged


def test_unusable_interpreter_refuses_to_start_before_writing_any_chunk(
    tmp_path, monkeypatch
):
    """No usable interpreter → non-zero exit, no JSONL, no chunks.

    The old code fell back to sys.executable when execution/venv was absent —
    which is the normal state of a git worktree. That interpreter exists and
    runs, but cannot import `config`, so EVERY chunk failed while the tailer
    reported healthy for four restarts. Refusing to start is the only honest
    behaviour.
    """
    recording = tmp_path / "meet_mic.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    # Undo the autouse stub: this test is ABOUT the real resolver.
    monkeypatch.setattr(
        lt, "resolve_transcriber_python",
        lambda env: (None, ["/nope/python: does not exist"]),
    )

    with (
        patch.object(lt.time, "sleep"),
        patch.object(lt, "transcribe_slice") as mock_tx,
        patch.object(lt, "log") as mock_log,
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1"]
        )

    assert rc != 0, "must refuse to start, not degrade"
    assert not out.exists(), "no JSONL may be written before the preflight passes"
    mock_tx.assert_not_called()

    logged = " ".join(str(c) for c in mock_log.call_args_list)
    assert "refusing to start" in logged.lower()
    assert "/nope/python" in logged, "must name each candidate it tried"


def test_preflight_rejects_an_interpreter_that_cannot_import(tmp_path):
    """Existence is not usability — the probe must actually import."""
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\nexit 0\n")

    failing = MagicMock(returncode=1, stderr="ModuleNotFoundError: No module named 'config'")
    with patch.object(lt.subprocess, "run", return_value=failing):
        problem = lt.preflight_interpreter(fake, env={})

    assert problem is not None
    assert "config" in problem


def test_preflight_accepts_an_interpreter_that_imports_cleanly(tmp_path):
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\nexit 0\n")

    ok = MagicMock(returncode=0, stderr="", stdout="")
    with patch.object(lt.subprocess, "run", return_value=ok):
        assert lt.preflight_interpreter(fake, env={}) is None


def test_preflight_rejects_a_missing_interpreter(tmp_path):
    assert lt.preflight_interpreter(tmp_path / "absent", env={}) == "does not exist"


def test_resolver_prefers_the_explicit_override_then_venv_then_dotvenv(monkeypatch):
    """LIVE_TRANSCRIPT_PYTHON wins, but only after it passes preflight."""
    seen: list[Path] = []

    def fake_preflight(candidate, env):
        seen.append(candidate)
        return None if str(candidate) == "/opt/good/python" else "no good"

    monkeypatch.setattr(lt, "preflight_interpreter", fake_preflight)

    chosen, attempts = lt.resolve_transcriber_python(
        {"LIVE_TRANSCRIPT_PYTHON": "/opt/good/python"}
    )
    assert str(chosen) == "/opt/good/python"
    assert seen[0] == Path("/opt/good/python"), "the override must be tried first"
    assert attempts == []


def test_resolver_rejects_a_bad_override_and_falls_through(monkeypatch):
    """A bad override is not honoured — that would be the same silent degradation."""
    def fake_preflight(candidate, env):
        return None if candidate == lt.DOTVENV_PYTHON else "unusable"

    monkeypatch.setattr(lt, "preflight_interpreter", fake_preflight)

    chosen, attempts = lt.resolve_transcriber_python(
        {"LIVE_TRANSCRIPT_PYTHON": "/opt/bad/python"}
    )
    assert chosen == lt.DOTVENV_PYTHON
    assert any("/opt/bad/python" in a for a in attempts)


def test_resolver_never_falls_back_to_sys_executable(monkeypatch):
    """The fallback that caused the outage must not exist any more."""
    monkeypatch.setattr(lt, "preflight_interpreter", lambda c, env: "unusable")

    chosen, attempts = lt.resolve_transcriber_python({})
    assert chosen is None, "a run with no usable interpreter must not be started"
    assert attempts, "it must report what it tried"


def test_missing_openai_key_fails_at_startup_not_per_chunk(tmp_path, monkeypatch):
    """A missing key is a startup failure, not N identical per-chunk errors.

    A REGRESSION pin: the startup check already existed and this passes before
    the fix. It is kept because the issue asks for the guarantee explicitly, and
    the new preflight runs alongside it — neither may swallow the other.
    """
    recording = tmp_path / "meet_mic.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(lt.Path, "home", staticmethod(lambda: tmp_path / "nohome"))

    with (
        patch.object(lt.time, "sleep"),
        patch.object(lt, "transcribe_slice") as mock_tx,
        patch.object(lt, "log") as mock_log,
    ):
        rc = lt.main(["--file", str(recording), "--out", str(out), "--interval", "1"])

    assert rc != 0
    assert not out.exists()
    mock_tx.assert_not_called()
    assert any("OPENAI_API_KEY" in str(c) for c in mock_log.call_args_list)


def test_transcribe_slice_requests_and_parses_the_detected_language(tmp_path):
    """The language signal needs --emit-language, and the marker must not leak."""
    wav = tmp_path / "slice.wav"
    wav.write_bytes(b"x")
    stdout = (
        "Transcribing audio file: /tmp/x.wav\n"
        "hello world\n"
        f"{lt.LANGUAGE_MARKER}ka\n"
    )
    proc = MagicMock(returncode=0, stderr="", stdout=stdout)

    with patch.object(lt.subprocess, "run", return_value=proc) as mock_run:
        ok, payload, lang = lt.transcribe_slice(
            wav, env={}, python_bin=Path("/usr/bin/python3")
        )

    assert ok is True
    assert payload == "hello world", "the marker must be stripped from the text"
    assert lang == "ka"
    assert "--emit-language" in mock_run.call_args.args[0]


def test_a_fabricated_chunk_is_marked_but_never_dropped(tmp_path, monkeypatch):
    """The whole point: a caught chunk keeps its text and gains a reason.

    Silently deleting it would reproduce the exact defect class of #619 — a
    component producing nothing usable while reporting success — and would make
    a false positive both invisible and unrecoverable.
    """
    recording = tmp_path / "meet_mic.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    georgian = "ლ ალ ნეტ კილე ლან ავალე ციოთკ ოოიანი ნსოყა აუვშღე არფოდი"
    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")

    with (
        patch.object(lt, "probe_duration", side_effect=[35.0, None]),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "measure_slice_rms_db", return_value=-31.6),
        patch.object(lt, "transcribe_slice", return_value=(True, georgian, None)),
        patch.object(lt, "log"),
    ):
        rc = lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    assert rc == 0, "a fabrication is not a transcription FAILURE"
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    chunk = lines[0]

    assert chunk["filtered"] == "script_mismatch"
    assert chunk["text"] == georgian, "the text must be preserved for review"
    assert chunk["ok"] is True
    # -31.6 dB is inside the operator's verified speech range: this chunk proves
    # the loudness gate could not have caught it.
    assert chunk["rms_db"] == -31.6


def test_genuine_speech_at_the_same_loudness_is_not_marked(tmp_path, monkeypatch):
    """The counterpart assertion — same dB, real words, untouched."""
    recording = tmp_path / "meet_mic.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    real = (
        "Review the session history in general to see if there are more bugs "
        "that you should be fixing directly, or via dispatch from the session."
    )
    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")

    with (
        patch.object(lt, "probe_duration", side_effect=[35.0, None]),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "measure_slice_rms_db", return_value=-30.5),
        patch.object(lt, "transcribe_slice", return_value=(True, real, "en")),
        patch.object(lt, "log"),
    ):
        lt.main(
            ["--file", str(recording), "--out", str(out), "--interval", "1", "--start-at", "0"]
        )

    chunk = json.loads(out.read_text().splitlines()[0])
    assert "filtered" not in chunk
    assert chunk["text"] == real
    assert chunk["language"] == "en"


def test_no_hallucination_filter_flag_disables_the_marking(tmp_path, monkeypatch):
    recording = tmp_path / "meet_mic.mp4"
    recording.write_bytes(b"x")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    ffmpeg_ok = MagicMock(returncode=0, stderr="", stdout="")
    with (
        patch.object(lt, "probe_duration", side_effect=[35.0, None]),
        patch.object(lt.time, "sleep"),
        patch.object(lt.subprocess, "run", return_value=ffmpeg_ok),
        patch.object(lt, "measure_slice_rms_db", return_value=-31.6),
        patch.object(lt, "transcribe_slice", return_value=(True, "ご視聴ありがとうございました", None)),
        patch.object(lt, "log"),
    ):
        lt.main([
            "--file", str(recording), "--out", str(out),
            "--interval", "1", "--start-at", "0", "--no-hallucination-filter",
        ])

    chunk = json.loads(out.read_text().splitlines()[0])
    assert "filtered" not in chunk
