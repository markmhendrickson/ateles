"""
Tests for the transcript clarity gate.

Fixtures are the three voice memos recorded 2026-09-05, verbatim. They are
about adapter architecture and Neotoma documentation — no personal data, no
third-party names, checked rather than assumed before committing.

The calibration these tests pin down:

    fixture                    words  dur(s)  dup_frac  run  5gram  verdict
    false_start_2s                 5     2.0     0.000    1      1  PASS
    repetition_loop_long         864   512.9     0.328   12     19  FLAG (1)
    clear_neotoma_detail         103    54.7     0.000    1      1  PASS

Every threshold sits inside the gap between the flagged and passing columns,
so the gate is not tuned to the exact observed values.

``false_start_2s`` changed from FLAG to PASS when the gate was measured
against all 883 stored transcriptions rather than these three memos. See
``test_false_start_now_passes_as_a_short_clip`` for why, and the CHECK 2 and
CHECK 4 comments in ``transcript_clarity.py`` for the two defects that scan
found: 234 of 234 language flags were false positives, and 218 of 252
truncation-only flags were real short memos.

Fixture text below the three real memos is synthesized, not transcript
content, and names no one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.transcript_clarity import (  # noqa: E402
    MAX_CONSECUTIVE_REPEATS,
    MAX_DUPLICATE_SENTENCE_FRACTION,
    MIN_WORDS,
    TRUNCATION_DURATION_SECONDS,
    assess_transcript,
    classify_language,
    is_bracket_only,
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


def test_false_start_now_passes_as_a_short_clip():
    """
    The 2-second "As I'm thinking about the" false start passes — deliberately.

    It was originally calibrated as a must-flag, on three memos. Measured
    against all 883 stored transcriptions, that calibration does not hold: 41
    real short memos sit in this fixture's exact neighbourhood (1-3.5 seconds,
    3-8 words), and this fixture's 2.5 words/second is ABOVE the 1.43 median of
    the 218 genuine short memos the old rule was pausing. No measurement on
    text plus duration separates this clip from a memo the operator meant to
    make, so the gate cannot flag it without flagging all 41 with it.

    Flagging a real memo costs the operator a confirmation; the gate exists to
    catch lost audio, and 2 seconds of audio that produced 5 words lost none.
    """
    report = assess_transcript(
        _fixture("false_start_2s.txt"), duration_seconds=FALSE_START_DURATION
    )
    assert report.clear, report.summary
    assert report.metrics["words"] == 5
    assert report.metrics["short_clip"] is True


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


# ── Defect 1: the language label is not a clean ISO-639-1 code ──────────────
#
# Measured over 883 stored transcriptions: all 234 language flags were false
# positives and the corpus contains no genuine non-en/es/ca content. The gate
# was comparing raw transcriber labels ('eng', 'ca-es-mixed') against a set of
# ISO-639-1 codes. These tests pin the normalization that fixes it.


def _clear_with_language(code):
    return assess_transcript(
        _fixture("clear_neotoma_detail.txt"),
        duration_seconds=CLEAR_DURATION,
        language=code,
    )


def test_iso_639_2_codes_are_recognized_as_expected():
    """'eng'/'spa'/'cat' are the same languages as 'en'/'es'/'ca'.

    'eng' alone accounts for 169 of the 234 false positives in the corpus.
    """
    for code in ("eng", "spa", "cat"):
        report = _clear_with_language(code)
        assert report.clear, f"{code}: {report.summary}"
        assert report.metrics["language_verdict"] == "expected"


def test_compound_label_containing_an_expected_language_passes():
    """A compound label is the operator code-switching, not foreign audio."""
    for code in ("ca-es-mixed", "eng/cat", "es/en mixed"):
        report = _clear_with_language(code)
        assert report.clear, f"{code}: {report.summary}"
        assert report.metrics["language_verdict"] == "expected"


def test_regional_suffix_is_stripped_before_comparison():
    """'en-US' is English; the region must not read as a second language."""
    for code in ("en-US", "es-ES", "ca-ES", "en-GB"):
        report = _clear_with_language(code)
        assert report.clear, f"{code}: {report.summary}"
        assert report.metrics["language_codes"][0] in ("en", "es", "ca")


def test_regional_suffix_on_an_unexpected_language_still_flags():
    """Stripping the region must not turn 'pt-BR' into a pass."""
    report = _clear_with_language("pt-BR")
    assert not report.clear
    assert 4 in {f.check for f in report.findings}
    assert report.metrics["language_codes"] == ["pt"]


def test_unparseable_label_does_not_flag():
    """
    A label we cannot read is missing evidence, not adverse evidence.

    Treating "unparseable" as "wrong language" is precisely what produced 234
    false positives, so the two are separate outcomes and only one flags.
    """
    for code in ("zz9", "!!!", "qqqq", "mixed"):
        report = _clear_with_language(code)
        assert report.clear, f"{code}: {report.summary}"
        assert report.metrics["language_verdict"] == "unparseable"


def test_genuine_wrong_language_still_flags():
    """The check must keep working: Portuguese is not one of the three."""
    for code, expected in (("de", "de"), ("por", "pt"), ("jpn", "ja"), ("ru", "ru")):
        report = _clear_with_language(code)
        assert not report.clear, f"{code}: expected a flag"
        assert 4 in {f.check for f in report.findings}
        assert report.metrics["language_codes"] == [expected]
        assert report.metrics["language_verdict"] == "unexpected"


def test_language_classification_outcomes_are_distinct():
    """The four verdicts are separate outcomes, not two."""
    assert classify_language(None) == ("none", [])
    assert classify_language("auto") == ("none", [])
    assert classify_language("eng") == ("expected", ["en"])
    assert classify_language("por") == ("unexpected", ["pt"])
    assert classify_language("zz9")[0] == "unparseable"


# ── Defect 2: truncation must be measured against duration ─────────────────
#
# Measured over 883 stored transcriptions: 252 transcripts were flagged by
# check 2 alone and 218 were real short memos (median 2.6s, 3 words). Across
# all 340 labelled check-2 flags, duration separated the populations without
# exception: under 20s, all 294 were real memos; at or over 20s, all 46 were
# failures.


def test_short_clip_with_proportionate_text_passes():
    """A brief recording that produced text is a real short memo."""
    for text, duration in (
        ("Okay.", 2.6),
        ("Call the plumber back.", 3.1),
        ("Yes.", 1.2),
        ("Remember to move the standing order.", 4.0),
    ):
        report = assess_transcript(text, duration_seconds=duration)
        assert report.clear, f"{text!r} @ {duration}s: {report.summary}"
        assert report.metrics["short_clip"] is True


def test_short_clip_with_very_low_words_per_second_still_passes():
    """
    One word over nineteen seconds of mostly silence is still a real memo.

    The corpus contains these down to 0.05 words/second, all labelled genuine,
    so the words-per-second floor must not apply below the duration gate.
    """
    report = assess_transcript("Okay.", duration_seconds=19.0)
    assert report.clear, report.summary


def test_short_clip_with_no_text_flags_as_empty_not_truncated():
    """A short clip that produced nothing is check 3's business, not check 2."""
    report = assess_transcript("", duration_seconds=2.6)
    assert not report.clear
    assert [f.check for f in report.findings] == [3]


