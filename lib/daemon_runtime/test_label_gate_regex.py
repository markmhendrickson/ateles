"""PARENT_LINK reads PR and issue bodies, which their authors control.

Two properties are pinned here:

1. Cost. A long run of whitespace anywhere in the text must not make the parent
   link scan take longer than a fraction of a second. The original pattern put
   two adjacent whitespace quantifiers around an optional colon, so the engine
   could split one whitespace run between them in many ways before giving up.
2. Meaning. The rewrite finds exactly the same links as the original: the same
   match spans and the same ``repo`` / ``number`` groups on every input. The
   original is frozen below, character for character, for that comparison.

Revert the pattern in ``label_gate.py`` and the timing test goes red; change
what it accepts and the differential test goes red.
"""

from __future__ import annotations

import random
import re
import time

import pytest

from lib.daemon_runtime import label_gate as lg

# ── The pattern as it stood before the rewrite (frozen; never edit) ──────────
_FROZEN_CLOSING = r"clos(?:e|es|ed)|fix(?:es|ed)?|resolv(?:e|es|ed)"
_FROZEN_PARENTAGE = r"part\s+of|refs?|references?|parent|related\s+to"
_FROZEN_ISSUE_REF = r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<number>\d+)"
_FROZEN_PARENT_LINK = re.compile(
    rf"\b(?:{_FROZEN_CLOSING}|{_FROZEN_PARENTAGE})\s*:?\s+{_FROZEN_ISSUE_REF}", re.I
)


def _signature(pattern: re.Pattern[str], text: str) -> list[tuple]:
    return [
        (m.span(), m.span("repo"), m.span("number"), m.group("repo"), m.group("number"))
        for m in pattern.finditer(text)
    ]


# ── Meaning: identical results to the original ───────────────────────────────

# Whitespace the `\s` class accepts, ASCII and not: tab, newline, vertical tab,
# form feed, carriage return, the file/group/record/unit separators, NEL, NBSP,
# an en/em space, line and paragraph separators, ideographic space, BOM-adjacent
# marks that are NOT whitespace (zero-width space, BOM) so a wrong class shows.
_WS = [" ", "  ", "\t", "\n", "\r\n", "\x0b", "\x0c", "\x1c", "\x1f", "\x85", "\xa0",
       " ", " ", " ", "　", "​", "﻿"]
_KEYWORDS = [
    "close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves",
    "resolved", "ref", "refs", "reference", "references", "parent", "part of",
    "part  of", "part\tof", "part\nof", "related to", "related　to", "Part Of",
    "RELATED TO", "Closes", "FIXES", "Resolved", "Ref", "clos", "fixe", "closeds",
    "prefix", "xfix", "unrelated to", "apart of", "partof", "part ofx", "referencess",
    # case-folding oddities: Kelvin sign and long s fold onto k and s under re.I
    "Keys", "fiſes", "reſoſ",
]
_PUNCT = [":", "::", " :", ": ", "：", ";", ",", ".", "-", "*", "**", "(", ")", "[", "]", "`"]
_REFS = ["#1", "#12", "#007", "#", "# 5", "#x", "o/r#3", "own-er/re.po#41", "a/b#9", "a/b",
         "a/#4", "/b#4", "a//b#4", "a b#4", "o/r #3", "#٣", "#12٣", "#1_2",
         "https://github.com/o/r/issues/3", "o/r/s#5", "O/R#8", "ateles#12"]
_FILLER = ["x", "see", "and", "the", "PR", "\n\n", "- ", "> ", "1", "12", "abc", "é", "ü", "日本"]

_REAL_EXAMPLES = [
    "Closes #12", "Part of #7 and closes #9", "Refs o/r#3", "Closes other/repo#3",
    "see #5 for background", "", "Fixes: #4", "Fixes : #4", "fixes :#4", "Related to #1\nCloses #2",
    "Resolves   #33", "parent: #8", "Parent #8", "References: o/r#3", "closed\n#4",
    "Part of #7, Refs #8, closes #9", "Closes #1, closes #2 and fixes #3",
    "Design basis: docs/foundation/principles.md\n\nCloses #1398", "Fixes\t#5", "Fixes #5",
    "FIXES  #7", "closes:\n#4", "closes\n: #4", "part\nof\n#4",
]


def _random_text(rng: random.Random) -> str:
    parts: list[str] = []
    for _ in range(rng.randint(1, 14)):
        roll = rng.random()
        if roll < 0.30:
            parts.append(rng.choice(_KEYWORDS))
        elif roll < 0.52:
            parts.append(rng.choice(_WS) * rng.choice((1, 1, 1, 2, 3)))
        elif roll < 0.64:
            parts.append(rng.choice(_PUNCT))
        elif roll < 0.84:
            parts.append(rng.choice(_REFS))
        elif roll < 0.92:
            parts.append(rng.choice(_FILLER))
        else:
            parts.append(rng.choice(_REAL_EXAMPLES))
    return "".join(parts)


