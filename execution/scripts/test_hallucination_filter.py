"""Tests for the post-transcription hallucination filter.

Every fabrication and every genuine utterance below is VERBATIM from the
operator's own live JSONL of 2026-08-28, 2026-08-31, and 2026-09-01. Nothing is
invented: a filter tuned on synthetic examples proves nothing about the
fabrications Whisper actually produces, and — more importantly — nothing about
the real speech it must not touch.

Both directions are asserted. A filter that rejects genuine speech is worse than
no filter at all, because the operator loses words he actually said and has no
way to know it happened.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from hallucination_filter import (  # noqa: E402
    foreign_diacritics,
    is_caption_boilerplate,
    is_degenerate_repetition,
    max_consecutive_repetition,
    non_latin_ratio,
    normalize_language,
    screen_transcription,
)

# --------------------------------------------------------------------------
# Real fabrications, captured from the operator's live JSONL.
# --------------------------------------------------------------------------

# The decisive counterexample from issue #619 comment 6: this arrived at
# -31.6 dB, squarely inside the operator's verified speech range of -28 to
# -37 dB. No loudness threshold can separate it from real speech.
GEORGIAN_AT_SPEECH_LOUDNESS = (
    "ლ ალ�네!!!! კილე ლან ავალე ც���იოთჳკ ოოიანი ნსოყა� "
    "hazard აუვშღე არფოდი? წლი 🌰"
)

REAL_FABRICATIONS = [
    GEORGIAN_AT_SPEECH_LOUDNESS,
    # Khmer recipe text, -40.6 dB
    "អទបាវគគុ់ ខិបប! Green papaya អូមវន ជរីតើង ឡស៦យមួយើអDAY�ខ្មួន្᠈ម។ "
    "mix Green papaya green papaya អូមៜន័។ អូមៜន័។ អូមៜ។",
    # Chinese food text, -45.1 dB
    "提供 多種類型的食品 包括 菜 和 牛肉 。 食品 所有食品",
    # Ukrainian YouTube sign-off
    "Дякую за перегляд і до зустрічі у наступному відео!",
    # Japanese caption boilerplate — the single most frequent fabrication
    "ご視聴ありがとうございました",
    "チャンネル登録をお願いいたします。",
    # Thai / mixed-script recipe gibberish
    "200 ml หาาฟ 200 ml 탄iten 200 ml tánit 250 ml สṛ 400 กิน seek",
    # Degenerate repetition, Japanese
    "つづく つづく つづく つづく",
    # Degenerate repetition, Chinese ("蒸汽" x8 in the issue)
    "通常,如果您使用蒸汽,您将需要使用蒸汽。 如果您利用蒸汽,您将需要使用蒸汽。 "
    "蒸汽是比较容易制作的。 蒸汽将让您的皮盖住其余部分。",
    # English caption boilerplate
    "Thank you for watching.",
    "Thanks for watching.",
    "Please subscribe to my channel.",
    "If you like this video, don't forget to like it and subscribe to my channel.",
    "📢 Share this video with your friends on social media.",
    "🎶 Music Outro 🎶",
    # Bare artifacts on a full-length window
    "Thank you.",
    "you",
    "P",
    # Pure emoji runs
    "🔥🔥🔥🔥🔥",
    "🔪🔪🔪🔪🔪🔪🔪",
    "😍😍😍😍😍",
]

# --------------------------------------------------------------------------
# Real speech, same recordings, same microphone, same sessions.
# --------------------------------------------------------------------------

REAL_SPEECH = [
    # -30.5 dB, immediately before the Georgian fabrication above
    "Review the session history in general to see if there are more bugs that "
    "you should be fixing directly, or not directly, but via dispatch from the "
    "session.",
    # -28.1 dB
    "Actually, I don't know, the recording shouldn't be optional, we should "
    "have it, but maybe we should find a way to store the recording directly in "
    "some sort of cheap cloud storage.",
    # -30.4 dB
    "Alright, I've changed your permissions to bypass permissions, which should "
    "help with the merge permission question.",
    # -36.8 dB — the operator's QUIETEST verified line, and it repeats a phrase
    # three times. Both properties make it the hardest true negative in the set.
    "Having a recording, I guess, is optional. It's also going to take up a lot "
    "of space. I don't know why. I don't know why. I don't know why.",
    # The operator testing his own microphone — repetition that is real speech
    "Can you hear me now? Can you hear me now? Can you hear me now? Just let me "
    "know when you can hear me, okay?",
    # Long-form English about the swarm
    "I'm actually not sure that the default root view of the dashboard should "
    "be the current session because the work we're doing here should be "
    "essentially beyond the session.",
    "Objectives should capture some sort of benefit or material outcome, not "
    "output or work-to-complete per se.",
    # Genuine Spanish conversation — the filter must be language-agnostic about
    # real speech, and this is where an occurrence-counting repetition detector
    # produced 60+ false positives during calibration.
    "No, no, hablamos de otra forma que como escribimos. Porque estudiando "
    "somos más precisos, pero a veces no decir un poco de todo. No, yo acorto, "
    "porque me da pereza escribir. Me da pereza y también es más rápido.",
    "Es que aquí no se puede entrar No, no, no, ahora sí ¿Por qué? Para "
    "trabajar ¿Para qué? Para trabajar, amigo ¿Por qué? Te lo diré después, "
    "¿vale?",
    # A child chanting inside real conversation
    "Yo le doy 10 minutos más y ya está. Venga. ¿Nos dejas, cariño? Venga, "
    "para allá. 1, 2, 3, 4, 5, 6, 7, 8, 9, 10. Chico feo. Chica, chica, chica, "
    "chica. Vale. Entonces, hemos hablado de los estados, clientes, buzón.",
    # Emphatic agreement — "sí sí sí sí" is speech, not a decoding loop
    "esto es super te gusta? si si si no no no es que esto me ayuda yo pienso "
    "si si si si si si si si si",
]


@pytest.mark.parametrize("text", REAL_FABRICATIONS)
def test_real_fabrications_are_caught(text):
    """Every fabrication actually captured from the operator's mic is flagged."""
    verdict = screen_transcription(
        text, expected_language="en", window_seconds=35.0
    )
    assert verdict.filtered, f"missed a real fabrication: {text[:80]!r}"
    assert verdict.reason, "a filtered chunk must always record WHY"