def test_long_recording_with_little_text_still_flags():
    """The failure the gate exists for: audio went in, almost no text came out."""
    for text, duration in (
        ("Okay.", 300.0),
        ("Just a couple of words.", 120.0),
        ("Thanks.", 21.0),
    ):
        report = assess_transcript(text, duration_seconds=duration)
        assert not report.clear, f"{text!r} @ {duration}s must flag"
        assert 2 in {f.check for f in report.findings}


def test_truncation_duration_gate_boundary():
    """The gate is at TRUNCATION_DURATION_SECONDS: below passes, at flags."""
    text = "Okay."
    just_under = assess_transcript(
        text, duration_seconds=TRUNCATION_DURATION_SECONDS - 0.1
    )
    at_gate = assess_transcript(text, duration_seconds=TRUNCATION_DURATION_SECONDS)
    assert just_under.clear, just_under.summary
    assert not at_gate.clear
    assert 2 in {f.check for f in at_gate.findings}


def test_missing_duration_does_not_flag_truncation():
    """
    Without duration there is no truncation measurement to make.

    The permissive default matches the language fix: absent evidence must not
    manufacture a flag. Duration resolves for essentially every real memo, so
    this path is rare.
    """
    report = assess_transcript("Okay.")
    assert report.clear, report.summary
    assert report.metrics["duration_known"] is False
    assert "words_per_second" not in report.metrics


def test_missing_duration_still_catches_the_text_only_checks():
    """Empty, repetition and language all flag without a duration."""
    assert not assess_transcript("").clear
    assert not assess_transcript(_fixture("repetition_loop_long.txt")).clear
    assert not assess_transcript("Bom dia.", language="por").clear


# ── Bracket-only transcripts (non-linguistic capture) ───────────────────────
#
# Whisper attaches a confident language code even to audio with nothing
# spoken in it, describing what it heard with a bracketed tag instead:
# [silence], [pause], [breathing], [outro jingle], channel markers like
# [Mic]/[System]. Verified against the 33 language flags remaining after the
# CHECK 4 normalize fix: 28 of 33 are entirely such tags, with no lexical
# word anywhere in the transcript. None of the strings below are real
# transcript content — they are synthesized to match the corpus's observed
# tag vocabulary and shape.


def test_single_bracket_tag_is_bracket_only():
    assert is_bracket_only("[silence]")
    assert is_bracket_only("[pause]")


def test_multiple_bracket_tags_are_bracket_only():
    assert is_bracket_only("[Mic]\n[typing]\n\n[System]\n[outro jingle]")
    assert is_bracket_only("[breathing][clicking]")


