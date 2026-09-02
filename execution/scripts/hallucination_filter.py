#!/usr/bin/env python3
"""Post-transcription hallucination filter for live transcript chunks.

The loudness gate in ``live_transcript_tail.py`` has a structural ceiling. It
answers "was there sustained acoustic energy in this window", which is exactly
the wrong question for this failure: on 2026-09-01 a fabricated Georgian-script
chunk arrived at -31.6 dB, inside the operator's own verified speech range of
-28 to -37 dB. No threshold separates -31.6 dB noise from -31.2 dB speech,
because loudness is not the distinguishing property. Raising the gate far
enough to catch it would clip the operator's quietest verified line (-36.8 dB)
and still admit the fabrication.

So this filter looks at the transcription RESULT instead. It complements the
gate rather than replacing it: the gate still saves the API call on the class it
can address (true silence), and this catches what arrives past it.

Six signals, ordered by observed catch rate on real chunks:

1. ``language_mismatch`` — Whisper reports a detected language. The session's
   language is known. Observed fabrications came back as Georgian, Khmer,
   Chinese, Ukrainian, and Thai in recordings whose speech is English. Highest
   catch rate, and it is the only signal that catches a *fluent-looking*
   fabrication. Backed by a script check so it still fires when the detected
   language is missing or wrong: Whisper sometimes labels Georgian output "en".
2. ``degenerate_repetition`` — the same phrase 4+ times in one chunk. Observed:
   "1 tsp lemon juice" x4, "蒸汽" x8, "食品" x3. Real speech does not do this
   inside a 30-second window.
3. ``caption_boilerplate`` — "thank you for watching", "please subscribe" and
   variants, in several languages. Artifacts of the YouTube-caption training
   corpus that dominates Whisper's training data.
4. ``too_short_for_window`` — a 30s window yielding "P" or "you". Not speech.
5. ``foreign_diacritic`` — a Latin-script fabrication betrayed by a letter that
   does not occur in any language the session could plausibly be in. Observed
   2026-09-01 on the STREAMING path: "A widać o mnie." (Polish) arrived inside
   an English session. Script-family checks cannot see this — Polish is Latin
   script — so signal 1b passes it through. Measured over 629 real chunks that
   the script check does not catch: 4 flagged, all 4 genuine fabrications, 0
   false positives.
6. ``low_confidence`` — the decoder's own mean token logprob. The only signal
   that sees a Latin-script fabrication whose orthography is entirely ordinary
   ("Bitte.", "Hallo.", "Rio.", "Takk."), which every text-based signal above
   structurally cannot. Requires the session to request logprobs; when they are
   absent the signal is skipped and nothing else changes. Held-out measurement
   (different audio, frozen threshold): 25/25 Latin-script fabrications caught,
   0/50 genuine utterances lost, AUC 1.000, classes non-overlapping. See
   MIN_MEAN_LOGPROB.

A further signal, ``lone_word_turn``, was built and then REJECTED on measurement.
The reasoning was that server VAD closes a turn on silence, so a complete turn
of one bare word is noise — and it did catch "Soita." (Finnish) and "Utanfor."
(Norwegian). But measured against the operator's streaming captures it also
filtered "Eighteen" and "seventeen" (a counting test), "root", "system" and
"that": 5 false positives against 2 true positives. Single-word utterances are
ordinary in dictation and in technical speech, and a filter that eats them is
worse than the fabrications it removes. Kept here as a note so it is not
rediscovered and reintroduced: a lone word is NOT evidence of fabrication.

Another, ``short_turn_duration``, was measured on 2026-09-01 and REJECTED for
the same reason. On one capture it looked decisive — both fabrications there ran
under 1.02s while most real turns ran 3s+. Across the corpus the classes do not
separate: the shortest REAL turn is "As we go." at 1.03s, two hundredths of a
second longer than the "Nein." fabrication. A floor low enough to spare real
speech catches nothing that ``script_mismatch`` has not already caught, and by
1.5s it is eating whole English questions. Duration is NOT the discriminator,
and there is no send-path floor to place either: the client streams fixed-size
PCM payloads gated on RMS, so spans are the server's OUTPUT, not the client's
input — a turn's duration does not exist until after it has been transcribed.

A caution for whoever measures next. The capture corpus is NOT uniformly
current: rows written before a since-fixed bug can silently argue for or
against a signal on the strength of data that can no longer occur. Concretely,
the three zero-duration rows in the 1302 and 1304 captures ("Thank God.",
"An bhfuil sé sin maith anois?", "orda finish form diurtama?") are artifacts of
the pre-#631 boundary defect, where both ends of a span fell back to the same
value — the exact 91.78-91.78 shape that commit fixed. They read as damning
evidence for a duration floor and are nothing of the kind. Before a duration or
boundary signal is judged on corpus rows, check which captures predate the fix
and exclude them.

**Nothing is ever silently dropped.** A caught chunk keeps its text and gains a
``filtered`` reason, so a false positive stays recoverable by eye and the
filter's own accuracy stays measurable against the JSONL. Silently discarding a
chunk would reproduce the exact defect class this filter exists to fix.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

# --- 1. Language ------------------------------------------------------------

# Whisper returns language as either an ISO code ("en") or an English name
# ("english"), depending on model and path. Normalize both.
_LANGUAGE_ALIASES = {
    "en": "en", "eng": "en", "english": "en",
    "es": "es", "spa": "es", "spanish": "es", "castilian": "es",
    "ca": "ca", "cat": "ca", "catalan": "ca", "valencian": "ca",
    "fr": "fr", "fra": "fr", "fre": "fr", "french": "fr",
    "de": "de", "deu": "de", "ger": "de", "german": "de",
    "pt": "pt", "por": "pt", "portuguese": "pt",
    "it": "it", "ita": "it", "italian": "it",
}


def normalize_language(value: str | None) -> str | None:
    """Fold a Whisper language label to an ISO-639-1 code, or None if unknown.

    ``"auto"`` and empty values normalize to None — the tailer must not treat
    "we don't know" as a mismatch, or every unlabelled chunk becomes a false
    positive.
    """
    if not value:
        return None
    key = value.strip().lower().replace("_", "-")
    if key in ("auto", "unknown", "none"):
        return None
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]
    # "en-US" -> "en"
    base = key.split("-", 1)[0]
    return _LANGUAGE_ALIASES.get(base, base or None)


# Scripts that cannot appear in Latin-alphabet speech. Detected from the text
# itself, so the check still fires when Whisper mislabels the language — which
# it does: the Georgian chunk of 2026-09-01 is not reliably labelled "ka".
_NON_LATIN_SCRIPTS = (
    "GEORGIAN", "KHMER", "CJK", "HIRAGANA", "KATAKANA", "HANGUL",
    "CYRILLIC", "THAI", "ARABIC", "HEBREW", "DEVANAGARI", "LAO",
    "MYANMAR", "ETHIOPIC", "ARMENIAN", "TAMIL", "TELUGU", "BENGALI",
)

# Latin-script sessions. A non-Latin script in one of these is fabrication,
# whatever the reported language says.
_LATIN_SCRIPT_LANGUAGES = {"en", "es", "ca", "fr", "de", "pt", "it", "nl", "sv", "da", "no", "fi", "pl", "cs", "tr", "id", "vi"}

# A stray emoji or a single borrowed glyph is not a fabrication. Real fabricated
# chunks are dominated by the foreign script.
NON_LATIN_SCRIPT_RATIO = 0.20


def script_families(text: str) -> Counter:
    """Count characters per script family, ignoring punctuation and digits."""
    counts: Counter = Counter()
    for ch in text:
        if not ch.isalpha():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        for family in _NON_LATIN_SCRIPTS:
            if name.startswith(family) or f" {family} " in f" {name} ":
                counts[family] += 1
                break
        else:
            if name.startswith("LATIN"):
                counts["LATIN"] += 1
    return counts


def non_latin_ratio(text: str) -> float:
    """Share of alphabetic characters belonging to a non-Latin script."""
    counts = script_families(text)
    total = sum(counts.values())
    if not total:
        return 0.0
    return (total - counts.get("LATIN", 0)) / total


# --- 2. Degenerate repetition ----------------------------------------------

# Four is the observed floor: "1 tsp lemon juice" x4 is the mildest real case,
# and genuine speech does not repeat a phrase four times back to back.
REPETITION_THRESHOLD = 4

# The repetition must be CONSECUTIVE. Measured against 728 real chunks: scoring
# a phrase's total occurrences anywhere in the window flags 106 chunks, nearly
# all of them genuine bilingual conversation, because ordinary speech reuses
# "porque", "de la", "me da pereza" freely across a 35-second window. Whisper's
# degenerate decoding loop is different in kind — it emits the same phrase back
# to back with nothing between ("つづく つづく つづく つづく"), which ordinary
# speech does not do. Requiring adjacency is what separates the two.
# Two further calibrations, both forced by real speech in the corpus:
#
#  - A repeated SINGLE token is not a signal at all. Real speakers do it for
#    emphasis and rhythm — "No, no, no, no", "pum pum pum pum pum", "chica,
#    chica, chica, chica" are all the operator or his counterpart, verbatim. The
#    Whisper loop always repeats a phrase, so requiring 2+ tokens costs nothing
#    and removes every one of those false positives.
#  - A long phrase repeated 3x is likewise real: "I don't know why. I don't know
#    why. I don't know why." and "Can you hear me now?" x3 are both the operator
#    testing his own mic. 4x is where genuine speech stops and the decoding loop
#    starts.
MIN_REPEATED_PHRASE_TOKENS = 2

_TOKEN_RE = re.compile(r"[^\s]+")


def max_consecutive_repetition(text: str, *, max_phrase_len: int = 8) -> tuple[int, int]:
    """Longest run of a phrase repeated BACK TO BACK.

    Returns ``(run_length, phrase_tokens)``. A run of 1 means no repetition.
    Adjacency is the whole point — see REPETITION_THRESHOLD above for why the
    "count occurrences anywhere" variant was rejected on measured data.
    """
    tokens = [t.strip(".,!?;:。，¡!¿?\"'()").lower() for t in _TOKEN_RE.findall(text)]
    tokens = [t for t in tokens if t]
    if not tokens:
        return (0, 0)

    best_run, best_len = 1, 1
    for n in range(MIN_REPEATED_PHRASE_TOKENS, max_phrase_len + 1):
        if len(tokens) < 2 * n:
            break
        i = 0
        while i + n <= len(tokens):
            phrase = tokens[i:i + n]
            run = 1
            j = i + n
            while j + n <= len(tokens) and tokens[j:j + n] == phrase:
                run += 1
                j += n
            if run > 1 and (run > best_run or (run == best_run and n > best_len)):
                best_run, best_len = run, n
            i += 1
    return (best_run, best_len)


# The repeated run must also account for most of the chunk. A person chanting
# "sí, sí, sí, sí" or "chica, chica, chica, chica" inside a paragraph of real
# conversation is speech; a chunk that is ALMOST NOTHING BUT the repeated phrase
# is the decoding loop. Measured: without this, three genuine chunks are
# flagged — a child chanting, an emphatic run of agreement, and the operator
# testing his own microphone — and no additional fabrication is caught, because
# the fabricated repetition cases in the corpus are non-Latin script and already
# caught upstream.
REPETITION_DOMINANCE_RATIO = 0.6


def is_degenerate_repetition(text: str) -> tuple[bool, str]:
    """True when the text shows Whisper's back-to-back decoding loop."""
    run, phrase_len = max_consecutive_repetition(text)
    if run < REPETITION_THRESHOLD:
        return False, ""

    total_tokens = len([t for t in _TOKEN_RE.findall(text) if t.strip(".,!?;:")])
    if total_tokens and (run * phrase_len) < REPETITION_DOMINANCE_RATIO * total_tokens:
        return False, ""

    return True, f"a {phrase_len}-token phrase repeats {run}x back to back"


