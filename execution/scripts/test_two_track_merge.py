"""Two-track merge by segment: synthetic fixtures, no audio and no models."""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import two_track_merge as m  # noqa: E402
from lib.transcript_clarity import strip_diarization_markup  # noqa: E402


def seg(start, end, text):
    return {"start": start, "end": end, "text": text}


def profile(spans, *, floor=-80.0, total=60.0):
    """Energy profile with the given (start, end, dB) loud spans on a quiet floor."""
    frames = [floor] * int(total / m.FRAME_SECONDS)
    for start, end, db in spans:
        for i in range(int(start / m.FRAME_SECONDS), int(end / m.FRAME_SECONDS)):
            frames[i] = db
    return m.EnergyProfile(frames)


def body(text):
    return strip_diarization_markup(text).strip()


def test_overlapping_speakers_stay_whole_and_ordered_by_start():
    # The remote speaker talks 0-10 s; the operator says "yeah" at 4-5 s.
    tracks = {
        m.SYSTEM: [seg(0, 10, "so the plan is to ship the release on Friday after review")],
        m.MIC: [seg(4, 5, "yeah right")],
    }
    result = m.merge_tracks(tracks)
    assert [(t["label"], t["text"]) for t in result.turns] == [
        (m.SYSTEM, "so the plan is to ship the release on Friday after review"),
        (m.MIC, "yeah right"),
    ]
    # No word of the remote sentence is separated from the rest of it.
    assert "so the plan is to ship the release on Friday after review" in result.text


def test_consecutive_segments_of_one_speaker_join_into_one_turn():
    tracks = {m.SYSTEM: [seg(0, 3, "first part"), seg(3.2, 6, "second part")]}
    result = m.merge_tracks(tracks)
    assert [t["text"] for t in result.turns] == ["first part second part"]


def test_an_interjection_ends_the_turn_so_each_speaker_reads_whole():
    tracks = {
        m.SYSTEM: [seg(0, 3, "first part"), seg(6, 9, "second part")],
        m.MIC: [seg(4, 5, "okay")],
    }
    result = m.merge_tracks(tracks)
    assert [(t["label"], t["text"]) for t in result.turns] == [
        (m.SYSTEM, "first part"), (m.MIC, "okay"), (m.SYSTEM, "second part"),
    ]


def test_remote_clusters_are_labelled_by_diarization_overlap_not_per_word():
    diarization = {"status": "ok", "speaker_count": 2, "segments": [
        {"start": 0, "end": 10, "speaker": "A"}, {"start": 10, "end": 20, "speaker": "B"},
    ]}
    energy = {m.SYSTEM: profile([(0, 20, -20)]), m.MIC: profile([(30, 40, -20)])}
    tracks = {m.SYSTEM: [seg(0, 8, "one sentence from the first remote speaker"),
                         seg(11, 19, "reply from the second remote speaker")]}
    result = m.merge_tracks(tracks, diarization=diarization, energy=energy)
    assert [t["label"] for t in result.turns] == ["System/S1", "System/S2"]
    assert result.turns[0]["text"] == "one sentence from the first remote speaker"


def test_cluster_carried_by_the_mic_is_not_named_a_remote_speaker():
    # Cluster B is the operator's voice (loud on the quiet mic, absent on system).
    diarization = {"status": "ok", "speaker_count": 2, "segments": [
        {"start": 0, "end": 10, "speaker": "A"}, {"start": 10, "end": 20, "speaker": "B"},
    ]}
    energy = {m.SYSTEM: profile([(0, 10, -20)]), m.MIC: profile([(10, 20, -45)])}
    mapping = m.map_clusters_to_tracks({"A": [(0, 10)], "B": [(10, 20)]}, energy)
    assert mapping["A"]["track"] == m.SYSTEM and mapping["B"]["track"] == m.MIC
    tracks = {m.SYSTEM: [seg(0, 9, "remote words here")], m.MIC: [seg(11, 19, "operator words here")]}
    result = m.merge_tracks(tracks, diarization=diarization, energy=energy)
    assert [t["label"] for t in result.turns] == [m.SYSTEM, m.MIC]


def test_a_cluster_neither_track_clearly_carries_stays_unmapped():
    energy = {m.SYSTEM: profile([(0, 10, -20)]), m.MIC: profile([(0, 10, -20)])}
    assert m.map_clusters_to_tracks({"A": [(0, 10)]}, energy)["A"]["track"] is None


def test_mic_leak_of_the_remote_voice_is_dropped():
    # The mic hears the remote speaker 25 dB down: same words, overlapping.
    energy = {
        m.SYSTEM: profile([(0, 10, -20)]),
        m.MIC: profile([(0, 10, -60), (20, 30, -40)]),
    }
    tracks = {
        m.SYSTEM: [seg(0, 10, "we should move the meeting to Thursday")],
        m.MIC: [seg(0.2, 10, "we should move the meeting to thursday"), seg(20, 28, "sounds good to me")],
    }
    result = m.merge_tracks(tracks, energy=energy)
    assert [(t["label"], t["text"]) for t in result.turns] == [
        (m.SYSTEM, "we should move the meeting to Thursday"), (m.MIC, "sounds good to me"),
    ]
    assert result.stats["bleed_dropped"] == 1
    assert "1 cross-talk segments dropped" in result.text.splitlines()[0]


