"""
Tests for the transcript clarity gate.

Fixtures are the three voice memos recorded 2026-09-05, verbatim. They are
about adapter architecture and Neotoma documentation — no personal data, no
third-party names, checked rather than assumed before committing.

The calibration these tests pin down:

    fixture                    words  dur(s)  dup_frac  run  5gram  verdict
    false_start_2s                 5     2.0     0.000    1      1  FLAG (2)
    repetition_loop_long         864   512.9     0.328   12     19  FLAG (1)
    clear_neotoma_detail         103    54.7     0.000    1      1  PASS

Every threshold sits inside the gap between the flagged and passing columns,
so the gate is not tuned to the exact observed values.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.transcript_clarity import (  # noqa: E402
    MAX_CONSECUTIVE_REPEATS,
    MAX_DUPLICATE_SENTENCE_FRACTION,
    MIN_WORDS,
    assess_transcript,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "transcripts"

# Durations measured from the source .wav files with `wave`.
FALSE_START_DURATION = 2.0
REPETITION_DURATION = 512.9
CLEAR_DURATION = 54.7


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── The three real memos ────────────────────────────────────────────────────


def test_clear_memo_passes():
    """The 103-word Neotoma-documentation memo is clean and must process."""
    report = assess_transcript(
        _fixture("clear_neotoma_detail.txt"), duration_seconds=CLEAR_DURATION
    )
    assert report.clear, report.summary
    assert report.findings == []


def test_false_start_is_flagged_as_truncated():
    """The 2-second "As I'm thinking about the" false start must not process."""
    report = assess_transcript(
        _fixture("false_start_2s.txt"), duration_seconds=FALSE_START_DURATION
    )
    assert not report.clear
    assert 2 in {f.check for f in report.findings}
    assert report.metrics["words"] == 5


def test_repetition_loop_is_flagged():
    """The 8.5-minute memo ends in a hallucinated loop and must not process."""
    report = assess_transcript(
        _fixture("repetition_loop_long.txt"), duration_seconds=REPETITION_DURATION
    )
    assert not report.clear
    assert 1 in {f.check for f in report.findings}
    finding = next(f for f in report.findings if f.check == 1)
    # The operator gets the offending span, not the whole 864-word transcript.
    assert "avoid this" in finding.excerpt.lower()
    assert len(finding.excerpt) <= 200


def test_measured_values_match_recorded_calibration():
    """
    Pin the measurements the thresholds were chosen from. If the metric
    computation drifts, this fails before the thresholds silently stop meaning
    what the comments claim they mean.
    """
    bad = assess_transcript(
        _fixture("repetition_loop_long.txt"), duration_seconds=REPETITION_DURATION
    )
    good = assess_transcript(
        _fixture("clear_neotoma_detail.txt"), duration_seconds=CLEAR_DURATION
    )
    assert bad.metrics["duplicate_sentence_fraction"] == 0.328
    assert bad.metrics["longest_consecutive_repeat"] == 12
    assert bad.metrics["max_ngram_repeats"] == 19
    assert good.metrics["duplicate_sentence_fraction"] == 0.0
    assert good.metrics["longest_consecutive_repeat"] == 1
    assert good.metrics["max_ngram_repeats"] == 1
    # Thresholds live strictly between the two populations.
    assert good.metrics["duplicate_sentence_fraction"] < MAX_DUPLICATE_SENTENCE_FRACTION
    assert bad.metrics["duplicate_sentence_fraction"] > MAX_DUPLICATE_SENTENCE_FRACTION
    assert good.metrics["longest_consecutive_repeat"] <= MAX_CONSECUTIVE_REPEATS
    assert bad.metrics["longest_consecutive_repeat"] > MAX_CONSECUTIVE_REPEATS


def test_long_memo_body_passes_without_the_hallucinated_tail():
    """
    The substance of the long memo is fine — it is only the trailing loop that
    fails. Proves check 1 targets the artifact, not memo length.
    """
    text = _fixture("repetition_loop_long.txt")
    body = text.split("Maybe there's a way to avoid this")[0]
    report = assess_transcript(body, duration_seconds=400.0)
    assert report.clear, report.summary


# ── Each check in isolation ─────────────────────────────────────────────────


def test_empty_transcript_flags_check_3():
    report = assess_transcript("", duration_seconds=30.0)
    assert not report.clear
    assert [f.check for f in report.findings] == [3]


def test_whitespace_only_transcript_flags_check_3():
    report = assess_transcript("   \n  \t ", duration_seconds=30.0)
    assert not report.clear
    assert [f.check for f in report.findings] == [3]


def test_consecutive_repeats_flag_even_when_fraction_is_low():
    """A short run of repeats inside a long clean transcript still flags."""
    filler = " ".join(f"Sentence number {i} about the pipeline." for i in range(200))
    text = filler + " " + ("Same trailing phrase here. " * 10)
    report = assess_transcript(text, duration_seconds=600.0)
    assert not report.clear
    assert 1 in {f.check for f in report.findings}


def test_ngram_repetition_without_sentence_punctuation_flags():
    """Repetition that never terminates a sentence is still caught."""
    text = "the system will retry the request " * 12
    report = assess_transcript(text, duration_seconds=120.0)
    assert not report.clear
    assert 1 in {f.check for f in report.findings}


def test_short_transcript_flags_check_2():
    report = assess_transcript("Just a few words here.", duration_seconds=60.0)
    assert not report.clear
    assert 2 in {f.check for f in report.findings}


def test_low_words_per_second_flags_check_2():
    """A long recording that produced almost no text lost most of its audio."""
    text = " ".join(f"word{i}" for i in range(40))
    report = assess_transcript(text, duration_seconds=600.0)
    assert not report.clear
    assert any(
        "words/second" in f.reason for f in report.findings if f.check == 2
    ), report.summary


def test_degenerate_single_segment_flags_check_2():
    """One sentence spanning many minutes is degenerate output."""
    text = " ".join(f"word{i}" for i in range(600))  # no terminal punctuation
    report = assess_transcript(text, duration_seconds=400.0)
    assert not report.clear
    assert 2 in {f.check for f in report.findings}


def test_unexpected_language_flags_check_4():
    report = assess_transcript(
        _fixture("clear_neotoma_detail.txt"),
        duration_seconds=CLEAR_DURATION,
        language="de",
    )
    assert not report.clear
    assert 4 in {f.check for f in report.findings}


def test_expected_languages_do_not_flag():
    for code in ("en", "es", "ca", "English", "SPANISH"):
        report = assess_transcript(
            _fixture("clear_neotoma_detail.txt"),
            duration_seconds=CLEAR_DURATION,
            language=code,
        )
        assert report.clear, f"{code}: {report.summary}"


def test_auto_or_missing_language_is_not_a_mismatch():
    for code in (None, "", "auto"):
        report = assess_transcript(
            _fixture("clear_neotoma_detail.txt"),
            duration_seconds=CLEAR_DURATION,
            language=code,
        )
        assert report.clear, f"{code!r}: {report.summary}"


def test_missing_duration_does_not_manufacture_a_flag():
    """Duration-dependent checks are skipped, not failed, when duration is unknown."""
    report = assess_transcript(_fixture("clear_neotoma_detail.txt"))
    assert report.clear, report.summary
    assert "words_per_second" not in report.metrics


def test_missing_duration_still_catches_repetition():
    """Text-only checks keep working without a duration."""
    report = assess_transcript(_fixture("repetition_loop_long.txt"))
    assert not report.clear
    assert 1 in {f.check for f in report.findings}


def test_summary_names_the_check_number():
    report = assess_transcript("tiny.", duration_seconds=60.0)
    assert "check 2" in report.summary


def test_boundary_word_count_passes():
    """A transcript exactly at MIN_WORDS is not flagged as truncated."""
    text = " ".join(f"word{i}" for i in range(MIN_WORDS)) + "."
    report = assess_transcript(text, duration_seconds=30.0)
    assert not any(
        f.check == 2 and "words (threshold" in f.reason for f in report.findings
    ), report.summary