# --- 3. Caption boilerplate -------------------------------------------------

# Whisper's training corpus is saturated with YouTube captions, so silence and
# noise decode to their closing credits.
#
# The boilerplate must DOMINATE the chunk, not merely appear in it. Measured on
# 730 real chunks: Whisper spliced "Amara.org" into the middle of a genuine
# 35-second stretch of Spanish conversation, and appended "Thank you for
# watching." to several real English sentences. Filtering those whole chunks
# would discard minutes of real discussion to remove a fabricated tail — the
# wrong trade, and one the operator cannot undo. A chunk that is *entirely*
# caption boilerplate is fabrication; a real chunk with a boilerplate splice is
# a real chunk and stays, tail included.
#
# Dominance is measured per SENTENCE, not per character, because the artifact
# arrives as whole sentences: "If you like this video, don't forget to like it
# and subscribe to my channel." is one sentence of pure boilerplate whose
# matched substring covers only a third of its characters.
BOILERPLATE_DOMINANCE_RATIO = 0.5

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n+")

_BOILERPLATE_PATTERNS = (
    r"thanks?\s+(?:you\s+)?(?:very\s+much\s+)?for\s+watching",
    r"thank\s+you\s+for\s+watching",
    r"please\s+subscribe",
    r"don'?t\s+forget\s+to\s+subscribe",
    r"subscribe\s+to\s+(?:my|our|the)\s+channel",
    r"like\s+and\s+subscribe",
    r"see\s+you\s+(?:in\s+)?(?:the\s+)?next\s+(?:video|time)",
    r"thanks?\s+for\s+listening",
    r"^\W*thank\s+you\W*$",          # a bare "Thank you." IS the artifact
    r"^\W*thanks\W*$",
    r"^\W*bye\W*(?:bye\W*)?$",
    r"^\W*you\W*$",
    r"^\W*ご視聴ありがとう",   # ご視聴ありがとう
    r"доскорого",  # до скорого
    r"Подпишись",  # Подпишись
    r"amara\.org",
    r"subtitles?\s+by",
    # Further variants observed in the corpus, all on chunks with no speech.
    r"if\s+you\s+like\s+this\s+video",
    r"(?:please\s+)?leave\s+a\s+comment",
    r"share\s+this\s+video\s+with",
    r"music\s+outro",
    r"hit\s+the\s+(?:like|bell)",
    r"don'?t\s+forget\s+to\s+like",
)
_BOILERPLATE_RE = [re.compile(p, re.IGNORECASE) for p in _BOILERPLATE_PATTERNS]


