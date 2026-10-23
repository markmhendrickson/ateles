"""Look-alike characters must not hide a blocking token from the veto scans.

`output_has_blocking_verdict`, `body_has_blocking_findings` and the second
verdict-line veto in `lens_own_verdict` look for ASCII tokens, so a token
spelled with a look-alike character from another script has to be folded back
first or it reads as clean (principles.md, principle 5: fail closed on the field
that carries the safety meaning).

What is pinned here:

* the fold itself: every derived character is pinned individually below, so a
  deleted table entry deletes nothing from this file;
* the veto scans see every spelling, including characters no table lists (the
  bounded wildcard backstop) and a look-alike glued to a token;
* the header and verdict-line recognisers do NOT use the wide fold, so a
  look-alike in a clearing position stays unreadable;
* ordinary Greek and Cyrillic prose is not turned into a false block.
"""

import logging
import unicodedata

import pytest

import swarm_dispatch
from swarm_dispatch import (
    _normalize_for_blocking_scan,
    body_has_blocking_findings,
    lens_own_verdict,
    output_has_blocking_verdict,
    sign_off_is_warranted,
)


def _veto_table():
    return swarm_dispatch._VETO_CONFUSABLE_TO_ASCII


def _tables():
    return swarm_dispatch._CONFUSABLE_TABLES


# Every character the derivation produces, written out by code point. Reviewed
# by hand and NOT generated from the table: removing a table entry (or its
# name) fails here, and adding one means adding it here.
PINNED = [
    (0x0131, "i"),
    (0x0196, "I"),
    (0x01C0, "I"),
    (0x0261, "g"),
    (0x0262, "G"),
    (0x026A, "I"),
    (0x0274, "N"),
    (0x0280, "R"),
    (0x028F, "Y"),
    (0x0299, "B"),
    (0x029C, "H"),
    (0x029F, "L"),
    (0x02CD, "_"),
    (0x037F, "J"),
    (0x0391, "A"),
    (0x0392, "B"),
    (0x0395, "E"),
    (0x0396, "Z"),
    (0x0397, "H"),
    (0x0399, "I"),
    (0x039A, "K"),
    (0x039C, "M"),
    (0x039D, "N"),
    (0x039F, "O"),
    (0x03A1, "P"),
    (0x03A4, "T"),
    (0x03A5, "Y"),
    (0x03A7, "X"),
    (0x03B1, "a"),
    (0x03B9, "i"),
    (0x03BA, "k"),
    (0x03BD, "n"),
    (0x03BF, "o"),
    (0x03C1, "p"),
    (0x03C5, "u"),
    (0x03C7, "x"),
    (0x03F2, "c"),
    (0x03F3, "j"),
    (0x03F9, "C"),
    (0x0405, "S"),
    (0x0406, "I"),
    (0x0408, "J"),
    (0x0410, "A"),
    (0x0412, "B"),
    (0x0415, "E"),
    (0x041A, "K"),
    (0x041C, "M"),
    (0x041D, "H"),
    (0x041E, "O"),
    (0x0420, "P"),
    (0x0421, "C"),
    (0x0422, "T"),
    (0x0423, "Y"),
    (0x0425, "X"),
    (0x0430, "a"),
    (0x0432, "b"),
    (0x0435, "e"),
    (0x043A, "k"),
    (0x043E, "o"),
    (0x0440, "p"),
    (0x0441, "c"),
    (0x0443, "y"),
    (0x0445, "x"),
    (0x0455, "s"),
    (0x0456, "i"),
    (0x0458, "j"),
    (0x04BA, "H"),
    (0x04BB, "h"),
    (0x04C0, "L"),
    (0x04CF, "L"),
    (0x0500, "D"),
    (0x0501, "d"),
    (0x050C, "G"),
    (0x051A, "Q"),
    (0x051B, "q"),
    (0x051C, "W"),
    (0x051D, "w"),
    (0x054D, "U"),
    (0x0555, "O"),
    (0x0570, "h"),
    (0x0578, "n"),
    (0x057D, "u"),
    (0x0585, "o"),
    (0x13A0, "D"),
    (0x13A1, "R"),
    (0x13A2, "T"),
    (0x13A9, "Y"),
    (0x13AA, "A"),
    (0x13AC, "E"),
    (0x13B3, "W"),
    (0x13B7, "M"),
    (0x13BB, "H"),
    (0x13C0, "G"),
    (0x13D2, "R"),
    (0x13DA, "S"),
    (0x13F4, "B"),
    (0x1D00, "A"),
    (0x1D04, "C"),
    (0x1D05, "D"),
    (0x1D07, "E"),
    (0x1D0A, "J"),
    (0x1D0B, "K"),
    (0x1D0D, "M"),
    (0x1D0F, "O"),
    (0x1D18, "P"),
    (0x1D1B, "T"),
    (0x1D1C, "U"),
    (0x1D20, "V"),
    (0x1D21, "W"),
    (0x1D22, "Z"),
    (0x2017, "_"),
    (0x203F, "_"),
    (0x2045, "["),
    (0x2046, "]"),
    (0x222A, "U"),
    (0x22A4, "T"),
    (0x23BD, "_"),
    (0x2574, "_"),
    (0x2581, "_"),
    (0x2758, "I"),
    (0x27E6, "["),
    (0x27E7, "]"),
    (0x2C80, "A"),
    (0x2C81, "a"),
    (0x2C88, "E"),
    (0x2C8C, "Z"),
    (0x2C8E, "H"),
    (0x2C92, "I"),
    (0x2C94, "K"),
    (0x2C98, "M"),
    (0x2C9A, "N"),
    (0x2C9E, "O"),
    (0x2C9F, "o"),
    (0x2CA2, "P"),
    (0x2CA3, "p"),
    (0x2CA4, "C"),
    (0x2CA5, "c"),
    (0x2CA6, "T"),
    (0x2CA8, "Y"),
    (0x2CAC, "X"),
    (0x2D4F, "I"),
    (0x3007, "O"),
    (0x301A, "["),
    (0x301B, "]"),
    (0xA4D0, "B"),
    (0xA4D1, "P"),
    (0xA4D3, "D"),
    (0xA4D4, "T"),
    (0xA4D6, "G"),
    (0xA4D7, "K"),
    (0xA4DA, "C"),
    (0xA4DF, "M"),
    (0xA4E0, "N"),
    (0xA4E2, "S"),
    (0xA4EE, "A"),
    (0xA4F0, "E"),
    (0xA4F2, "I"),
    (0xA4F3, "O"),
    (0xA4F4, "U"),
    (0xA730, "F"),
    (0xA731, "S"),
    (0xA7AF, "Q"),
    (0x10309, "I"),
]