def test_real_cross_talk_is_kept_when_both_voices_are_at_speech_level():
    energy = {m.SYSTEM: profile([(0, 10, -20)]), m.MIC: profile([(4, 6, -42)])}
    tracks = {m.SYSTEM: [seg(0, 10, "a long remote explanation")], m.MIC: [seg(4, 6, "I disagree actually")]}
    result = m.merge_tracks(tracks, energy=energy)
    assert result.stats["bleed_dropped"] == 0
    assert [t["label"] for t in result.turns] == [m.SYSTEM, m.MIC]


def test_quiet_mic_is_compared_to_its_own_level_not_the_system_track():
    # The mic sits 25 dB below the system track throughout, but its own speech
    # at 4-6 s is at the mic's own speech level: real, not a leak.
    energy = {
        m.SYSTEM: profile([(0, 10, -15)]),
        m.MIC: profile([(0, 4, -75), (4, 6, -40), (6, 10, -75), (20, 30, -40)]),
    }
    tracks = {m.SYSTEM: [seg(0, 10, "remote speaking")], m.MIC: [seg(4, 6, "operator interjects")]}
    assert m.merge_tracks(tracks, energy=energy).stats["bleed_dropped"] == 0


def test_without_level_measurements_nothing_is_dropped_and_the_header_says_so():
    tracks = {m.SYSTEM: [seg(0, 10, "same words")], m.MIC: [seg(0, 10, "same words")]}
    result = m.merge_tracks(tracks, energy=None)
    assert len(result.turns) == 2
    assert "cross-talk check skipped" in result.text.splitlines()[0]


def test_repetition_loops_are_collapsed_and_counted():
    looped = "thank you " * 12 + "and then we left"
    result = m.merge_tracks({m.SYSTEM: [seg(0, 30, looped)]})
    assert "thank you thank you […] and then we left" in result.text
    assert result.stats["repeats_removed"] == 20


def test_ordinary_repeated_phrases_are_not_collapsed():
    text = "i think we can, i think it works, i think so, and i think that is fine"
    assert m.collapse_repeats(text) == (text, 0)


def test_non_speech_segments_are_dropped():
    tracks = {m.SYSTEM: [seg(0, 2, "[Pausa]"), seg(2, 4, "(music)"), seg(4, 6, "real words")]}
    assert [t["text"] for t in m.merge_tracks(tracks).turns] == ["real words"]


def test_header_is_one_line_that_consumers_strip_and_turn_prefixes_are_clock_stamped():
    diarization = {"status": "ok", "speaker_count": 1, "cli_build": "abc12345",
                   "segments": [{"start": 0, "end": 10, "speaker": "A"}]}
    energy = {m.SYSTEM: profile([(0, 10, -20)]), m.MIC: profile([(20, 30, -20)])}
    tracks = {m.SYSTEM: [seg(65, 70, "hello there")], m.MIC: [seg(75, 80, "hi back")]}
    result = m.merge_tracks(tracks, diarization=diarization, energy=energy)
    header, rest = result.text.split("\n\n", 1)
    assert header.startswith("[Speakers: ") and header.endswith("]") and "\n" not in header
    assert header.count("]") == 1
    assert "speaker names not established" in header
    assert rest == "[1:05–1:10] [System]\nhello there\n\n[1:15–1:20] [Mic]\nhi back"
    # The clarity checks see only speech.
    assert body(result.text).split() == ["hello", "there", "hi", "back"]
    assert "[System]" not in body(result.text) and "Speakers" not in body(result.text)


def test_failed_diarization_is_stated_and_the_transcript_still_lands():
    tracks = {m.SYSTEM: [seg(0, 3, "hello")], m.MIC: [seg(4, 6, "hi")]}
    result = m.merge_tracks(tracks, diarization={"status": "failed", "category": "timed out"})
    assert "FluidAudio offline failed (timed out)" in result.text.splitlines()[0]
    assert [t["label"] for t in result.turns] == [m.SYSTEM, m.MIC]


def test_no_speech_returns_the_marker_with_no_header():
    result = m.merge_tracks({m.SYSTEM: [seg(0, 2, "[silence]")], m.MIC: []})
    assert result.turns == [] and result.text == m.NO_SPEECH_TEXT


def test_frame_profile_is_relative_to_the_tracks_own_speech_level():
    quiet = profile([(0, 5, -45)])
    loud = profile([(0, 5, -15)])
    assert quiet.relative_db([(0, 5)]) == pytest.approx(loud.relative_db([(0, 5)]), abs=0.5)
    assert quiet.relative_db([(10, 15)]) == pytest.approx(-35.0, abs=0.5)
