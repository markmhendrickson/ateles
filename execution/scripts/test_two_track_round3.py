"""Round 3: nothing disappears silently, cross-talk pairs are decided once, long calls
are measured on the real compressed files Tyto records, and children die with the parent.

Synthetic audio only: macOS ``say`` and ffmpeg tones, never a real recording.
"""
import array
import os
import random
import shutil
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path
from unittest.mock import patch

import chunked_whisper as cw
import local_whisper as lw
import pytest
import transcribe_audio as ta
import two_track_merge as m

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe missing")


def seg(start, end, text):
    return {"start": start, "end": end, "text": text}


def profile(spans, *, floor=-80.0, total=60.0):
    frames = [floor] * int(total / m.FRAME_SECONDS)
    for start, end, db in spans:
        for i in range(int(start / m.FRAME_SECONDS), int(end / m.FRAME_SECONDS)):
            frames[i] = db
    return m.EnergyProfile(frames)


def chunk(start, status, **extra):
    return {"kind": "chunk", "index": int(start // 120), "start": float(start), "end": float(start) + 120.0,
            "status": status, **extra}


# --- 3. cross-talk: a pair is decided once, narrowly --------------------------

def test_the_two_copies_of_one_utterance_are_never_both_dropped():
    # Mic 0-4 and System 1.5-5.5 say the same words; the louder side flips inside the window.
    energy = {m.SYSTEM: profile([(0, 2.7, -20), (2.7, 5.5, -40)]), m.MIC: profile([(0, 2.7, -40), (2.7, 4, -20)])}
    words = "we should ship the release on friday"
    tracks = {m.SYSTEM: [seg(1.5, 5.5, words)], m.MIC: [seg(0.0, 4.0, words)]}
    result = m.merge_tracks(tracks, energy=energy)
    assert result.stats["bleed_dropped"] <= 1
    assert result.text.count(words) >= 1 and result.text != m.NO_SPEECH_TEXT


def test_a_long_garbled_copy_is_dropped_once_and_the_clean_sentence_survives():
    # The reviewer's reproduction: round 2 dropped both the System sentence and the Mic copy.
    clean = "Please send the updated numbers before the end of the day so everyone can read them."
    energy = {m.SYSTEM: profile([(0, 11, -20)]), m.MIC: profile([(0, 11, -60), (12, 24, -20)])}
    tracks = {
        m.SYSTEM: [seg(0, 6.1, "We should move the meeting to Thursday afternoon because the release review is not finished yet."),
                   seg(7.1, 24.0, clean)],
        m.MIC: [seg(0, 12, "We should move the meeting to Thursday afternoon because the release review is not finished "
                           "Please send the updated numbers before the end of today so everyone can move then."),
                seg(12, 24, "I can commit to sending the numbers by Friday morning")],
    }
    result = m.merge_tracks(tracks, energy=energy)
    assert result.stats["bleed_dropped"] <= 1                  # never both copies
    assert "Please send the updated numbers before the end of the day" in result.text
    assert "I can commit to sending the numbers by Friday morning" in result.text


def test_every_removed_cross_talk_segment_is_marked_in_the_body_and_named_in_the_header():
    energy = {m.SYSTEM: profile([(0, 10, -20)]), m.MIC: profile([(0, 10, -60), (20, 40, -20)])}
    tracks = {m.SYSTEM: [seg(0, 10, "we should move the meeting to Thursday afternoon")],
              m.MIC: [seg(0.2, 10, "move the meeting to thursday afternoon")]}
    result = m.merge_tracks(tracks, energy=energy)
    header = result.text.splitlines()[0]
    assert "1 cross-talk segment removed (Mic 0:00–0:10)" in header
    assert "[0:00–0:10] [Mic]\n[CROSS-TALK REMOVED" in result.text
    assert "leaks not matched by text are kept" in header
    assert result.stats["bleed_segments"][0]["matched"]["track"] == m.SYSTEM


def test_only_the_overlapping_stretch_of_a_long_remote_segment_is_matched():
    # The operator says the remote's OPENING words, but over its closing seconds.
    remote = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"
    energy = {m.SYSTEM: profile([(0, 20, -20)]), m.MIC: profile([(15, 19, -50), (30, 50, -20)])}
    tracks = {m.SYSTEM: [seg(0, 20, remote)], m.MIC: [seg(15, 19, "alpha bravo charlie delta")]}
    assert m.merge_tracks(tracks, energy=energy).stats["bleed_dropped"] == 0


def test_function_words_alone_never_match():
    energy = {m.SYSTEM: profile([(0, 10, -20)]), m.MIC: profile([(2, 8, -50), (30, 50, -20)])}
    tracks = {m.SYSTEM: [seg(0, 10, "that is what the one and the all of them")], m.MIC: [seg(2, 8, "that is what the one")]}
    assert m.merge_tracks(tracks, energy=energy).stats["bleed_dropped"] == 0


def test_a_match_only_inside_the_timestamp_padding_is_still_a_leak():
    energy = {m.SYSTEM: profile([(0, 9, -20)]), m.MIC: profile([(9.2, 11, -60), (30, 50, -20)])}
    remote = "one two three four five six seven eight nine ten alpha bravo charlie delta echo"
    tracks = {m.SYSTEM: [seg(0, 9.0, remote)], m.MIC: [seg(9.2, 11, "charlie delta echo")]}
    assert m.merge_tracks(tracks, energy=energy).stats["bleed_dropped"] == 1


def test_a_segment_overlapping_less_than_half_is_not_a_leak():
    energy = {m.SYSTEM: profile([(9, 10, -20)]), m.MIC: profile([(0, 10, -60), (30, 50, -20)])}
    words = "alpha bravo charlie delta"
    tracks = {m.SYSTEM: [seg(9, 10, words)], m.MIC: [seg(0, 10, words)]}
    assert m.merge_tracks(tracks, energy=energy).stats["bleed_dropped"] == 0


# --- 4. nothing skipped is silent -----------------------------------------------

def test_an_audible_chunk_with_no_words_is_a_marker_and_a_header_span():
    chunks = {m.SYSTEM: [chunk(0, "ok"), chunk(120, "empty"), chunk(240, "ok")]}
    result = m.merge_tracks({m.SYSTEM: [seg(1, 3, "hello there")]}, chunks=chunks)
    assert "1 audible chunk with no words recognised (System 2:00–4:00)" in result.text.splitlines()[0]
    assert "[2:00–4:00] [System]\n[NO WORDS RECOGNISED" in result.text


def test_adjacent_silent_chunks_become_one_marker_and_the_header_names_track_and_span():
    chunks = {m.MIC: [chunk(0, "silent"), chunk(120, "silent"), chunk(240, "silent")]}
    result = m.merge_tracks({m.SYSTEM: [seg(1, 3, "hello there")], m.MIC: []}, chunks=chunks)
    assert "3 chunks skipped as silent (Mic 0:00–6:00)" in result.text.splitlines()[0]
    assert result.text.count("[SKIPPED AS SILENT") == 1 and "[0:00–6:00] [Mic]" in result.text


def test_the_header_names_each_chunk_that_still_loops():
    chunks = {m.SYSTEM: [chunk(0, "ok"), chunk(120, "ok", loop_after_rerun=True)],
              m.MIC: [chunk(240, "ok", loop_after_rerun=True)]}
    header = m.merge_tracks({m.SYSTEM: [seg(1, 3, "hello there")]}, chunks=chunks).text.splitlines()[0]
    assert "2 chunks still looping after a re-run (System 2:00–4:00, Mic 4:00–6:00)" in header


def test_a_bracket_in_a_reason_cannot_break_the_header_or_the_marker():
    chunks = {m.SYSTEM: [chunk(0, "screened: bad]reason", reason="bad]reason", detail="a ] b [ c", withheld_text="x")]}
    text = m.merge_tracks({m.SYSTEM: [], m.MIC: []}, chunks=chunks).text
    assert text.splitlines()[0].count("]") == 1
    assert "bad reason" in text and "a b c" in text


# --- 5. sentence-length loops -----------------------------------------------------

@pytest.mark.parametrize("words, reps", [(19, 6), (15, 6), (8, 3)])
def test_a_repeated_sentence_is_a_loop(words, reps):
    phrase = " ".join(f"word{i}x" for i in range(words))
    text, removed = m.collapse_repeats(" ".join([phrase] * reps))
    assert removed == (reps - 2) * words and text.endswith("[…]")
    assert text.count(phrase) == 2 and cw.has_repeat_loop(" ".join([phrase] * reps))


def test_a_sentence_said_twice_is_not_a_loop():
    phrase = " ".join(f"word{i}x" for i in range(12))
    assert m.collapse_repeats(phrase + " and then " + phrase)[1] == 0
    assert m.collapse_repeats(phrase + " " + phrase)[1] == 0


# --- 4. the frame gate and the empty chunk ---------------------------------------

def _wave(path: Path, seconds: float, *, noise_db: float = -90.0, bursts=()):
    """Noise floor plus tone bursts [(start_s, end_s, amplitude)]."""
    import math
    rate = 16000
    rng = random.Random(1)
    floor = 10 ** (noise_db / 20) * 32767
    samples = array.array("h", (int(rng.uniform(-1, 1) * floor) for _ in range(int(seconds * rate))))
    for start, end, amplitude in bursts:
        for i in range(int(start * rate), min(len(samples), int(end * rate))):
            samples[i] = int(amplitude * 32767 * math.sin(2 * math.pi * 220 * i / rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    return path


@needs_ffmpeg
def test_a_chunk_holding_a_few_seconds_of_speech_is_transcribed_not_skipped(tmp_path):
    track = _wave(tmp_path / "t.wav", 120, bursts=[(40.0, 44.8, 0.2)])
    calls = []

    def asr(wav, language, extra):
        calls.append(1)
        return [{"start": 40.0, "end": 44.8, "text": "sounds good to me"}], "en"

    found = cw.transcribe_track(track, asr=asr, language="en", ffmpeg=FFMPEG)
    assert calls and found["chunks"][0]["status"] == "ok" and found["segments"]


@needs_ffmpeg
def test_a_chunk_with_under_half_a_second_of_sound_is_skipped_and_says_so(tmp_path):
    track = _wave(tmp_path / "t.wav", 60, bursts=[(10.0, 10.3, 0.2)])
    found = cw.transcribe_track(track, asr=lambda *a: (_ for _ in ()).throw(AssertionError("no whisper")),
                                language="en", ffmpeg=FFMPEG)
    info = found["chunks"][0]
    assert info["status"] == "silent" and info["loud_seconds"] < 0.5 and found["segments"] == []


@needs_ffmpeg
@pytest.mark.parametrize("returned", [[], [{"start": 0.0, "end": 3.0, "text": "[BLANK_AUDIO]"}]])
def test_an_audible_chunk_that_returns_no_words_is_recorded_empty(tmp_path, returned):
    track = _wave(tmp_path / "t.wav", 30, bursts=[(5.0, 15.0, 0.2)])
    found = cw.transcribe_track(track, asr=lambda wav, language, extra: (returned, "en"), language="en", ffmpeg=FFMPEG)
    assert found["chunks"][0]["status"] == "empty" and found["segments"] == []


# --- 1/2. long calls on the compressed files Tyto records --------------------------

def _compressed(path: Path, seconds: int) -> Path:
    """A real AAC file of the given length: generated speech looped if ``say`` exists, else a tone."""
    source = path.with_suffix(".src.wav")
    if shutil.which("say"):
        aiff = path.with_suffix(".aiff")
        subprocess.run(["say", "-o", str(aiff), "Please send the updated numbers before Friday."], check=True)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-stream_loop", "-1", "-i", str(aiff), "-t", str(seconds),
                        "-ar", "16000", "-ac", "1", str(source)], check=True)
    else:
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=220", "-t", str(seconds),
                        "-ar", "16000", "-ac", "1", str(source)], check=True)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(source), "-c:a", "aac", "-b:a", "16k", str(path)], check=True)
    source.unlink()
    return path