GREEK_CAPITAL_BETA = "\u0392"
GREEK_CAPITAL_OMICRON = "\u039f"
CYRILLIC_CAPITAL_IE = "\u0415"
SMALL_CAPITAL_D = "\u1d05"

MISSING = {
    GREEK_CAPITAL_BETA: "B",
    GREEK_CAPITAL_OMICRON: "O",
    CYRILLIC_CAPITAL_IE: "E",
    SMALL_CAPITAL_D: "D",
}

# Every pair the first hand-written table folded, written out by character.
LEGACY_PAIRS = {
    "\u0412": "B",
    "\u0432": "b",
    "\u041e": "O",
    "\u043e": "o",
    "\u0421": "C",
    "\u0441": "c",
    "\u041a": "K",
    "\u043a": "k",
    "\u0406": "I",
    "\u0456": "i",
    "\u0399": "I",
    "\u03b9": "i",
    "\u039d": "N",
    "\u03bd": "n",
    "\u2c9a": "N",
    "\u050c": "G",
    "\u13c0": "G",
}

VERDICT_WORDS = ("BLOCKED", "REQUEST_CHANGES", "CHANGES_REQUESTED")
MARKER = "[BLOCKING]"
ALL_TOKENS = (*VERDICT_WORDS, MARKER)


def _detected(token: str, variant: str) -> bool:
    """Whether the veto scan that owns *token* detects *variant*."""
    if token == MARKER:
        return body_has_blocking_findings(f"**COMMENT**\n\n{variant} scope: summary")
    return output_has_blocking_verdict(f"summary\n\n{variant}\n")


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
                    token,
                    variant,
                    id=f"{token}-U+{ord(look_alike):04X}-{token.index(ascii_letter)}",
                )


# ── the four letters the first table was missing ─────────────────────────────


@pytest.mark.parametrize(("token", "variant"), list(_cases(ALL_TOKENS)))
def test_one_swapped_letter_is_detected(token, variant):
    assert _detected(token, variant)