def test_rewrite_matches_the_original_on_random_strings():
    rng = random.Random(20261006)
    checked_with_match = 0
    total = 150_000
    for _ in range(total):
        text = _random_text(rng)
        expected = _signature(_FROZEN_PARENT_LINK, text)
        assert _signature(lg.PARENT_LINK, text) == expected, repr(text)
        checked_with_match += bool(expected)
    # The comparison must exercise matches, not just agree about nothing.
    assert checked_with_match > total * 0.15


@pytest.mark.parametrize("text", _REAL_EXAMPLES)
def test_rewrite_matches_the_original_on_real_examples(text):
    assert _signature(lg.PARENT_LINK, text) == _signature(_FROZEN_PARENT_LINK, text)


def test_rewrite_matches_the_original_on_every_short_whitespace_colon_combination():
    """Exhaustive over a small alphabet: all strings up to length 7 built from
    keyword end, whitespace kinds, colon and ref start."""
    alphabet = ["fix", " ", "\n", " ", ":", "#1", "x"]
    stack: list[tuple[str, ...]] = [()]
    seen = 0
    while stack:
        combo = stack.pop()
        text = "".join(combo)
        assert _signature(lg.PARENT_LINK, text) == _signature(_FROZEN_PARENT_LINK, text), repr(text)
        seen += 1
        if len(combo) < 7:
            stack.extend(combo + (tok,) for tok in alphabet)
    assert seen > 100_000


def test_parent_issue_number_unchanged_on_real_examples():
    cases = {
        "Closes #12": 12,
        "Part of #7 and closes #9": 7,
        "Fixes: #4": 4,
        "Fixes   :   #4": 4,
        "Closes other/repo#3": None,
    }
    for body, expected in cases.items():
        assert lg.parent_issue_number(body, "o/r") == expected


# ── Cost: long whitespace runs must stay cheap ───────────────────────────────

_BUDGET_SECONDS = 0.5  # generous: the rewrite takes a few milliseconds


def _shapes(n: int) -> dict[str, str]:
    kinds = {"spaces": " ", "tabs": "\t", "newlines": "\n", "nbsp": "\xa0", "ideographic": "　"}
    out: dict[str, str] = {}
    for name, ws in kinds.items():
        run = ws * n
        out[f"{name}-after-keyword"] = "Fixes" + run + "x"
        out[f"{name}-before-keyword"] = run + "Fixes" + run + "x"
        out[f"{name}-after-colon"] = "Fixes:" + run + "x"
        out[f"{name}-around-colon"] = "Fixes" + run + ":" + run + "x"
        out[f"{name}-inside-part-of"] = "Part" + run + "of" + run + "x"
        out[f"{name}-inside-related-to"] = "Related" + run + "to" + run + ":" + run + "x"
        out[f"{name}-before-ref-marker"] = "Closes" + run + "#"
        out[f"{name}-before-repo-name"] = "Refs" + run + "o/" + "a" * 8 + "x"
    out["mixed"] = "Fixes" + (" \t\n" * (n // 3)) + "x"
    out["mixed-around-colon"] = "Fixes" + (" \t\n" * (n // 3)) + ":" + (" \t\n" * (n // 3)) + "x"
    out["alternating-colon-space"] = "Fixes" + (" :" * (n // 2)) + "x"
    out["repeated-keyword-runs"] = ("Fixes" + " " * 64) * (n // 69) + "x"
    out["long-repo-name"] = "Refs " + "a" * n
    out["long-repo-then-slash"] = "Refs " + "a/" * (n // 2)
    out["long-digits"] = "Refs #" + "7" * n
    out["trailing-nonmatch-after-match"] = "Fixes #1\n" + "Fixes" + " " * n + "x"
    return out


_SIZES = (16_384, 65_536)
_SHAPE_NAMES = sorted(_shapes(8))


@pytest.mark.parametrize("size", _SIZES)
@pytest.mark.parametrize("shape", _SHAPE_NAMES)
def test_long_whitespace_runs_stay_cheap(shape, size):
    text = _shapes(size)[shape]
    start = time.perf_counter()
    links = list(lg.PARENT_LINK.finditer(text))
    first = lg.parent_issue_number(text, "o/r")
    elapsed = time.perf_counter() - start
    assert elapsed < _BUDGET_SECONDS, f"{shape} at {size}: {elapsed:.2f}s"
    # Sanity: the shapes with a real link in them still find it.
    if shape == "trailing-nonmatch-after-match":
        assert first == 1 and len(links) == 1
    if shape == "long-digits":
        assert first is None


def test_an_absurd_issue_number_is_no_link_not_an_error():
    """`int()` raises past the interpreter's digit limit; a PR body must not be
    able to make the parent lookup raise."""
    huge = "7" * 5000
    assert lg.parent_issue_number(f"Refs #{huge}", "o/r") is None
    assert lg.parent_issue_number(f"Refs #{huge}\nCloses #12", "o/r") == 12
    assert lg.parent_issue_number("Refs #" + "7" * lg.MAX_ISSUE_NUMBER_DIGITS, "o/r") is not None
    assert lg.parent_issue_number("Refs #" + "7" * (lg.MAX_ISSUE_NUMBER_DIGITS + 1), "o/r") is None