@pytest.mark.parametrize("text", REAL_SPEECH)
def test_real_speech_survives(text):
    """No genuine utterance from the same recordings is filtered.

    This is the load-bearing half. A filter that eats real speech costs the
    operator words he actually said, silently.
    """
    verdict = screen_transcription(
        text, expected_language="en", window_seconds=35.0
    )
    assert not verdict.filtered, (
        f"FALSE POSITIVE on genuine speech ({verdict.reason}): {text[:80]!r}"
    )


def test_the_georgian_counterexample_is_caught_without_a_language_label():
    """The issue's decisive case, with the label Whisper does not reliably give.

    Comment 6 of #619 proves the loudness gate cannot catch this. The script
    check must therefore stand on its own, with no help from a detected-language
    field.
    """
    verdict = screen_transcription(
        GEORGIAN_AT_SPEECH_LOUDNESS,
        expected_language="en",
        detected_language=None,
        window_seconds=35.0,
    )
    assert verdict.filtered
    assert verdict.reason == "script_mismatch"


def test_language_mismatch_fires_when_whisper_reports_one():
    verdict = screen_transcription(
        "Some perfectly ordinary looking sentence.",
        expected_language="en",
        detected_language="ka",
        window_seconds=35.0,
    )
    assert verdict.filtered
    assert verdict.reason == "language_mismatch"


def test_matching_language_does_not_fire():
    verdict = screen_transcription(
        "Some perfectly ordinary looking sentence.",
        expected_language="en",
        detected_language="english",
        window_seconds=35.0,
    )
    assert not verdict.filtered


def test_unknown_language_is_not_a_mismatch():
    """'auto' means we do not know — it must never be read as a mismatch."""
    for label in (None, "", "auto", "unknown"):
        verdict = screen_transcription(
            "An ordinary English sentence about the dashboard.",
            expected_language="en",
            detected_language=label,
            window_seconds=35.0,
        )
        assert not verdict.filtered, f"{label!r} treated as a mismatch"


def test_spanish_session_keeps_spanish_speech():
    """The expected language is configurable; a Spanish session keeps Spanish."""
    verdict = screen_transcription(
        "Entonces, hemos hablado de los estados, clientes, buzón.",
        expected_language="es",
        detected_language="es",
        window_seconds=35.0,
    )
    assert not verdict.filtered


def test_boilerplate_spliced_into_real_speech_is_kept():
    """A real sentence with a fabricated tail keeps its real half.

    Observed repeatedly: Whisper appends "Thank you for watching." to genuine
    English. Filtering the whole chunk would discard the operator's actual words
    to remove the tail — a trade that loses more than it saves.
    """
    verdict = screen_transcription(
        "or should we organize those related tasks into projects? "
        "Thank you for watching.",
        expected_language="en",
        window_seconds=35.0,
    )
    assert not verdict.filtered


def test_a_short_utterance_in_a_short_window_is_not_filtered():
    """The length signal scales with the window; a 5s chunk may legitimately be short."""
    assert not screen_transcription("Yes.", expected_language="en",
                                    window_seconds=4.0).filtered
    assert screen_transcription("Yes.", expected_language="en",
                                window_seconds=35.0).filtered


def test_empty_text_is_not_the_filters_business():
    """Silence belongs to the loudness gate, not here."""
    assert not screen_transcription("", expected_language="en",
                                    window_seconds=35.0).filtered


# --------------------------------------------------------------------------
# Component-level pins for the calibrations the measured data forced.
# --------------------------------------------------------------------------