def is_caption_boilerplate(text: str) -> bool:
    """True when caption boilerplate accounts for most of the chunk.

    Dominance, not presence — see BOILERPLATE_DOMINANCE_RATIO.
    """
    stripped = " ".join(text.split())
    if not stripped:
        return False

    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(stripped) if s.strip()]
    if not sentences:
        return False

    boilerplate_chars = sum(
        len(s) for s in sentences
        if any(p.search(s) for p in _BOILERPLATE_RE)
    )
    return boilerplate_chars >= BOILERPLATE_DOMINANCE_RATIO * len(stripped)


# --- 4. Too short for the window -------------------------------------------

# A 30s window that decodes to "P" carries no speech. Scaled by window length so
# a deliberately short --interval does not start filtering genuinely short utterances.
MIN_CHARS_PER_LONG_WINDOW = 12
LONG_WINDOW_SECONDS = 20.0


def is_too_short_for_window(text: str, window_seconds: float | None) -> bool:
    if window_seconds is None or window_seconds < LONG_WINDOW_SECONDS:
        return False
    return len(text.strip()) < MIN_CHARS_PER_LONG_WINDOW


def has_no_letters(text: str) -> bool:
    """True when the output carries no letters AND no digits.

    Observed as pure emoji runs ("🔥🔥🔥🔥🔥", "🔪🔪🔪🔪🔪🔪🔪"). Whisper emits these
    from noise. Independent of window length, because no window length makes a
    row of knife emoji into speech.

    Digits count as speech content. An earlier revision asked only "are there
    letters", which made every purely numeric utterance a fabrication: "42",
    "$1,500", "2026", "3.14159". That fired on real speech on 2026-09-01 — the
    operator's counting test transcribed as "1, 2, 3, 4, 5, 6, 7, 8" and was
    filtered as an emoji run, the sole false positive in 837 captured turns.

    Numbers are ordinary in dictation, and the streaming path exists for
    technical and financial speech where they are the whole point. Requiring
    the absence of BOTH letters and digits keeps all three emoji runs in the
    corpus and passes every numeric turn.
    """
    stripped = text.strip()
    return bool(stripped) and not any(ch.isalpha() or ch.isdigit() for ch in stripped)