@pytest.mark.parametrize(("token", "variant"), list(_cases(VERDICT_WORDS)))
def test_bold_verdict_with_one_swapped_letter_is_detected(token, variant):
    assert output_has_blocking_verdict(f"summary\n\n**{variant}**\n")


def test_each_missing_confusable_is_exercised_against_a_token_it_fits():
    """The matrix above must exercise all four letters, or a skipped case would
    pass silently (principle 3)."""
    for look_alike, ascii_letter in MISSING.items():
        families = [t for t in ALL_TOKENS if ascii_letter in t]
        assert families, f"{look_alike!r} -> {ascii_letter!r} appears in no token"
        for token in families:
            assert list(_swaps(token, ascii_letter, look_alike))


# ── the derived table ────────────────────────────────────────────────────────


def test_every_pinned_character_folds_to_its_letter():
    table = {**_veto_table(), **_tables().pre_nfkc}
    wrong = {
        hex(cp): table.get(chr(cp))
        for cp, letter in PINNED
        if table.get(chr(cp)) != letter
    }
    assert not wrong


def test_the_table_holds_nothing_that_is_not_pinned():
    table = {**_veto_table(), **_tables().pre_nfkc}
    assert {ord(c) for c in table} == {cp for cp, _ in PINNED}


def test_table_is_at_least_as_large_as_when_it_was_written():
    """A narrower fold on another interpreter (names missing from its Unicode
    data) must fail a test, not just log: 160 entries today, so 150 leaves a
    small margin."""
    assert swarm_dispatch._VETO_CONFUSABLE_FLOOR == 150
    assert len(_veto_table()) >= 150


def test_derived_mapping_covers_the_explicit_missing_cases():
    for look_alike, ascii_letter in MISSING.items():
        assert _veto_table().get(look_alike) == ascii_letter, hex(ord(look_alike))


def test_derived_mapping_covers_everything_the_first_table_folded():
    for look_alike, ascii_letter in LEGACY_PAIRS.items():
        assert _veto_table().get(look_alike) == ascii_letter, hex(ord(look_alike))
    assert _normalize_for_blocking_scan("\uff22") == "B"  # fullwidth, folded by NFKC


def test_recogniser_table_is_a_subset_of_the_veto_table():
    """The narrow table must never fold something to a different letter than
    the wide one: one source of truth, defined once (principle 9)."""
    for src, dst in swarm_dispatch._CONFUSABLE_TO_ASCII.items():
        if src in _veto_table():
            assert _veto_table()[src] == dst, hex(ord(src))


def test_table_keys_are_stable_single_characters_mapped_to_one_ascii_character():
    for src, dst in _veto_table().items():
        assert len(src) == 1 and not src.isascii(), hex(ord(src))
        assert len(dst) == 1 and dst.isascii(), (src, dst)
        assert dst.isalpha() or dst in "[]_", (src, dst)
        assert unicodedata.normalize("NFKC", src) == src, hex(ord(src))
    for src, dst in _tables().pre_nfkc.items():
        assert unicodedata.normalize("NFKC", src) != src, hex(ord(src))
        assert dst.isascii() and len(dst) == 1


def test_the_four_names_the_fix_is_about_resolve_here():
    for name in (
        "GREEK CAPITAL LETTER BETA",
        "GREEK CAPITAL LETTER OMICRON",
        "CYRILLIC CAPITAL LETTER IE",
        "LATIN LETTER SMALL CAPITAL D",
    ):
        assert _veto_table().get(unicodedata.lookup(name)), name


def test_derivation_is_a_function_not_a_second_hand_list():
    assert swarm_dispatch._derive_confusable_table() == _veto_table()
    assert swarm_dispatch._derive_confusable_tables().pre_nfkc == _tables().pre_nfkc


def test_an_unknown_name_is_logged_not_swallowed(caplog):
    with caplog.at_level(logging.WARNING, logger="apis.swarm_dispatch"):
        tables = swarm_dispatch._derive_confusable_tables(
            {"NOT A REAL CHARACTER NAME": "Z"}
        )
    assert "NOT A REAL CHARACTER NAME" in caplog.text
    assert tables.stable == _veto_table()  # the unknown name added nothing


