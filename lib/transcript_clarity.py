"""
Transcript clarity checks — measured, not judged.

A voice memo is processed automatically only when its transcript passes every
check below. Anything else pauses for operator confirmation. The point of this
module is that "is this clear?" is answered by counting things in the
transcript, never by asking a model for an opinion: an unvalidated instrument
that returns a confident answer to every input is worse than no instrument.

Signals we can actually measure
-------------------------------
The pipeline transcribes via the OpenAI Whisper API (``whisper-1``) with the
default ``response_format``, which returns **plain text only** — no token or
segment log-probabilities. Per-segment confidence is therefore NOT available
to us, and any threshold expressed in logprobs would be decoration. Every
check here runs on the transcript text plus the audio duration, both of which
we always have. If the pipeline later moves to a backend that emits segment
confidence (whisper.cpp, or ``response_format="verbose_json"``), add a check
here rather than replacing these — they are independent evidence.

Calibration
-----------
The thresholds were first chosen against the three memos recorded 2026-09-05,
whose measurements are recorded in ``test_transcript_clarity.py`` alongside
the fixtures. Running this module against all 883 stored transcriptions then
found two of the four checks measurably wrong, and both are fixed here.

A third defect surfaced verifying that fix: of the 33 transcripts still
flagging on language after the fixes below, 28 contain no lexical words at
all — they are Whisper's bracketed non-speech descriptors (``[silence]``,
``[pause]``, ``[outro jingle]``, ``[background noise]``, channel tags like
``[Mic]``/``[System]``, ...) with a confident-looking language code attached
to nothing linguistic. See ``is_bracket_only()`` and CHECK 4 below.

* **Check 4 (language)** fired 234 times and was wrong all 234 times. The
  transcriber does not return clean ISO-639-1 codes; it returns ISO-639-2
  ('eng', 'spa'), regional codes, and compound labels ('ca-es-mixed'). The
  labels are now normalized before comparison, and an unreadable label is a
  separate outcome from a wrong language — only the latter flags.

* **Check 2 (truncation)** flagged on word count alone. Of the 252 transcripts
  it flagged by itself, 218 were real short memos that transcribed perfectly
  (median 2.6 seconds, 3 words). Truncation is now measured against duration,
  which separated the two populations across all 340 labelled cases without a
  single exception.

Checks 1 (repetition) and 3 (empty) were measured correct over the same corpus
— 132 genuine repetition loops and 47 genuine empties — and are unchanged.

The lesson the two defects share: a check that cannot tell "I could not
measure this" from "this measured badly" will report the former as the latter,
confidently, forever. Absent evidence must not manufacture a flag.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Thresholds — each justified by a measurement, not by taste.
# ---------------------------------------------------------------------------

# CHECK 1 — repetition loop.
# Whisper hallucinates on trailing silence by repeating the last phrase. The
# 8.5-minute memo of 2026-09-05 ends with "Maybe there's a way to avoid this."
# 18 times: 32.8% of its sentences are duplicates and 12 of them run back to
# back. The two clean memos measure 0.0% and a run of 1. Any cut between those
# works; these sit near the midpoint of a very wide gap.
MAX_DUPLICATE_SENTENCE_FRACTION = 0.15  # observed: 0.328 bad vs 0.000 good
MAX_CONSECUTIVE_REPEATS = 4  # observed: 12 bad vs 1 good

# A repeated n-gram catches the same artifact when the repetition is not
# sentence-aligned (no terminal punctuation between repeats).
REPEAT_NGRAM_SIZE = 5
MAX_NGRAM_REPEATS = 6  # observed: 19 bad vs 1 good

# CHECK 2 — truncation / degenerate output.
# The 2-second false start transcribes to 5 words ("As I'm thinking about
# the"). The shortest substantive memo is 103 words over 54.7s.
#
# The original check flagged on word count ALONE, and that was wrong 86% of
# the time. Measured over 883 stored transcriptions: 252 transcripts were
# flagged by check 2 and nothing else, and 218 of them are real short memos
# that transcribed perfectly — median 2.6 seconds, median 3 words, genuine
# "Okay." recordings the operator meant to make. A short recording that
# produced proportionate text is a memo, not a failure.
#
# Word count is therefore only evidence of truncation when the DURATION says
# text is missing. Across all 340 check-2 flags the analysis pass labelled,
# duration separates the two populations without a single exception:
#
#     duration < 20s   ->  294 real short memos,   0 failures
#     duration >= 20s  ->    0 real short memos,  46 failures
#
# So the word-count floor applies only above TRUNCATION_DURATION_SECONDS. The
# words-per-second and seconds-per-sentence checks below are already
# duration-aware and already correct; they are untouched and still catch the
# long-recording-with-almost-no-text failures (the 46 above all measure under
# 0.5 words/second).
MIN_WORDS = 15  # observed: 5 flagged vs 103 and 864 passing

# Above this, a transcript under MIN_WORDS means audio went missing. Below it,
# a short transcript is simply a short memo. 20s sits in the gap: the longest
# real short memo in the corpus runs 19.2s, the shortest true failure 20.5s.
TRUNCATION_DURATION_SECONDS = 20.0

# The old MIN_DURATION_SECONDS = 5.0 check flagged any recording under five
# seconds outright. It fired on 40 of the real short memos and on nothing
# else, so it is removed rather than retuned: "the recording is short" is a
# fact about the recording, never evidence that the transcript lost anything.
# A short clip that produced text is a memo; a short clip that produced no
# text is caught by check 3 (empty).

# Speech runs ~1.7-2.6 words/second in these recordings (1.68, 1.88, 2.55).
# A transcript far *shorter* than the duration implies means Whisper dropped
# most of the audio. The floor is set well below the slowest observed rate so
# normal pauses and thinking-out-loud never trip it.
MIN_WORDS_PER_SECOND = 0.5  # observed minimum: 1.68

# A single segment spanning minutes with no sentence boundary is degenerate
# output rather than speech.
MAX_SECONDS_PER_SENTENCE = 120.0

# CHECK 3 — empty transcript.
# Distinct from truncation so the operator message can say which happened.

# CHECK 4 (bracket-only exemption) — non-linguistic capture.
# Whisper emits bracketed descriptors — [silence], [pause], [breathing],
# [outro jingle], channel tags like [Mic]/[System] — on audio with nothing
# spoken in it, and still attaches a confident language code. A transcript
# that is ENTIRELY such tags, with no lexical text outside them, is not a
# language problem: there is no language to have gotten wrong, so CHECK 4
# below skips it regardless of the label Whisper assigned.
#
# Measured over the 33 language flags remaining after the CHECK 4 normalize
# fix, 28 are bracket-only. The other 5 are NOT: two contain real words
# alongside or instead of tags ("Say things [babbling]", "Bonito. Bonito. I
# want to look my"), one is a single filler character with no brackets ('嗯'),
# one is a hallucinated nonsense-syllable loop with no brackets ("Saramiiku,
# Saramiiku, ..."), and one is a genuine three-word Cyrillic greeting. Only
# the 28 true bracket-only cases are exempt; the other 5 keep going through
# CHECK 4 like any other transcript, because they carry something that could
# be a real word — that is deliberate: the Cyrillic greeting stays flagged.
#
# Exempting bracket-only text from CHECK 4 is not the same as calling it
# "clear" — that would silently swallow a genuine capture failure. It isn't:
# 24 of the 28 corpus cases run under 31 seconds and are the audio
# equivalent of an accidental button press — [silence] on a third-of-a-second
# clip tells the operator nothing they need to act on, and correctly reads as
# clean once CHECK 4 stops misreading its language tag. The other 4 sit at
# 23s, 31s, 52s and 1268s (21 minutes) — long enough that CHECK 2's
# duration-gated word-count and words-per-second floors already fire on
# their own (a handful of bracket tokens is nowhere near MIN_WORDS once
# duration crosses TRUNCATION_DURATION_SECONDS), independent of this fix.
# Nothing here is swallowed: CHECK 2 is untouched and keeps flagging all
# four as likely dropped captures — so no new mechanism is needed to
# surface them, and no duration threshold is invented for this exemption:
# bracket-only text is
# exempt from CHECK 4 regardless of duration, and whether the transcript is
# otherwise clean is left entirely to the checks that already measure that
# (chiefly CHECK 2, which is duration-aware by design).

# CHECK 4 — language mismatch.
# The operator speaks English, Spanish and Catalan. Anything else is worth a
# pause rather than a guess. An absent/auto language is not a mismatch — we
# only flag a positively-detected unexpected language.
#
# The label the transcriber hands us is NOT a clean ISO-639-1 code. Measured
# over 883 stored transcriptions, the corpus contains ISO-639-2 codes ('eng',
# 'spa', 'por'), regional codes, and compound labels ('ca-es-mixed',
# 'eng/cat', 'es/en mixed'). The original check compared the raw label against
# a set of ISO-639-1 codes, so every one of those 234 labels was read as an
# unexpected language: 234 flags, 234 false positives, zero genuine non-en/es/ca
# content. Normalize before comparing.
EXPECTED_LANGUAGES = frozenset({"en", "es", "ca"})

# ISO-639-2/B and /T codes for the languages we care about mapping. Only the
# codes actually observed plus their obvious siblings — an incomplete map is
# safe because an unrecognized code is treated as unparseable, not as wrong.
_ISO_639_2_TO_1 = {
    "eng": "en",
    "spa": "es",
    "cat": "ca",
    "por": "pt",
    "ita": "it",
    "fra": "fr",
    "fre": "fr",
    "deu": "de",
    "ger": "de",
    "nld": "nl",
    "dut": "nl",
    "rus": "ru",
    "jpn": "ja",
    "zho": "zh",
    "chi": "zh",
    "fin": "fi",
    "swe": "sv",
    "pol": "pl",
    "ara": "ar",
    "eus": "eu",
    "baq": "eu",
    "glg": "gl",
}

# English names the transcriber sometimes returns instead of a code.
_LANGUAGE_NAME_TO_CODE = {
    "english": "en",
    "spanish": "es",
    "castilian": "es",
    "catalan": "ca",
    "portuguese": "pt",
    "italian": "it",
    "french": "fr",
    "german": "de",
    "dutch": "nl",
    "russian": "ru",
    "japanese": "ja",
    "chinese": "zh",
    "finnish": "fi",
}

# Labels that carry no language claim at all. 'auto' is the transcriber saying
# it did not decide; an empty string likewise.
_NON_CLAIMS = frozenset({"", "auto", "unknown", "und", "none", "null", "mul", "zxx"})

# A compound label separates its parts with any of these.
_COMPOUND_SPLIT_RE = re.compile(r"[/,+&_\s-]+")

# A BCP-47 region subtag on the ORIGINAL (pre-lowercase) label: two uppercase
# letters or three digits, hyphenated onto a 2-3 letter language subtag. Case
# is what distinguishes 'en-US' (region) from Whisper's 'ca-es-mixed' (two
# languages), so this must run before the label is lowercased.
_REGION_SUFFIX_RE = re.compile(r"\b([A-Za-z]{2,3})-(?:[A-Z]{2}|[0-9]{3})\b")

# Words that decorate a compound label without naming a language.
_COMPOUND_NOISE = frozenset({"mixed", "mix", "and", "with", "plus", "or"})

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# A Whisper non-speech descriptor: square brackets around anything. Matches
# '[silence]', '[heavy breathing]', '[Mic]', '[música suave]' alike — the
# content inside is never checked against a fixed vocabulary, because an
# incomplete list of descriptor names would be the same mistake as the
# original EXPECTED_LANGUAGES check: confidently wrong on the tags it didn't
# anticipate. What matters is only that everything OUTSIDE the brackets is
# empty.
_BRACKET_TAG_RE = re.compile(r"\[[^\[\]]*\]")


@dataclass
class ClarityFinding:
    """One failed check: which one, why, and the span the operator should see."""

    check: int
    name: str
    reason: str
    excerpt: str = ""


@dataclass
class ClarityReport:
    """Verdict for one transcript. ``clear`` is true only if nothing flagged."""

    clear: bool
    findings: list[ClarityFinding] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """One line per finding, for the operator notification."""
        if self.clear:
            return "clear"
        return "; ".join(f"check {f.check} ({f.name}): {f.reason}" for f in self.findings)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _normalize(sentence: str) -> str:
    return re.sub(r"[^\w\s]", "", sentence.lower()).strip()


def _normalize_language_part(part: str) -> str | None:
    """
    Map one atom of a language label to an ISO-639-1 code.

    Returns None when the atom names no language we recognize — which includes
    decorative words like 'mixed' and codes outside our map.
    """
    part = part.strip().lower()
    if not part or part in _COMPOUND_NOISE:
        return None
    if part in _LANGUAGE_NAME_TO_CODE:
        return _LANGUAGE_NAME_TO_CODE[part]
    if len(part) == 2 and part.isalpha():
        return part
    if len(part) == 3 and part.isalpha():
        return _ISO_639_2_TO_1.get(part)
    return None


def classify_language(label: str | None) -> tuple[str, list[str]]:
    """
    Decide what a transcriber's language label means.

    "Unparseable" and "wrong language" are deliberately different outcomes.
    Conflating them is exactly what made the original check fire 234 times on
    883 transcripts without once being right: an ISO-639-2 code we failed to
    recognize is our ignorance, not evidence about the audio, and ignorance
    must not pause the pipeline.

    Args:
        label: the raw language string, as the transcriber wrote it.

    Returns:
        (verdict, codes) where verdict is one of:

        ``"none"``
            No language claim was made (absent, empty, ``auto``, ``und``).
        ``"expected"``
            At least one recognized part is en/es/ca. A compound label
            containing an expected language is expected — ``ca-es-mixed`` is
            the operator code-switching, not foreign audio.
        ``"unexpected"``
            Every recognized part is a language we do not expect, and at least
            one part was recognized. This is the only verdict that flags.
        ``"unparseable"``
            A claim was made but nothing in it resolved to a language code.
            Reported in metrics, never flagged.

        ``codes`` lists the ISO-639-1 codes recovered, in order of appearance.
    """
    original = (label or "").strip()
    if original.lower() in _NON_CLAIMS:
        return "none", []

    # Drop a properly-cased BCP-47 region subtag first ('en-US' -> 'en'), so a
    # region never reads as a second language. Case is doing the work here:
    # Whisper's lowercase 'ca-es-mixed' is two languages, not a region, and
    # survives this step to be split as a compound below.
    raw = _REGION_SUFFIX_RE.sub(r"\1", original).lower()

    codes: list[str] = []
    parts = [atom for atom in _COMPOUND_SPLIT_RE.split(raw) if atom]
    for part in parts:
        code = _normalize_language_part(part)
        if code and code not in codes:
            codes.append(code)

    if not codes:
        return "unparseable", []
    if any(code in EXPECTED_LANGUAGES for code in codes):
        return "expected", codes
    return "unexpected", codes


def is_bracket_only(text: str) -> bool:
    """
    True when a transcript is entirely Whisper non-speech descriptors.

    Strips every ``[...]`` tag and checks whether any word survives. A
    transcript with real words alongside a tag — ``"Say things [babbling]"``
    — is NOT bracket-only: it has lexical content and stays subject to the
    language check like any other transcript. Only a transcript with nothing
    but tags (and surrounding whitespace) counts.

    An already-empty transcript returns False here on purpose: check 3
    (empty) is the one that reports "the transcriber returned no text at
    all", and callers should not run this check for text that never reaches
    it — see the early return in ``assess_transcript``.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    remainder = _BRACKET_TAG_RE.sub("", stripped)
    return not remainder.split()