# --- 5. Foreign diacritics (Latin-script fabrication) -----------------------

# Signal 1b asks "is this a non-Latin script". That question cannot see a
# Latin-script fabrication: Polish, Finnish, Turkish and Indonesian are all
# Latin. On 2026-09-01 the streaming path produced "A widać o mnie." inside an
# English session, and the script check passed it through at 0% non-Latin.
#
# What separates it is ORTHOGRAPHY, not script. `ć` does not occur in English,
# Spanish, Catalan, French, German, Portuguese or Italian — the only languages
# this operator's sessions are ever plausibly in. A letter from outside that
# union is evidence the decoder wandered into a language nobody is speaking.
#
# The allowed set is deliberately the UNION over the session's plausible
# languages rather than just the expected one, because real speech code-switches
# and a Spanish "ñ" inside an English session is the operator, not Whisper.
# Getting this wrong in the strict direction would filter genuine bilingual
# speech, which is the more costly error.
_LATIN_DIACRITICS_BY_LANGUAGE = {
    "en": "",
    "es": "áéíóúüñ¿¡ºª",
    "ca": "àèéíïòóúüçl·",
    "fr": "àâäçéèêëîïôöùûüÿœæ",
    "de": "äöüß",
    "pt": "ãáàâçéêíõóôú",
    "it": "àèéìíîòóùú",
    "nl": "ëïéèü",
}