def test_import_derivation_logged_no_skipped_names_on_this_interpreter(caplog):
    with caplog.at_level(logging.WARNING, logger="apis.swarm_dispatch"):
        swarm_dispatch._derive_confusable_tables()
    assert "not in this Unicode data" not in caplog.text


# ── gaps found in review ─────────────────────────────────────────────────────


@pytest.mark.parametrize("look_alike", ["\u03f9", "\u03f2"])
def test_greek_lunate_sigma_stands_in_for_c(look_alike):
    for token in ("REQUEST_CHANGES", "CHANGES_REQUESTED"):
        for variant in _swaps(token, "C", look_alike):
            assert _detected(token, variant)
    assert output_has_blocking_verdict(
        "**changes_requested**".replace("c", look_alike, 1)
    )
    assert body_has_blocking_findings(f"[BLO{look_alike}KING] scope: x")


@pytest.mark.parametrize("palochka", ["\u04c0", "\u04cf"])
def test_palochka_reads_as_l_and_as_i(palochka):
    assert body_has_blocking_findings(f"[B{palochka}OCKING] scope: x")  # as L
    assert body_has_blocking_findings(f"[BLOCK{palochka}NG] scope: x")  # as I
    assert output_has_blocking_verdict(f"B{palochka}OCKED")
    # one of each in the same word: the backstop sees both
    assert body_has_blocking_findings(f"[B{palochka}OCK{palochka}NG] scope: x")


@pytest.mark.parametrize(
    "stroke", ["\u01c0", "\u2c92", "\ua4f2", "\U00010309", "\u2d4f", "\u2758", "\u0196"]
)
def test_plain_stroke_look_alikes_read_as_i_and_as_l(stroke):
    assert body_has_blocking_findings(f"[BLOCK{stroke}NG] scope: x")  # as I
    assert body_has_blocking_findings(f"[B{stroke}OCKING] scope: x")  # as L
    assert output_has_blocking_verdict(f"B{stroke}OCKED")
    assert body_has_blocking_findings(f"[B{stroke}OCK{stroke}NG] scope: x")


def test_latin_script_g_and_dotless_i():
    assert body_has_blocking_findings("[BLOCKIN\u0261] scope: x")
    assert body_has_blocking_findings("[BLOCK\u0131NG] scope: x")


@pytest.mark.parametrize(
    ("token", "variant"),
    [
        ("REQUEST_CHANGES", "REQ\u054dEST_CHANGES"),  # Armenian Seh as U
        ("BLOCKED", "BL\u0555CKED"),  # Armenian capital Oh
        ("REQUEST_CHANGES", "\u13a1EQUEST_CHANGES"),  # Cherokee E as R
        ("BLOCKED", "BLOCK\u13acD"),  # Cherokee GV as E
        ("REQUEST_CHANGES", "REQUE\u13daT_CHANGES"),  # Cherokee DU as S
        ("BLOCKED", "BLOCKE\u13a0"),  # Cherokee A as D
        ("REQUEST_CHANGES", "REQ\u222aEST_CHANGES"),  # union as U
        ("REQUEST_CHANGES", "REQUES\u22a4_CHANGES"),  # down tack as T
        ("BLOCKED", "BL\u3007CKED"),  # ideographic zero as O
        ("BLOCKED", "\ua4d0LOCKED"),  # Lisu BA as B
        ("REQUEST_CHANGES", "REQUEST_CHANGES".replace("A", "\u2c80")),  # Coptic Alfa
        ("BLOCKED", "BLOCKE\u1d05"),  # small capital D
    ],
)
def test_other_scripts_and_symbols_are_detected(token, variant):
    assert _detected(token, variant)


@pytest.mark.parametrize(
    "underscore",
    [
        "\u2581",
        "\u23bd",
        "\u02cd",
        "\u2017",
        "\u203f",
        "\u2574",
        "\uff3f",
        " \u0332",
        "\u00a0\u0332",
    ],
)
def test_underscore_stand_ins(underscore):
    for token in ("REQUEST_CHANGES", "CHANGES_REQUESTED"):
        assert _detected(token, token.replace("_", underscore))
        assert _detected(
            token, token.replace("_", underscore).replace("E", "\u0415", 1)
        )
    assert output_has_blocking_verdict(f"**changes{underscore}requested**")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("\u2045", "\u2046"),
        ("\u27e6", "\u27e7"),
        ("\u301a", "\u301b"),
        ("\uff3b", "\uff3d"),
        ("[", "\u2046"),
    ],
)
def test_bracket_stand_ins(left, right):
    assert body_has_blocking_findings(f"{left}BLOCKING{right} scope: x")
    assert body_has_blocking_findings(f"**{left}BLOCKING{right} scope:** x")