@needs_ffmpeg
def test_duration_of_a_compressed_recording_is_measured_without_pydub(tmp_path):
    clip = _compressed(tmp_path / "call.m4a", 12)
    assert not clip.name.endswith(".wav")
    assert ta.get_audio_duration(clip) == pytest.approx(12, abs=1.0)


@needs_ffmpeg
def test_a_130_minute_mp4_is_refused_before_any_work_with_the_remedy_first(tmp_path, monkeypatch):
    mic = _compressed(tmp_path / "mic.mp4", 130 * 60)
    system = _compressed(tmp_path / "system.mp4", 130 * 60)
    monkeypatch.delenv("TYTO_TRANSCRIBE_PROCESS_TIMEOUT_SECS", raising=False)
    monkeypatch.delenv("TRANSCRIBE_PAIRED_ASR", raising=False)
    with patch.object(lw, "resolve_whisper_cli", side_effect=AssertionError("refuse before any work")):
        with pytest.raises(RuntimeError) as refused:
            ta.transcribe_two_files(mic, system)
    message = str(refused.value)
    assert message.startswith("Set TRANSCRIBE_PAIRED_ASR=parakeet or raise TYTO_TRANSCRIBE_PROCESS_TIMEOUT_SECS")
    assert "--recover-held" in message
    assert len(message) < 240 and message.endswith("ERR_CALL_TOO_LONG_FOR_LOCAL_ASR")  # Tyto shows 300 after a 26-char prefix