# Sessions are assumed to admit the operator's own languages even when the
# expected language is narrower, so code-switching never reads as fabrication.
DEFAULT_PLAUSIBLE_LANGUAGES = ("en", "es", "ca", "fr", "de", "pt", "it")


def _allowed_diacritics(languages: tuple[str, ...]) -> set[str]:
    allowed: set[str] = set()
    for lang in languages:
        chars = _LATIN_DIACRITICS_BY_LANGUAGE.get(lang, "")
        allowed.update(chars)
        allowed.update(chars.upper())
    return allowed


def foreign_diacritics(
    text: str, *, languages: tuple[str, ...] = DEFAULT_PLAUSIBLE_LANGUAGES
) -> list[str]:
    """Letters outside the orthography of every plausible session language.

    Only non-ASCII alphabetic characters are considered, so ordinary English
    text can never trigger this. Returns the offending characters so the
    recorded reason names the actual evidence.
    """
    allowed = _allowed_diacritics(languages)
    seen: list[str] = []
    for ch in text:
        if not ch.isalpha() or ord(ch) < 128:
            continue
        if ch not in allowed and ch not in seen:
            seen.append(ch)
    return seen


# --- 6. Decoder confidence (mean token logprob) -----------------------------

# The signals above all ask a question about the TEXT: what alphabet, what
# orthography, what repetition. That family has a structural ceiling on the
# Latin-script gap, because a fabricated "Bitte." and a genuine "Hello." are
# orthographically indistinguishable — both are well-formed Latin words the
# session's languages admit.
#
# This signal asks a different question: how confident was the decoder. A
# fabrication is the model resolving noise into the nearest plausible token
# sequence, and it is measurably unsure while doing so. Genuine speech is not.
#
# MEASURED against the live API, not inferred from documentation. The Realtime
# session exposes per-token logprobs when the session declares
# ``include: ["item.input_audio_transcription.logprobs"]``; the REST
# transcription endpoint exposes the same via ``include[]=logprobs``. An earlier
# conclusion in this workstream held that no per-segment confidence existed —
# that was wrong, and it was wrong because it was never tested against the
# socket.
#
# Separation on a held-out set (different audio sources, different seed, this
# threshold frozen before scoring): 25/25 Latin-script fabrications caught,
# 0/50 genuine utterances lost, AUC 1.000. The classes do not overlap — the
# worst fabrication scored -2.08, the worst genuine utterance -0.81, a margin of
# 1.27 nats. That margin is why this is a usable threshold and Silero's p90
# AUC 0.644 was not.
#
# -1.5 sits inside the empty band rather than hard against either class, so
# neither an unusually mumbled real utterance nor an unusually fluent
# fabrication lands on the boundary. It is deliberately NOT tuned to the last
# decimal: a threshold fitted tightly to one corpus is a threshold that moves
# when the audio does.
MIN_MEAN_LOGPROB = -1.5

# Below this token count the mean is dominated by a single token's value and
# stops being an average of anything. Short genuine utterances ("Cool.", "Yes.")
# are ordinary in conversation, so a one-token mean must not be allowed to
# convict them. Such rows fall through to the text-based signals above.
MIN_LOGPROB_TOKENS = 2


def mean_token_logprob(logprobs: list | None) -> tuple[float | None, int]:
    """Mean logprob over the tokens of one transcription result.

    Accepts the API's list of ``{"token", "logprob", "bytes"}`` entries and
    returns ``(mean, n_tokens)``, or ``(None, 0)`` when the caller did not
    request logprobs — in which case this signal is simply skipped, so a session
    that does not send ``include`` behaves exactly as it did before.
    """
    if not logprobs:
        return (None, 0)
    values = [
        entry.get("logprob")
        for entry in logprobs
        if isinstance(entry, dict) and entry.get("logprob") is not None
    ]
    if not values:
        return (None, 0)
    return (sum(values) / len(values), len(values))


