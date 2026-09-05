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

Thresholds are calibrated against the three memos recorded 2026-09-05, whose
measurements are recorded in ``test_transcript_clarity.py`` alongside the
fixtures. Two are substantive and must pass; one is a 2-second false start and
one carries a hallucinated repetition loop, and both must flag.
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
# the"). The shortest substantive memo is 103 words over 54.7s. A memo under
# 15 words is not something the operator meant to file.
MIN_WORDS = 15  # observed: 5 flagged vs 103 and 864 passing
MIN_DURATION_SECONDS = 5.0  # observed: 2.0s flagged vs 54.7s and 512.9s

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

# CHECK 4 — language mismatch.
# The operator speaks English, Spanish and Catalan. Anything else is worth a
# pause rather than a guess. An absent/auto language is not a mismatch — we
# only flag a positively-detected unexpected language.
EXPECTED_LANGUAGES = frozenset({"en", "es", "ca", "english", "spanish", "catalan"})

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


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
    if len(words) < MIN_WORDS:
        findings.append(
            ClarityFinding(
                check=2,
                name="truncated",
                reason=(
                    f"only {len(words)} words (threshold {MIN_WORDS}) — "
                    "likely a false start or a dropped recording"
                ),
                excerpt=stripped[:200],
            )
        )

    if duration_seconds is not None and duration_seconds > 0:
        wps = len(words) / duration_seconds
        metrics["words_per_second"] = round(wps, 2)
        if duration_seconds < MIN_DURATION_SECONDS:
            findings.append(
                ClarityFinding(
                    check=2,
                    name="truncated",
                    reason=(
                        f"the recording is only {duration_seconds:.1f}s "
                        f"(threshold {MIN_DURATION_SECONDS:.0f}s)"
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

    # --- CHECK 4: language mismatch ---------------------------------------
    if language:
        code = language.strip().lower()
        if code not in ("", "auto") and code not in EXPECTED_LANGUAGES:
            findings.append(
                ClarityFinding(
                    check=4,
                    name="unexpected language",
                    reason=(
                        f"detected {language!r}, which is not one of the "
                        "expected languages (en, es, ca)"
                    ),
                    excerpt=stripped[:200],
                )
            )

    return ClarityReport(clear=not findings, findings=findings, metrics=metrics)
