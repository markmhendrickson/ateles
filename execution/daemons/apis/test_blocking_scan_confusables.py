"""Look-alike letters in review text must fold to their Latin letter.

`_normalize_for_blocking_scan` feeds `output_has_blocking_verdict` and
`body_has_blocking_findings`. Both look for ASCII tokens, so a token spelled
with a single look-alike letter from another script has to be folded back first
or it reads as clean (principles.md, principle 5: fail closed on the field that
carries the safety meaning).

The fold table is derived at import time from Unicode character names rather
than listed by hand, so these tests pin three things: the cases that were
missing, the cases the old fixed table already covered, and that ordinary
Greek and Cyrillic prose is not turned into a false block.
"""

import unicodedata

import pytest

import swarm_dispatch
from swarm_dispatch import (
    _CONFUSABLE_TO_ASCII,
    _normalize_for_blocking_scan,
    body_has_blocking_findings,
    output_has_blocking_verdict,
)

GREEK_CAPITAL_BETA = "Β"
GREEK_CAPITAL_OMICRON = "Ο"
CYRILLIC_CAPITAL_IE = "Е"
SMALL_CAPITAL_D = "ᴅ"

MISSING = {
    GREEK_CAPITAL_BETA: "B",
    GREEK_CAPITAL_OMICRON: "O",
    CYRILLIC_CAPITAL_IE: "E",
    SMALL_CAPITAL_D: "D",
}

# Every pair the previous fixed table folded, written out by character so a
# derivation that drops one fails here.
LEGACY_PAIRS = {
    "В": "B", "в": "b",
    "О": "O", "о": "o",
    "С": "C", "с": "c",
    "К": "K", "к": "k",
    "І": "I", "і": "i",
    "Ι": "I", "ι": "i",
    "Ν": "N", "ν": "n",
    "Ⲛ": "N",
    "Ԍ": "G",
    "Ꮐ": "G",
}

VERDICT_WORDS = ("BLOCKED", "REQUEST_CHANGES", "CHANGES_REQUESTED")


def _swaps(token: str, ascii_letter: str, look_alike: str):
    """Every variant of *token* with one occurrence of *ascii_letter* swapped."""
    for i, ch in enumerate(token):
        if ch == ascii_letter:
            yield token[:i] + look_alike + token[i + 1 :]


def _cases(tokens):
    for token in tokens:
        for look_alike, ascii_letter in MISSING.items():
            for variant in _swaps(token, ascii_letter, look_alike):
                yield pytest.param(
                    variant,
                    id=f"{token}-U+{ord(look_alike):04X}-{token.index(ascii_letter)}",
                )


@pytest.mark.parametrize("variant", list(_cases(VERDICT_WORDS)))
def test_bare_verdict_with_one_swapped_letter_is_detected(variant):
    assert output_has_blocking_verdict(f"**🤖 Lens — Ateles swarm, qa**\n\n{variant}\n")


@pytest.mark.parametrize("variant", list(_cases(VERDICT_WORDS)))
def test_bold_verdict_with_one_swapped_letter_is_detected(variant):
    assert output_has_blocking_verdict(f"summary\n\n**{variant}**\n")


@pytest.mark.parametrize("variant", list(_cases(["[BLOCKING]"])))
def test_severe_finding_marker_with_one_swapped_letter_is_detected(variant):
    assert body_has_blocking_findings(f"**COMMENT**\n\n{variant} scope: summary")


def test_each_missing_confusable_is_covered_in_each_token_it_can_appear_in():
    """The matrix above must actually exercise all four letters against all
    three families, or a skipped case would pass silently (principle 3)."""
    for look_alike, ascii_letter in MISSING.items():
        families = [t for t in (*VERDICT_WORDS, "[BLOCKING]") if ascii_letter in t]
        assert families, f"{look_alike!r} -> {ascii_letter!r} appears in no token"
        for token in families:
            assert list(_swaps(token, ascii_letter, look_alike))
    assert {c for c in MISSING} == {
        GREEK_CAPITAL_BETA,
        GREEK_CAPITAL_OMICRON,
        CYRILLIC_CAPITAL_IE,
        SMALL_CAPITAL_D,
    }


def test_derived_mapping_covers_the_explicit_missing_cases():
    for look_alike, ascii_letter in MISSING.items():
        assert _CONFUSABLE_TO_ASCII.get(look_alike) == ascii_letter, hex(ord(look_alike))


def test_derived_mapping_covers_everything_the_old_table_folded():
    for look_alike, ascii_letter in LEGACY_PAIRS.items():
        assert _CONFUSABLE_TO_ASCII.get(look_alike) == ascii_letter, hex(ord(look_alike))
    assert _normalize_for_blocking_scan("Ｂ") == "B"  # fullwidth, folded by NFKC