# --- Filter -----------------------------------------------------------------


@dataclass(frozen=True)
class FilterVerdict:
    """Outcome of screening one transcription result.

    ``reason`` is None when the chunk passes. It is never a bare boolean, so the
    JSONL records *why* — which is what makes a false positive diagnosable and
    the filter's accuracy measurable after the fact.
    """

    filtered: bool
    reason: str | None = None
    detail: str | None = None


PASS = FilterVerdict(False)


def screen_transcription(
    text: str,
    *,
    expected_language: str | None = "en",
    detected_language: str | None = None,
    window_seconds: float | None = None,
    vad_closed: bool = False,  # noqa: ARG001 — see the rejected lone_word_turn note
    plausible_languages: tuple[str, ...] = DEFAULT_PLAUSIBLE_LANGUAGES,
    logprobs: list | None = None,
) -> FilterVerdict:
    """Screen one transcription result for hallucination signatures.

    Returns a verdict; the caller marks the record and keeps the text. Order is
    by catch rate, so the recorded reason names the strongest signal present.

    ``vad_closed`` says whether server VAD closed this turn. No signal currently
    uses it — the one that did (``lone_word_turn``) was rejected on measurement,
    see the module docstring. It stays in the signature because callers on the
    streaming path already know the answer and a future signal may need it.

    ``logprobs`` is the API's per-token confidence list, when the session asked
    for it. Omitted or empty, signal 6 is skipped and every other signal behaves
    exactly as before — so this parameter is additive, never a change in
    existing verdicts.
    """
    stripped = (text or "").strip()
    if not stripped:
        return PASS  # empty is the silence path's business, not ours

    expected = normalize_language(expected_language)
    detected = normalize_language(detected_language)

    # 1a. Reported language mismatch.
    if expected and detected and detected != expected:
        return FilterVerdict(
            True, "language_mismatch",
            f"detected {detected!r}, session is {expected!r}",
        )

    # 1b. Script mismatch — fires even when the language label is absent or
    # wrong, which is the common case for the fabrications observed.
    if expected in _LATIN_SCRIPT_LANGUAGES or (expected is None and detected is None):
        ratio = non_latin_ratio(stripped)
        if ratio >= NON_LATIN_SCRIPT_RATIO:
            families = [f for f, _ in script_families(stripped).most_common() if f != "LATIN"]
            return FilterVerdict(
                True, "script_mismatch",
                f"{ratio:.0%} non-Latin ({', '.join(families[:3]).lower()})",
            )

    # 1c. Foreign diacritics — catches the Latin-script fabrication that 1b
    # structurally cannot see (Polish, Finnish, Turkish are all Latin script).
    exotic = foreign_diacritics(stripped, languages=plausible_languages)
    if exotic:
        return FilterVerdict(
            True, "foreign_diacritic",
            f"{''.join(exotic[:5])!r} outside {'/'.join(plausible_languages)}",
        )

    # 2. Degenerate repetition.
    degenerate, detail = is_degenerate_repetition(stripped)
    if degenerate:
        return FilterVerdict(True, "degenerate_repetition", detail)

    # 3. Caption boilerplate.
    if is_caption_boilerplate(stripped):
        return FilterVerdict(
            True, "caption_boilerplate", stripped[:60],
        )

    # 4a. No letters at all — an emoji run, not speech.
    if has_no_letters(stripped):
        return FilterVerdict(True, "no_speech_content", "no alphabetic characters")

    # 4b. Too short for a long window.
    if is_too_short_for_window(stripped, window_seconds):
        return FilterVerdict(
            True, "too_short_for_window",
            f"{len(stripped)} chars in a {window_seconds:.0f}s window",
        )

    # 5. Low decoder confidence — the only signal that sees a Latin-script
    # fabrication whose orthography is entirely ordinary ("Bitte.", "Hallo.",
    # "Rio."). Last because it is the only one needing data the caller may not
    # have; when logprobs are absent it is skipped and the verdict is unchanged.
    mean_logprob, n_tokens = mean_token_logprob(logprobs)
    if (
        mean_logprob is not None
        and n_tokens >= MIN_LOGPROB_TOKENS
        and mean_logprob < MIN_MEAN_LOGPROB
    ):
        return FilterVerdict(
            True, "low_confidence",
            f"mean logprob {mean_logprob:.2f} over {n_tokens} tokens "
            f"(floor {MIN_MEAN_LOGPROB})",
        )

    return PASS