def test_underlined_blocked_with_a_trailing_underlined_space_is_still_detected():
    """A space plus a combining low line reads as an underscore, which would end
    BLOCKED's word boundary; the other forms keep it detected."""
    assert output_has_blocking_verdict("the verdict is BLOCKED\u00a0\u0332 for now")


# ── bounded wildcard backstop ────────────────────────────────────────────────

# Characters no table names and no prose fixture below uses as a look-alike;
# plus one that IS in the table, standing where a different letter belongs.
ARBITRARY = ["\u0416", "\u03a9", "\ua66e", "\u16a0", "\u3042", "\u2d63", "\u03b1"]


@pytest.mark.parametrize("token", ALL_TOKENS)
def test_every_position_replaced_by_an_arbitrary_character_is_detected(token):
    checked = 0
    for pos in range(len(token)):
        for ch in ARBITRARY:
            variant = token[:pos] + ch + token[pos + 1 :]
            checked += 1
            assert _detected(token, variant), (token, pos, hex(ord(ch)))
    assert checked == len(token) * len(ARBITRARY)


@pytest.mark.parametrize("token", ALL_TOKENS)
def test_any_two_positions_replaced_are_detected(token):
    for i in range(len(token)):
        for j in range(i + 1, len(token)):
            variant = list(token)
            variant[i], variant[j] = ARBITRARY[0], ARBITRARY[1]
            assert _detected(token, "".join(variant)), (token, i, j)


def test_bold_lower_case_verdict_with_an_arbitrary_character_is_detected():
    assert output_has_blocking_verdict("**bloc\u0416ed**")
    assert output_has_blocking_verdict("**request_chang\u0416s**")


def test_three_unknown_characters_are_beyond_the_bound():
    """The backstop is bounded on purpose: more unknown characters than this
    and the word is no longer recognisably the token."""
    assert not output_has_blocking_verdict("\u0416\u0416\u0416CKED")


def test_the_backstop_does_not_fire_without_the_rest_of_the_word():
    assert not output_has_blocking_verdict("BLOCK\u0416DX")  # a different word
    assert not output_has_blocking_verdict("blocked by \u0416")  # lower case, not bold


@pytest.mark.parametrize("dash", ["\u2212", "\u2013", "\u2011", "\u2012"])
def test_non_blocking_with_an_unusual_dash_is_not_flagged(dash):
    assert not body_has_blocking_findings(f"[NON{dash}BLOCKING] naming: nit")


# ── forms that used to be detected stay detected ─────────────────────────────


@pytest.mark.parametrize("glue", ["\u03b1", "\u0416", "\u03bf"])
def test_a_look_alike_glued_to_a_token_is_still_detected(glue):
    assert output_has_blocking_verdict(f"REQUEST_CHANGES{glue}")
    assert output_has_blocking_verdict(f"{glue}BLOCKED")
    assert output_has_blocking_verdict(f"**BLOCKED**{glue}")
    assert body_has_blocking_findings(f"[BLOCKING]{glue} scope: x")


def test_zero_width_split_and_spaced_forms_still_detected():
    zwj = "\u200d"
    assert body_has_blocking_findings(f"[BLOC{zwj}KING] scope: x")
    assert body_has_blocking_findings("[ B L O C K I N G ] scope: x")
    assert output_has_blocking_verdict(f"REQU{zwj}EST_CHANGES")
    assert body_has_blocking_findings(f"[{GREEK_CAPITAL_BETA}L{zwj}OCKING] scope: x")
    assert body_has_blocking_findings(
        f"[ {GREEK_CAPITAL_BETA} L O C K I N G ] scope: x"
    )
    assert output_has_blocking_verdict(f"{GREEK_CAPITAL_BETA}LOCKE{SMALL_CAPITAL_D}")


def test_non_blocking_marker_with_look_alike_letters_is_still_not_flagged():
    assert not body_has_blocking_findings(
        f"[NON-{GREEK_CAPITAL_BETA}LOCKING] naming: nit"
    )
    assert not body_has_blocking_findings("[\u039d\u039f\u039d-BLOCKING] naming: nit")