@needs_ffmpeg
def test_an_unmeasurable_length_is_refused_not_waved_through(tmp_path, monkeypatch):
    clip = _compressed(tmp_path / "call.m4a", 5)
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    assert ta.get_audio_duration(clip) is None
    with pytest.raises(RuntimeError, match="ERR_CALL_TOO_LONG_FOR_LOCAL_ASR"):
        ta._check_whisper_time_budget(None)


def test_a_call_near_the_deadline_returns_a_note_for_the_header(monkeypatch):
    monkeypatch.delenv("TYTO_TRANSCRIBE_PROCESS_TIMEOUT_SECS", raising=False)
    note = ta._check_whisper_time_budget(100 * 60.0)
    assert note and "long call" in note and ta._check_whisper_time_budget(10 * 60.0) is None


# --- 6. children die with the parent --------------------------------------------------

def test_a_child_started_elsewhere_is_killed_by_pattern_when_the_parent_dies(tmp_path):
    token = f"guardtoken{os.getpid()}x{int(time.time())}"
    pid_file = tmp_path / "pid"
    program = (
        "import subprocess, sys, time\n"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "import guarded_process as gp\n"
        f"with gp.guard_pattern({token!r}):\n"
        f"    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', {token!r}])\n"
        f"    open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "    time.sleep(120)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", program])
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        time.sleep(0.1)
    child = int(pid_file.read_text())
    os.kill(child, 0)
    parent.kill()
    parent.wait()
    for _ in range(60):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    os.kill(child, signal.SIGKILL)
    pytest.fail("the pattern-guarded child outlived its parent")