def test_non_english_bracket_tag_is_bracket_only():
    """The tag itself can be in any language — only the OUTSIDE text counts."""
    assert is_bracket_only("[pausa]")
    assert is_bracket_only("[música suave]")


def test_bracket_tag_with_real_words_is_not_bracket_only():
    """A tag alongside real words is linguistic content, not a pure capture."""
    assert not is_bracket_only("Say things [babbling]")
    assert not is_bracket_only("[Mic] Actually, let's start with the budget.")


def test_channel_tags_alone_are_bracket_only():
    assert is_bracket_only("[Mic]")
    assert is_bracket_only("[System]\n[Mic]")


def test_empty_string_is_not_bracket_only():
    """Empty text is check 3's business — this predicate must not double-claim it."""
    assert not is_bracket_only("")
    assert not is_bracket_only("   ")


def test_bracket_only_transcript_does_not_flag_language():
    """
    The core fix: a bracket-only transcript is not a language failure,
    regardless of the label Whisper assigned it.
    """
    for label in ("sh", "por", "ita", "fin", "jpn", "eng/cat", "ca-es-mixed"):
        report = assess_transcript("[silence]", duration_seconds=0.4, language=label)
        assert report.metrics["language_verdict"] == "non_linguistic"
        assert 4 not in {f.check for f in report.findings}, (
            f"label {label!r} incorrectly flagged check 4: {report.summary}"
        )


def test_bracket_only_short_clip_is_clean():
    """
    A brief bracket-only clip is clean, not held — the same outcome a real
    short memo gets. [silence] on a third-of-a-second recording is an
    accidental capture, not a defect to surface.
    """
    report = assess_transcript("[silence]", duration_seconds=0.4, language="sh")
    assert report.clear, report.summary
    assert report.metrics["bracket_only"] is True


def test_bracket_only_with_real_words_still_language_checked():
    """
    Mixed bracket-and-text content is linguistic and stays fully subject to
    the language check — the exemption must not overreach.
    """
    report = assess_transcript(
        "Say things [babbling]", duration_seconds=5.0, language="ita"
    )
    assert not report.metrics["bracket_only"]
    assert report.metrics["language_verdict"] == "unexpected"
    assert 4 in {f.check for f in report.findings}


def test_channel_tag_style_transcript_on_a_short_clip_is_clean():
    """
    [Mic]/[System]-only transcripts are the channel-tag variant of the tag
    corpus. On a short clip (under TRUNCATION_DURATION_SECONDS) the result is
    fully clean, same as the single-tag case.
    """
    report = assess_transcript(
        "[Mic]\n[typing]\n\n[System]\n[outro jingle]",
        duration_seconds=8.0,
        language="por",
    )
    assert report.metrics["bracket_only"] is True
    assert 4 not in {f.check for f in report.findings}
    assert report.clear, report.summary


def test_moderate_duration_bracket_only_flags_via_truncation_not_language():
    """
    Not every bracket-only transcript reads as clean: 4 of the 28 corpus
    cases sit at 23-1268 seconds, long enough that CHECK 2's duration-gated
    word-count floor already fires on a handful of bracket tokens,
    independent of the language exemption. The exemption changes WHICH check
    catches these (2, not 4) — not whether they get caught.
    """
    text = "[Mic]\n[typing]\n\n[System]\n[outro jingle]"
    report = assess_transcript(text, duration_seconds=45.0, language="por")
    assert report.metrics["bracket_only"] is True
    assert 4 not in {f.check for f in report.findings}
    assert not report.clear, "a 45-second recording with only tags must still flag"
    assert 2 in {f.check for f in report.findings}


def test_long_bracket_only_recording_still_flags_via_truncation():
    """
    The 21-minute corpus case: a long recording that produced nothing but
    ambient/UI tags is a genuine capture failure, and stays flagged — just
    not on check 4. Check 2 (truncation) already measures "far too little
    text for this much audio" and continues to catch it once check 4 stops
    misreading the tag's language label.
    """
    text = "[Mic]\n[typing]\n\n[System]\n[outro jingle]\n\n[Mic]\n[typing][tapping]\n\n[System]\n[singing]"
    report = assess_transcript(text, duration_seconds=1268.31, language="por")
    assert report.metrics["bracket_only"] is True
    assert 4 not in {f.check for f in report.findings}
    assert not report.clear, "a 21-minute capture with no speech must still flag"
    assert 2 in {f.check for f in report.findings}


def test_bracket_only_genuine_greeting_is_unaffected():
    """
    The one unresolved language flag in the corpus — a real three-word
    Cyrillic greeting — has no brackets at all and must keep flagging.
    Greeting words are a known ASR hallucination pattern, so this case is
    deliberately NOT special-cased here.
    """
    report = assess_transcript("Привет. А? Привет", language="rus")
    assert not is_bracket_only("Привет. А? Привет")
    assert report.metrics["language_verdict"] == "unexpected"
    assert 4 in {f.check for f in report.findings}