# ── the recognisers stay narrow: a look-alike in a clearing position is unreadable ──

HEADER = "**\U0001f916 Waxwing \u2014 Ateles swarm, arch**"


def _reply(header=HEADER, verdict="**SIGNED_OFF**", rest="\n\nNo findings.\n"):
    return f"{header}\n{verdict}{rest}"


def test_the_plain_clear_reply_is_still_readable():
    assert lens_own_verdict(_reply(), lens_agent="waxwing") == "signed_off"
    assert sign_off_is_warranted(_reply(), lens_agent="waxwing")


@pytest.mark.parametrize(
    "verdict",
    [
        "**SIGN\u0395D_OFF**",  # Greek capital Epsilon: not in the recogniser table
        "**APPROV\u0415**",  # Cyrillic Ie
        "**SIGNED_OFF**".replace("_", "\u2581"),
    ],
)
def test_a_look_alike_clearing_verdict_stays_unreadable(verdict):
    assert lens_own_verdict(_reply(verdict=verdict), lens_agent="waxwing") is None
    assert not sign_off_is_warranted(_reply(verdict=verdict), lens_agent="waxwing")


@pytest.mark.parametrize(
    "header",
    [
        "**\U0001f916 W\u0430xwing \u2014 Ateles swarm, arch**",  # Cyrillic a
        "**\U0001f916 \u03a9axwing \u2014 Ateles swarm, arch**",
        "**\U0001f916 Wax\u0561ing \u2014 Ateles swarm, arch**",
        "**\U0001f916 Waxwing \u2014 At\u0435les swarm, arch**",  # Cyrillic e in the fixed words
    ],
)
def test_a_look_alike_agent_name_stays_unreadable(header):
    assert lens_own_verdict(_reply(header=header), lens_agent="waxwing") is None
    assert not sign_off_is_warranted(_reply(header=header), lens_agent="waxwing")


def test_a_look_alike_second_verdict_line_vetoes_a_clear_reply():
    """The veto side IS wide: a later look-alike verdict line makes the reply
    unreadable (fail closed), where the narrow recogniser would have missed it."""
    reply = _reply(rest="\n\n**SIGN\u0395D_OFF**\n")
    assert lens_own_verdict(reply, lens_agent="waxwing") is None
    reply = _reply(rest="\n\n**C\u041eMMENT**\n")
    assert lens_own_verdict(reply, lens_agent="waxwing") is None


def test_the_veto_fold_is_not_used_by_the_recogniser_normaliser():
    assert _normalize_for_blocking_scan("\u0395") == "\u0395"  # Greek Epsilon survives
    assert (
        _normalize_for_blocking_scan("\u0412") == "B"
    )  # a frozen-table letter still folds


# ── ordinary prose is not turned into a false block ──────────────────────────

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
MIXED = "Всё хорошо, no findings. Ο κώδικας is fine; Блок не нужен."
ARMENIAN_CHEROKEE_COPTIC = "Շնորհակալություն, ստուգումը անցավ։ ᎠᏍᎦᏯ ᎤᏲᎢ ⲁⲛⲟⲕ ⲡⲉ ⲡⲛⲟⲩⲧⲉ."


@pytest.mark.parametrize(
    "prose", [RUSSIAN, GREEK, UKRAINIAN_BELARUSIAN, MIXED, ARMENIAN_CHEROKEE_COPTIC]
)
def test_ordinary_non_latin_prose_does_not_trip(prose):
    assert not output_has_blocking_verdict(prose)
    assert not body_has_blocking_findings(prose)
    assert not output_has_blocking_verdict(f"**{prose}**")
    assert not body_has_blocking_findings(f"[{prose}] scope: x")
    assert not output_has_blocking_verdict(f"{prose}\n\n{prose}")


def test_ordinary_english_with_accents_and_symbols_does_not_trip():
    text = "Na\u00efve caf\u00e9 \u2014 r\u00e9sum\u00e9: the \u201cblocked\u201d state, a \u2192 b, x \u2208 S, 5 \u00d7 3 \u2713 \u2026"
    assert not output_has_blocking_verdict(text)
    assert not body_has_blocking_findings(text)