def test_every_derived_key_is_a_stable_non_ascii_letter_mapped_to_an_ascii_letter():
    """A key NFKC rewrites would never reach the table, so it would be dead;
    an ASCII key or a non-letter value would be a different table entirely."""
    assert len(_CONFUSABLE_TO_ASCII) > len(LEGACY_PAIRS)
    for src, dst in _CONFUSABLE_TO_ASCII.items():
        assert len(src) == 1 and not src.isascii(), hex(ord(src))
        assert len(dst) == 1 and dst.isascii() and dst.isalpha(), (src, dst)
        assert unicodedata.normalize("NFKC", src) == src, hex(ord(src))


def test_derived_names_are_all_resolved_here():
    """Import-time derivation skips a name this Python's Unicode data lacks.
    Name the ones the fix is about so a skip cannot hide on this interpreter."""
    for name in (
        "GREEK CAPITAL LETTER BETA",
        "GREEK CAPITAL LETTER OMICRON",
        "CYRILLIC CAPITAL LETTER IE",
        "LATIN LETTER SMALL CAPITAL D",
    ):
        assert _CONFUSABLE_TO_ASCII.get(unicodedata.lookup(name)), name


@pytest.mark.parametrize("token", ["[BLOCKING]", *VERDICT_WORDS])
def test_every_derived_look_alike_is_detected_in_every_token_it_fits(token):
    """The generic form of the matrix: any derived upper-case look-alike of a
    letter in the token, swapped in at each position."""
    checked = 0
    for look_alike, ascii_letter in _CONFUSABLE_TO_ASCII.items():
        if not ascii_letter.isupper():
            continue
        for variant in _swaps(token, ascii_letter, look_alike):
            checked += 1
            if token.startswith("["):
                assert body_has_blocking_findings(f"{variant} scope: x"), (token, hex(ord(look_alike)))
            else:
                assert output_has_blocking_verdict(variant), (token, hex(ord(look_alike)))
    assert checked


def test_zero_width_split_and_spaced_forms_still_detected():
    zwj = "‍"
    assert body_has_blocking_findings(f"[BLOC{zwj}KING] scope: x")
    assert body_has_blocking_findings("[ B L O C K I N G ] scope: x")
    assert output_has_blocking_verdict(f"REQU{zwj}EST_CHANGES")
    # the new letters combined with the older evasions
    assert body_has_blocking_findings(f"[{GREEK_CAPITAL_BETA}L{zwj}OCKING] scope: x")
    assert body_has_blocking_findings(f"[ {GREEK_CAPITAL_BETA} L O C K I N G ] scope: x")
    assert output_has_blocking_verdict(f"{GREEK_CAPITAL_BETA}LOCKE{SMALL_CAPITAL_D}")


def test_non_blocking_marker_with_look_alike_letters_is_still_not_flagged():
    assert not body_has_blocking_findings(f"[NON-{GREEK_CAPITAL_BETA}LOCKING] naming: nit")
    assert not body_has_blocking_findings("[ΝΟΝ-BLOCKING] naming: nit")


RUSSIAN = (
    "Эта проверка не нашла проблем: тесты проходят, ветка актуальна, "
    "замечаний к безопасности нет. Результат: ЗАБЛОКИРОВАНО НЕ БЫЛО, "
    "ЗАПРОС ИЗМЕНЕНИЙ НЕ ТРЕБУЕТСЯ. Большое спасибо, Екатерина Борисовна."
)
GREEK = (
    "Η ανασκόπηση δεν βρήκε προβλήματα: οι έλεγχοι περνούν, ο κλάδος είναι "
    "ενημερωμένος και δεν υπάρχουν σχόλια ασφαλείας. ΚΑΜΙΑ ΑΠΟΡΡΙΨΗ, "
    "ΟΛΑ ΕΝΤΑΞΕΙ. Ευχαριστώ πολύ, Βασίλειος Οικονόμου."
)
UKRAINIAN_BELARUSIAN = "Блокування не потрібне, дякуємо. Заблакіравана не было, дзякуй."


@pytest.mark.parametrize("prose", [RUSSIAN, GREEK, UKRAINIAN_BELARUSIAN])
def test_ordinary_greek_and_cyrillic_prose_does_not_trip(prose):
    assert not output_has_blocking_verdict(prose)
    assert not body_has_blocking_findings(prose)
    assert not output_has_blocking_verdict(f"**{prose}**")
    assert not body_has_blocking_findings(f"[{prose}] scope: x")


def test_prose_mixing_scripts_with_ordinary_words_does_not_trip():
    mixed = "Всё хорошо, no findings. Ο κώδικας is fine; Блок не нужен."
    assert not output_has_blocking_verdict(mixed)
    assert not body_has_blocking_findings(mixed)


def test_derivation_is_import_time_not_a_second_hand_list():
    """Principle 9: one source. The mapping is built by a function, not typed."""
    assert callable(swarm_dispatch._derive_confusable_table)
    assert swarm_dispatch._derive_confusable_table() == _CONFUSABLE_TO_ASCII