def test_consecutive_repetition_requires_adjacency():
    """Occurrences scattered through a window are not a decoding loop."""
    scattered = "porque a porque b porque c porque d"
    assert max_consecutive_repetition(scattered)[0] == 1
    adjacent = "つづく つづく つづく つづく"
    assert max_consecutive_repetition(adjacent)[0] >= 2


def test_repetition_must_dominate_the_chunk():
    """A repeated phrase inside a paragraph of real speech is not degenerate."""
    embedded = (
        "Venga, para allá. Chica, chica, chica, chica. Vale. Entonces, hemos "
        "hablado de los estados, clientes, asisto, buzón, y de las incidencias."
    )
    assert not is_degenerate_repetition(embedded)[0]

    dominant = "Додати 1 ч.л. молока. Додати 1 ч.л. молока. Додати 1 ч.л. молока. Додати 1 ч.л. молока."
    assert is_degenerate_repetition(dominant)[0]


def test_boilerplate_requires_dominance_not_presence():
    assert is_caption_boilerplate("Thank you for watching.")
    assert not is_caption_boilerplate(
        "We need a discipline to make sure that objectives are well defined, "
        "and the swarm should dispatch these itself. Thank you for watching."
    )


def test_non_latin_ratio_ignores_a_stray_glyph():
    """One borrowed character in an English sentence is not a fabrication."""
    assert non_latin_ratio("The word is 蒸 in Chinese, apparently") < 0.20
    assert non_latin_ratio("提供 多種類型的食品 包括 菜 和 牛肉") >= 0.20


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("en", "en"), ("EN", "en"), ("english", "en"), ("en-US", "en"),
        ("English", "en"), ("es", "es"), ("spanish", "es"),
        ("auto", None), ("", None), (None, None),
    ],
)
def test_normalize_language(raw, expected):
    assert normalize_language(raw) == expected


# ---------------------------------------------------------------------------
# Latin-script fabrications (ateles#631, streaming path, 2026-09-01)
# ---------------------------------------------------------------------------
#
# The script check of signal 1b is structurally blind to these: Polish, Finnish
# and Norwegian are all Latin script, so a fabrication in one of them measures
# 0% non-Latin and passes. Measured over the 629 real chunks the script check
# does not catch, these two signals flag 6 — all 6 fabrications, 0 false
# positives.


def test_polish_fabrication_in_an_english_session_is_caught():
    """The operator's real captured chunk. 0% non-Latin, so 1b cannot see it."""
    verdict = screen_transcription(
        "A widać o mnie.", expected_language="en", vad_closed=True
    )
    assert verdict.filtered
    assert verdict.reason == "foreign_diacritic"


def test_the_script_check_alone_would_miss_the_polish_fabrication():
    """Pins WHY a new signal was needed rather than a tuned threshold."""
    assert non_latin_ratio("A widać o mnie.") == 0.0


@pytest.mark.parametrize("text", ["Soita.", "Utanfor."])
def test_a_lone_word_in_a_vad_closed_turn_is_caught(text):
    """Finnish and Norwegian fabrications, both bare Latin with no diacritic."""
    verdict = screen_transcription(text, expected_language="en", vad_closed=True)
    assert verdict.filtered
    assert verdict.reason == "lone_word_turn"


@pytest.mark.parametrize("text", ["Soita.", "Utanfor.", "Okay."])
def test_a_lone_word_is_NOT_filtered_without_a_vad_close(text):
    """A fixed-width chunk can clip a sentence to one word; VAD cannot.

    This is why the signal is gated: the chunking tailer must never enable it.
    """
    verdict = screen_transcription(text, expected_language="en", vad_closed=False)
    assert not verdict.filtered


@pytest.mark.parametrize(
    "text",
    [
        "El niño está aquí.",       # Spanish ñ
        "Ça va très bien.",          # French ç, è
        "Això és el català.",        # Catalan à, ï
        "Über die Straße.",          # German ü, ß
        "Não é verdade.",            # Portuguese ã
    ],
)
def test_code_switching_into_the_operators_own_languages_survives(text):
    """Real bilingual speech must never read as fabrication.

    The allowed set is the UNION over plausible session languages precisely so a
    Spanish "ñ" inside an English session is the operator, not Whisper.
    """
    verdict = screen_transcription(text, expected_language="en", vad_closed=True)
    assert not verdict.filtered, f"{text!r} is real code-switching"


def test_plain_english_can_never_trigger_the_diacritic_signal():
    """Only non-ASCII letters are considered, so ASCII text is always safe."""
    assert foreign_diacritics("Can you hear me? Ctrl-Alt-Shift-Enter.") == []


def test_the_diacritic_reason_names_the_actual_evidence():
    verdict = screen_transcription(
        "A widać o mnie.", expected_language="en", vad_closed=True
    )
    assert "ć" in (verdict.detail or "")