# ── scan-path regexes stay linear on pathological text ───────────────────────
#
# The scans run on text any commenter can write, so a regex that backtracks
# exponentially (or quadratically) on an unclosed bracket or a long run of
# spaces is an availability problem, and the fold multiplies how often the
# spaced-letter regex runs. Every input below is bounded so that the old
# regexes finish, slowly, instead of hanging the test run.

SCAN_FUNCTIONS = {
    "output_has_blocking_verdict": output_has_blocking_verdict,
    "body_has_blocking_findings": body_has_blocking_findings,
    "lens_own_verdict": lambda text: lens_own_verdict(
        HEADER + "\n" + text, lens_agent="waxwing"
    ),
    "sign_off_is_warranted": lambda text: sign_off_is_warranted(
        _reply(rest="\n\n" + text), lens_agent="waxwing"
    ),
}
TIME_LIMIT = 0.5

BOUNDED_PATHOLOGICAL = {
    "unclosed bracket, letters and spaces": "[" + "a " * 22,
    "unclosed bracket, letters and tabs": "[" + "a\t" * 22,
    "unclosed bracket, letters, hyphens and spaces": "[" + "a - " * 11,
    "unclosed bracket, hyphens": "[" + "a-" * 40,
}


def _seconds(fn, text):
    import time

    start = time.perf_counter()
    fn(text)
    return time.perf_counter() - start


@pytest.mark.parametrize("name", list(SCAN_FUNCTIONS))
@pytest.mark.parametrize("label", list(BOUNDED_PATHOLOGICAL))
def test_unclosed_bracket_followed_by_spaced_letters_is_fast(name, label):
    text = BOUNDED_PATHOLOGICAL[label]
    assert _seconds(SCAN_FUNCTIONS[name], text) < TIME_LIMIT