def assess_transcript(
    text: str,
    duration_seconds: float | None = None,
    language: str | None = None,
) -> ClarityReport:
    """
    Measure a transcript against every clarity check.

    Args:
        text: the transcript as written by the transcriber.
        duration_seconds: audio duration, when known. Checks that need it are
            skipped when it is None — a missing measurement must not manufacture
            a flag, and must not manufacture a pass either.
        language: language code the transcriber detected, when it reports one.

    Returns:
        A ClarityReport. ``clear`` is True only when no check flagged.
    """
    findings: list[ClarityFinding] = []
    stripped = (text or "").strip()
    words = stripped.split()
    sentences = _sentences(stripped)
    metrics: dict = {
        "words": len(words),
        "sentences": len(sentences),
        "duration_seconds": duration_seconds,
        "language": language,
    }

    # --- CHECK 3: empty ---------------------------------------------------
    if not stripped:
        findings.append(
            ClarityFinding(
                check=3,
                name="empty transcript",
                reason="the transcriber returned no text at all",
            )
        )
        # Nothing else is measurable on an empty string.
        return ClarityReport(clear=False, findings=findings, metrics=metrics)

    # --- CHECK 1: repetition loop ----------------------------------------
    norm = [_normalize(s) for s in sentences]
    counts = collections.Counter(n for n in norm if n)
    duplicated = sum(c for c in counts.values() if c > 1)
    dup_fraction = duplicated / len(norm) if norm else 0.0
    metrics["duplicate_sentence_fraction"] = round(dup_fraction, 3)

    longest_run = 1
    run = 1
    run_value = ""
    for i in range(1, len(norm)):
        if norm[i] and norm[i] == norm[i - 1]:
            run += 1
            if run > longest_run:
                longest_run, run_value = run, sentences[i]
        else:
            run = 1
    metrics["longest_consecutive_repeat"] = longest_run

    ngrams = [
        " ".join(words[i : i + REPEAT_NGRAM_SIZE]).lower()
        for i in range(len(words) - REPEAT_NGRAM_SIZE + 1)
    ]
    ngram_counts = collections.Counter(ngrams)
    top_ngram, top_ngram_count = (
        ngram_counts.most_common(1)[0] if ngram_counts else ("", 0)
    )
    metrics["max_ngram_repeats"] = top_ngram_count

    if dup_fraction > MAX_DUPLICATE_SENTENCE_FRACTION:
        repeated_sentence = counts.most_common(1)[0][0] if counts else ""
        findings.append(
            ClarityFinding(
                check=1,
                name="repetition loop",
                reason=(
                    f"{dup_fraction:.0%} of sentences are duplicates "
                    f"(threshold {MAX_DUPLICATE_SENTENCE_FRACTION:.0%}) — "
                    "likely a hallucination on trailing silence"
                ),
                excerpt=repeated_sentence[:200],
            )
        )
    elif longest_run > MAX_CONSECUTIVE_REPEATS:
        findings.append(
            ClarityFinding(
                check=1,
                name="repetition loop",
                reason=(
                    f"one sentence repeats {longest_run} times back to back "
                    f"(threshold {MAX_CONSECUTIVE_REPEATS})"
                ),
                excerpt=run_value[:200],
            )
        )
    elif top_ngram_count > MAX_NGRAM_REPEATS:
        findings.append(
            ClarityFinding(
                check=1,
                name="repetition loop",
                reason=(
                    f"the phrase {top_ngram!r} repeats {top_ngram_count} times "
                    f"(threshold {MAX_NGRAM_REPEATS})"
                ),
                excerpt=top_ngram[:200],
            )
        )

    # --- CHECK 2: truncation / degenerate output --------------------------
    # Truncation means the transcript is short RELATIVE TO THE AUDIO. Word
    # count on its own cannot say that, so every branch here is gated on
    # duration. When duration is unknown we do not flag: see the note below.
    has_duration = duration_seconds is not None and duration_seconds > 0
    metrics["duration_known"] = bool(has_duration)

    if has_duration:
        wps = len(words) / duration_seconds
        metrics["words_per_second"] = round(wps, 2)
        is_short_clip = duration_seconds < TRUNCATION_DURATION_SECONDS
        metrics["short_clip"] = is_short_clip

        if is_short_clip:
            # A brief recording that produced text is a real short memo. The
            # operator records these deliberately — "Okay.", a name, a single
            # reminder — and 218 of them were wrongly paused by the previous
            # word-count rule. Nothing about a short clip is flagged here;
            # a short clip with NO text is check 3's business.
            pass
        else:
            if len(words) < MIN_WORDS:
                findings.append(
                    ClarityFinding(
                        check=2,
                        name="truncated",
                        reason=(
                            f"only {len(words)} words (threshold {MIN_WORDS}) "
                            f"for {duration_seconds:.0f}s of audio — likely a "
                            "dropped recording"
                        ),
                        excerpt=stripped[:200],
                    )
                )
            elif wps < MIN_WORDS_PER_SECOND:
                findings.append(
                    ClarityFinding(
                        check=2,
                        name="truncated",
                        reason=(
                            f"{len(words)} words over {duration_seconds:.0f}s is "
                            f"{wps:.2f} words/second (threshold "
                            f"{MIN_WORDS_PER_SECOND}) — most of the audio appears "
                            "to be missing from the transcript"
                        ),
                        excerpt=stripped[:200],
                    )
                )

        if sentences and duration_seconds / len(sentences) > MAX_SECONDS_PER_SENTENCE:
            findings.append(
                ClarityFinding(
                    check=2,
                    name="degenerate segmentation",
                    reason=(
                        f"{len(sentences)} sentence(s) span {duration_seconds:.0f}s "
                        f"(over {MAX_SECONDS_PER_SENTENCE:.0f}s each)"
                    ),
                    excerpt=stripped[:200],
                )
            )
    # Duration unknown: do not flag truncation.
    #
    # The default is deliberate and it is the permissive one. Truncation is
    # defined relative to duration, so without duration there is no
    # measurement to make — flagging anyway would be the same mistake as
    # treating an unreadable language label as a wrong language. Duration
    # resolves for essentially every real memo (the entity field, else
    # ffprobe on the source audio), so this path is rare, and when it is
    # taken the other three checks still run: an empty transcript, a
    # repetition loop, and an unexpected language all flag without duration.

    # --- CHECK 4: language mismatch ---------------------------------------
    # Only "unexpected" flags. "none" and "unparseable" are recorded and pass:
    # a label we cannot read is missing evidence, not adverse evidence.
    #
    # A bracket-only transcript is exempt regardless of the label: see the
    # CHECK 4 (bracket-only exemption) comment above for why this is not the
    # same as calling the transcript clear, and why no duration gate belongs
    # here — CHECK 2 already independently measures whether a bracket-only
    # transcript's duration makes it a genuine capture failure.
    bracket_only = is_bracket_only(stripped)
    metrics["bracket_only"] = bracket_only
    if bracket_only:
        language_verdict, language_codes = "non_linguistic", []
    else:
        language_verdict, language_codes = classify_language(language)
    metrics["language_verdict"] = language_verdict
    metrics["language_codes"] = language_codes
    if language_verdict == "unexpected":
        detected = ", ".join(language_codes)
        findings.append(
            ClarityFinding(
                check=4,
                name="unexpected language",
                reason=(
                    f"detected {language!r} ({detected}), which is not one of "
                    "the expected languages (en, es, ca)"
                ),
                excerpt=stripped[:200],
            )
        )

    return ClarityReport(clear=not findings, findings=findings, metrics=metrics)