# Whitespace and punctuation runs that made the header and verdict-line
# regexes quadratic; each is long enough that the old shapes take over a
# second and short enough that they still finish.
QUADRATIC_SHAPES = {
    "spaces inside the header name": lambda n: "**\U0001f916 a" + " " * n + "x",
    "spaces before the header role": lambda n: (
        "**\U0001f916 a \u2014 Ateles swarm," + " " * n + "x"
    ),
    "run of stars on a verdict-like line": lambda n: _reply(rest="\n\n" + "*" * n),
    "run of hash, star, underscore and space on a verdict-like line": lambda n: _reply(
        rest="\n\n" + "#*_ " * (n // 4) + "x"
    ),
}


@pytest.mark.parametrize("label", list(QUADRATIC_SHAPES))
def test_whitespace_and_marker_runs_are_not_quadratic(label):
    text = QUADRATIC_SHAPES[label](30000)
    assert (
        _seconds(lambda t: lens_own_verdict(t, lens_agent="waxwing"), text) < TIME_LIMIT
    )
    assert (
        _seconds(lambda t: sign_off_is_warranted(t, lens_agent="waxwing"), text)
        < TIME_LIMIT
    )
    assert _seconds(output_has_blocking_verdict, text) < TIME_LIMIT
    assert _seconds(body_has_blocking_findings, text) < TIME_LIMIT


def test_a_comment_sized_body_of_non_latin_prose_is_scanned_quickly():
    text = (RUSSIAN + " ") * 150  # about 60 KB, the size of GitHub's comment limit
    assert _seconds(output_has_blocking_verdict, text) < 2
    assert _seconds(body_has_blocking_findings, text) < 2


LARGE_PATHOLOGICAL = [
    "[" + "a " * 200,
    "[" + "a " * 5000,
    "[" + "a\t" * 5000,
    "[" + "a - " * 5000,
    "[" + "a-" * 5000,
]


@pytest.mark.parametrize(
    "text", LARGE_PATHOLOGICAL, ids=lambda t: f"{len(t)}-{t[1:4]!r}"
)
def test_large_pathological_input_finishes_in_a_subprocess(text):
    """The exact large shapes, run in a child process so that a regex that does
    not return fails this test at the timeout instead of hanging the run."""
    import json
    import os
    import subprocess
    import sys

    code = (
        "import json, sys, time\n"
        "import swarm_dispatch as sd\n"
        "text = sys.stdin.read()\n"
        "out = {}\n"
        "for name, fn in (('verdict', sd.output_has_blocking_verdict), "
        "('body', sd.body_has_blocking_findings), "
        "('own', lambda t: sd.lens_own_verdict(t, lens_agent='waxwing')), "
        "('clearing', lambda t: sd.sign_off_is_warranted(t, lens_agent='waxwing'))):\n"
        "    start = time.perf_counter(); fn(text); out[name] = time.perf_counter() - start\n"
        "print(json.dumps(out))\n"
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", code],
            input=HEADER + "\n**SIGNED_OFF**\n" + text,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        )
    except subprocess.TimeoutExpired:
        pytest.fail("a scan did not return within 30 s")
    assert done.returncode == 0, done.stderr[-500:]
    assert all(seconds < TIME_LIMIT for seconds in json.loads(done.stdout).values())


# The rewritten regexes must accept exactly what the originals accepted.
# The `_OLD_*` expressions below are FROZEN COPIES of the pre-change
# expressions, kept only as the reference for these comparisons: never edit them.

_OLD_OWN_HEADER_RE = __import__("re").compile(
    r"^\*\*\U0001f916\s*(?P<name>[^\s\u2014\u2013-][^\u2014\u2013\-\n]*?)\s*[\u2014\u2013-]+\s*Ateles swarm,"
    r"\s*(?P<role>[^\n]*?\S)\s*\*\*\s*$"
)
_OLD_SPACED_LETTER_RUN_RE = __import__("re").compile(r"\[(?:\s*[A-Za-z\-]\s*){2,}\]")


def _random_strings(alphabet, count, longest, seed):
    import random

    rng = random.Random(seed)
    for _ in range(count):
        yield "".join(rng.choice(alphabet) for _ in range(rng.randint(1, longest)))


def test_rewritten_header_regex_matches_what_the_original_matched():
    alphabet = [
        "**",
        "\U0001f916",
        " ",
        "  ",
        "\t",
        "\u2014",
        "\u2013",
        "-",
        "a",
        "Bob",
        "Ateles swarm,",
        "qa",
        "\n",
        ",",
        "x y",
    ]
    seen = 0
    for text in _random_strings(alphabet, 20000, 12, 1):
        for candidate in (text, "**\U0001f916 " + text):
            old, new = (
                _OLD_OWN_HEADER_RE.match(candidate),
                swarm_dispatch._OWN_HEADER_RE.match(candidate),
            )
            assert (old is None) == (new is None), candidate
            if old:
                seen += 1
                assert old.group("name", "role") == new.group("name", "role"), candidate
    assert seen
    for header in (
        "**\U0001f916 Waxwing \u2014 Ateles swarm, arch**",
        "**\U0001f916  Agent Name  -  Ateles swarm,   qa lens  **  ",
        "**\U0001f916 Waxwing\u2013Ateles swarm,arch**",
    ):
        assert _OLD_OWN_HEADER_RE.match(header).group(
            "name", "role"
        ) == swarm_dispatch._OWN_HEADER_RE.match(header).group("name", "role")


def test_rewritten_spaced_letter_regex_matches_what_the_original_matched():
    alphabet = ["[", "]", "a", "B", "-", " ", "\t", "1", "ab", "\n"]
    for text in _random_strings(alphabet, 30000, 12, 2):
        old, new = (
            _OLD_SPACED_LETTER_RUN_RE.search(text),
            swarm_dispatch._SPACED_LETTER_RUN_RE.search(text),
        )
        assert (old is None) == (new is None), text
        if old:
            assert old.group() == new.group(), text


def test_rewritten_verdict_like_regex_matches_what_the_original_matched():
    import re

    tokens = "|".join(re.escape(t) for t in swarm_dispatch.REVIEW_VERDICT_TOKENS)
    old_re = re.compile(
        r"^[#*_\s]*(?:(?i:verdict)\s*[:=\u2014\u2013-]\s*)?[#*_\s]*(?:"
        + tokens
        + r")(?![A-Za-z0-9]|_[A-Za-z0-9])"
    )
    alphabet = [
        "*",
        "#",
        "_",
        " ",
        "Verdict",
        ":",
        "COMMENT",
        "APPROVE",
        "\u2014",
        "-",
        "x",
        "SIGNED_OFF",
        "\t",
        "1",
    ]
    for text in _random_strings(alphabet, 30000, 10, 3):
        assert (old_re.match(text) is None) == (
            swarm_dispatch._VERDICT_LIKE_RE.match(text) is None
        ), text
