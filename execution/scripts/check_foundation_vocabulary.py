#!/usr/bin/env python3
"""Check docs/foundation/ prose against the banned words in vocabulary.md.

`docs/foundation/vocabulary.md` ends every term entry with two lists:

    **Never:** "chip", "work item", "executing".
    **Not for:** "ticket" for a task; "owner" alone.

Never items are banned in every sense: a hit anywhere in the foundation prose fails the check (exit 1),
printed as ``file:line``. Not-for items are banned in the stated sense only: a hit is advisory, printed
with the sense so the author can judge it, and never fails the check.

Two sources feed the ban list, and they are deliberately separate:

* **The vocabulary's prose.** Each quoted phrase in a Never or Not-for list is a banned item, matched as a
  whole word, case insensitively, with a space also matching a hyphen (``"in flight"`` matches
  ``in-flight``). The vocabulary states forbidden words in words a person reads.
* **``PATTERNS`` below.** The bans that need more than a phrase — a sense distinction, an inflection set, a
  span between two words — live here as regular expressions, keyed by the ``###`` entry heading they belong
  to and by class (Never or Not-for). They used to be written inline in the vocabulary, which put regex
  syntax into a foundation document a person is meant to read. The prose still states the ban; this table
  holds the machinery. ``test_foundation.py`` asserts every key here names an entry that still exists in
  ``vocabulary.md``, so renaming a term fails the test rather than silently un-linting itself.
  A ``PATTERNS`` item may carry a third element, ``files`` — a frozenset of filenames the ban applies to.
  Omitted (or ``None``), it is global like every phrase-based ban. Scope a ban to a subset of documents only
  where the vocabulary's own Not-for already says the ordinary sense is permitted elsewhere: a global Never
  would then be wrong, not stricter, so the ban has to stay narrower than the term.

Lines not scanned: any line carrying a Never or Not-for list; any line containing "retired" (a retired
name may be named where it is retired); in vocabulary.md, the continuation lines of a wrapped Never or
Not-for list, the ``**Related:**`` link lists, and any table row (the Verbs, Owner, and Retired tables name
banned words on purpose); fenced code blocks are scanned (mermaid labels are prose).
``status.md`` is never scanned: it reports the checkout's names, which are the old ones.

A third, separate report runs after the Never/Not-for scan: **undefined-word candidates**. The two lists
above can only ban a word that already has a vocabulary.md entry; they cannot notice a word used
constantly, in several senses, with no entry at all — the gap that let `role`, `domain`, and `scope` go
undefined through 48 revisions while `authority_model.md` leaned on them as load-bearing terms. This report
counts word frequency across the same scanned corpus, excludes words already defined by a ``### heading``
(with their plurals), excludes ``ALLOWLIST_WORDS`` (ordinary English, curated by hand like ``GENERIC`` in
``link_vocabulary_terms.py``), and prints what is left above ``UNDEFINED_WORD_THRESHOLD``. It is advisory
only — a prompt to a human to judge whether a candidate wants a vocabulary entry or belongs on the
allowlist — and never affects the exit code; pass ``--no-undefined-words`` to suppress it.

Usage:
    check_foundation_vocabulary.py [--root DIR] [--quiet-advisory] [--top N] [--no-undefined-words]

Exit 1 on any Never hit, on a ``PATTERNS`` key that names no entry (an incomplete check is not a pass),
or on a missing corpus; 0 otherwise. Stdlib only; ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")
VOCABULARY = "vocabulary.md"
SKIPPED_FILES = {"status.md"}

# --- Undefined-term candidates: the checker gap this section closes -------------------------------------
#
# Every Never and Not-for ban above starts from a vocabulary.md entry. That structure cannot notice a word
# used constantly, in several senses, that never got an entry at all — the way `role`, `domain`, and `scope`
# sat undefined through 48 revisions while `authority_model.md` and `gates_and_workflows.md` leaned on them
# as load-bearing terms. `record`, `subject`, `capability`, and `event` were the same defect, found only by
# an operator reading the corpus by hand, not by this checker.
#
# This section is advisory, not a Never or a Not-for: it does not know whether a frequent undefined word is
# load-bearing (wants an entry) or ordinary English (wants ALLOWLIST). That judgment is a person's, the same
# way GENERIC in link_vocabulary_terms.py is a person's list, not a derived one. The check's job is only to
# surface candidates worth a person's five minutes, at a threshold tuned so the list stays short enough to
# read at a sitting (tens of words, not hundreds) — see ``test_foundation.py`` for the count this holds to
# against the real corpus.
#
# A candidate is a word that is: not already a defined term (a `### heading` in vocabulary.md, or a plural
# or a possessive of one); not in ALLOWLIST; and used at least UNDEFINED_WORD_THRESHOLD times across the
# scanned foundation prose (the same corpus the Never/Not-for scan reads: every foundation ``*.md`` but
# ``status.md``, code spans and fenced blocks excluded, vocabulary.md's own table rows and ban lists
# excluded so the file that defines terms does not nominate its own scaffolding).
UNDEFINED_WORD_THRESHOLD = 150

# Ordinary English this checker should never nominate, however often it appears: function words, and nouns,
# verbs, and adjectives that carry no sense this design binds. Curated by hand, the same way GENERIC is in
# link_vocabulary_terms.py — a wrong exclusion hides a real candidate, and a missing one is only one line of
# advisory output, so the list is grown deliberately rather than defensively. It is not exhaustive of
# English; it is exhaustive of what this corpus's word-frequency actually surfaces above the threshold that
# is not already a term or a genuine candidate.
ALLOWLIST_WORDS = frozenset(
    """
    that what with never from which this every rather than would nothing already because written
    where does through before against above other into without both after below over must they
    itself there says once here about when stated section naming
    binding channel effect recorded delivery rules level source
    handled reading created check steps could should shall being been having doing
    across during between among within toward beneath beside inside outside upon until unless
    whenever wherever whatever whichever whoever whomever yourself myself himself
    herself ourselves themselves done make makes made making gives give given giving
    take takes taking name named names naming say said saying state states stated stating call
    calls called calling need needs needed needing want wants wanted wanting exist exists existed
    existing require requires required requiring allow allows allowed allowing carry carries carried
    carrying hold holds holding held keep keeps kept keeping leave leaves leaving left read reads
    reading written writes writing wrote write letting lets more most less least much many few
    several such same other another first second third fourth once twice three four five six seven
    eight nine ten still even just also only really actually simply merely quite very too thus hence
    therefore however moreover furthermore instead otherwise meanwhile nevertheless
    nonetheless whereas although though unless until since while whether either neither nor yet
    each whose under work them none have exactly having whom itself under below over than upon
    close closed closes closing opens opened opening ruled ruling revised revision retired retiring
    resolves resolved resolving pass passes passed declares declared declaring taken taking bound
    definition question shape list default exists existing cannot
    """.split()
)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z']*")
_TESTID_ABBREVIATIONS = frozenset(
    {"wm", "gw", "fp", "au", "dm", "ad", "pm", "wf", "pl", "x", "m", "r", "s", "g", "re", "t", "c", "b"}
)


def _normalize_word(token: str) -> str:
    """Lowercase a token and strip a trailing possessive, so ``operator's`` counts as ``operator``."""
    w = token.lower()
    if w.endswith("'s"):
        w = w[:-2]
    return w.strip("'")


def _defined_words(vocab_text: str) -> set[str]:
    """Every word a ``### heading`` in vocabulary.md defines, singular and its plural.

    A multi-word heading like "step owner" contributes both "step" and "owner" — a candidate scan should
    not nominate a word that is already part of a defined compound, even where the bare word also has
    unrelated uses; that judgment (as with bare "scope") is the vocabulary's own Not-for prose, not this
    checker's to duplicate.
    """
    words: set[str] = set()
    for heading in _entry_headings(vocab_text):
        h = re.sub(r"\([^)]*\)", " ", heading)  # "execute (a task)" -> "execute"
        h = re.sub(r"`([^`]*)`", r"\1", h)  # code spans keep their word, lose the backticks
        for raw in _TOKEN_RE.findall(h):
            w = _normalize_word(raw)
            if not w:
                continue
            words.add(w)
            words.add(w + "s")
            if w.endswith("y") and len(w) > 1:
                words.add(w[:-1] + "ies")
    return words


def undefined_word_candidates(
    root: Path, *, threshold: int = UNDEFINED_WORD_THRESHOLD
) -> list[tuple[str, int]]:
    """Words used at least ``threshold`` times in foundation prose with no vocabulary entry.

    Advisory only — this function is never consulted by the Never/exit-1 path. It reads the same corpus
    ``scan`` does (every foundation ``*.md`` but ``status.md``, fenced/code spans stripped) plus
    vocabulary.md's own prose, excluding vocabulary.md's table rows and Never/Not-for lists so the document
    that defines terms is not scanned for its own scaffolding words. Returned most-frequent first.
    """
    fdir = root / FOUNDATION_DIR
    vocab_path = fdir / VOCABULARY
    if not fdir.is_dir() or not vocab_path.is_file():
        return []
    vocab_text = vocab_path.read_text(encoding="utf-8")
    defined = _defined_words(vocab_text)

    counts: Counter[str] = Counter()
    for path in sorted(fdir.glob("*.md")):
        if path.name in SKIPPED_FILES:
            continue
        is_vocab = path.name == VOCABULARY
        in_ban_list = False
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if is_vocab:
                if stripped.startswith((_NEVER_MARK, _NOT_FOR_MARK)):
                    in_ban_list = True
                    continue
                if in_ban_list and stripped and not stripped.startswith(("**", "#", "|")):
                    continue
                in_ban_list = False
                if stripped.startswith("|"):
                    continue  # table rows name terms on purpose (Owner, Retired, the tuple table, …)
            prose = as_read(line)
            prose = re.sub(r"```.*?```", " ", prose)
            prose = re.sub(r"`[^`]*`", " ", prose)  # code spans: field and entity-type names, not prose
            for raw in _TOKEN_RE.findall(prose):
                w = _normalize_word(raw)
                if len(w) < 4 or w in _TESTID_ABBREVIATIONS:
                    continue
                counts[w] += 1

    out = [
        (w, n)
        for w, n in counts.items()
        if n >= threshold and w not in defined and w not in ALLOWLIST_WORDS
    ]
    out.sort(key=lambda item: (-item[1], item[0]))
    return out

_NEVER_MARK = "**Never:**"
_NOT_FOR_MARK = "**Not for:**"
_QUOTED_RE = re.compile(r'"([^"]+)"')
_REGEX_ITEM_RE = re.compile(r"(?<![\w`])/((?:\\/|[^/])+)/(?![\w`])")

# Regex bans, keyed by the ``### heading`` in vocabulary.md that states them in prose, then by class.
# Each item is (regex source, sense) — the sense is printed with a Not-for advisory and ignored for Never.
# Every key must name an entry that exists in vocabulary.md; test_foundation.py asserts that, so a term
# rename fails loudly instead of quietly dropping the ban.
PATTERNS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "execute (a task)": {
        "never": [
            (r"\bworked\b(?!\s+on\b)", ""),
            (r"\bwork(?:s|ing)?\s+(?:a|an|the|that|its|each|every|one|this|those|these)\s+tasks?\b", ""),
            (r"\btasks?\s+(?:is|are|was|were|be|been|being)\s+worked\b", ""),
            (r"`executing`", ""),
            (r"\b(?:is|are|was|were|status|state|stays?|stayed)\s+executing\b", ""),
            (r"\bexecuting\s+(?:status|state|flag)\b", ""),
        ],
    },
    "claim": {
        "never": [
            (r"\bdispatch\w*", ""),
            (r"\bpick(?:s|ed|ing)?[\s-]up\b", ""),
            (r"\bhand(?:s|ed|ing)?[\s-]off\b", ""),
            (r"\bpush(?:es|ed|ing)?\b", ""),
            (r"\bspawn\w*\b", ""),
        ],
    },
    "lease": {
        "not_for": [
            (
                r"(?<!step )(?<!plan )(?<!grant )(?<!business )(?<!current )(?<!routed )\bowners?\b",
                "owner alone, for the lease holder or anything else",
            ),
            (r"(?<!lease )\bholders?\b", "holder without the lease"),
        ],
    },
    "held": {
        "not_for": [
            (r"\bstatus\s+(?:of\s+|=\s*|is\s+)?`?claimed`?", "claimed as a stored task status"),
        ],
    },
    "returned": {
        "never": [
            (r"\blease\w*\s+(?:is|are|was|were|be|been|being|gets?|got)\s+released\b", ""),
            (
                r"\breleas(?:e|es|ed|ing)\s+(?:a|an|the|its|their|one|any|each|every|expired|lapsed)"
                r"\s+(?:\w+\s+)?leases?\b",
                "",
            ),
        ],
        "not_for": [
            (r"\breleas\w*\b[^.;:]{0,30}\bleases?\b", "release for a lease"),
            (r"\bleases?\b[^.;:]{0,30}\breleas\w*", "release for a lease"),
        ],
    },
    "claimable": {
        "not_for": [
            (r"\bopen\s+(?:tasks?|pool)\b", "open for claimable; `open` is a status value"),
            (r"\btasks?\s+(?:is|are)\s+open\b", "open for claimable; `open` is a status value"),
        ],
    },
    "batch": {
        "never": [
            (r"\b(?:a|an|the|one|this|that|each|every)\s+splits?\b", ""),
            (r"\bsplit-?outs?\b", ""),
        ],
    },
    "step": {
        "not_for": [
            (
                r"\b(?:pm|ux|arch|impl|pr_review|qa|legal|merge|review|release)\s+(?:gate|phase|check)\b",
                "gate, phase or check for a step; `gate` is the action gate",
            ),
            (
                r"\bgates?\s+(?:owner|name|set|sequence|list)s?\b",
                "gate, phase or check for a step; `gate` is the action gate",
            ),
            (r"\bcheckpoint\s+step\b", "checkpoint for a step"),
            (r"\bstep\s+(?:named\s+)?`?checkpoint`?", "checkpoint for a step"),
        ],
    },
    "take (an action)": {
        "never": [
            (r"\bexecut\w*\b[^.;:]{0,40}\bactions?\b", ""),
            (r"\bactions?\b[^.;:]{0,40}\bexecut\w*", ""),
            (r"\bauto-?execut\w*", ""),
        ],
    },
    "checkpoint": {
        "not_for": [
            (r"\bcheckpoint\s+step\b", "checkpoint for a step"),
        ],
    },
    "escalate": {
        "never": [
            (r"`escalations?`", ""),
            (r"\bescalation\s+(?:entity|entities|record|schema|object)s?\b", ""),
            (
                r"\b(?:an|one|raises?|raised|raising|writes?|written|wrote)\s+(?:aggregated\s+)?escalations?\b",
                "",
            ),
        ],
    },
    "record": {
        # Global Never: "record" reads as Neotoma everywhere in foundation prose, so a sentence that
        # possesses or qualifies it with a foreign-system noun reads as the record itself sitting inside, or
        # belonging to, that system — backwards from decision 55 and the boundary the term defines. The
        # ordinary-English sense ("the record a step owner writes") is explicitly permitted by this same
        # entry's Not-for and is structurally distinct from every pattern below (no foreign-system noun is
        # adjacent to it), so a global ban does not collide with it anywhere in the corpus — checked by
        # running this exact ban, unscoped, against every foundation document at the revision that added it.
        #
        # This is a STRUCTURAL check, not a literal-phrase one: it fired twice on paraphrase before this
        # revision. The first cut required the exact words "external system" and missed "the rail's record",
        # "a record of a system the swarm does not own", "the merchant's record", "an external record",
        # "a record living in an external system" — none of which contain that phrase. Worse, it was also
        # scoped to only six adapter documents by filename, so even a literal hit of the banned phrase
        # outside `adapters.md`/`github.md`/`gmail.md`/`calendar.md`/`telegram.md`/`payments.md` (found
        # in `workflows.md` and `data_model.md` once this structural version ran) passed silently. The
        # shape that is actually banned is grammatical, not lexical, and not confined to one corner of the
        # corpus: `record` (any inflection) as the head noun,
        # (a) possessed by a foreign-system noun ("the rail's record", "the merchant's record"), or
        # (b) governed by of/in/for/at/living-in/held-in/held-by pointing at one, with up to three words
        #     of filler between the preposition and the noun so "a record of a thing in an external
        #     system" and "a record in that system" (anaphoric, not just "the"/"an") both fire, or
        # (c) modified by the adjective external/foreign directly, with or without a determiner ("an
        #     external record", "no external record", "external records" bare, "one foreign record"), a
        #     space or a hyphen both counting (the vocabulary's own phrase-matcher convention: "external-
        #     record types" found live in migration.md), or
        # (d) the head of a relative clause whose subject is a foreign-system noun ("the record an
        #     external system holds/keeps/has") — the one shape identical to the pre-fix Never phrase
        #     ("record ... external system") but missing the preposition that (b) requires.
        # The closed list of nouns that make (a)-(d) fire is "system" generically, plus the per-domain
        # systems this corpus actually names as external (rail, merchant, checkout). `adapter` is
        # deliberately EXCLUDED even though adapters.md discusses them constantly: the adapter entry's own
        # definition makes it the swarm's own component, not an external system ("the only component that
        # touches the system" — it stands with the record, not against it), so "record an adapter keeps"
        # is the ordinary permitted sense, not the collision, and including it produced a real false
        # positive (`adapters.md`'s "which kinds of record an adapter keeps current"). The (b) filler
        # excludes "record"/"and"/"or" so it cannot skip past a full clause boundary to a later, unrelated
        # "external" (the other false positive found: "a record in the record and an external one alike").
        # Every clause below was checked, unscoped, against the full corpus at the revision that added it:
        # zero false positives, including against the "system of record" idiom which reads backwards from
        # the ban, and the two false positives above, found and excluded by name.
        "never": [
            (
                r"\brecord[a-z']*\b\s+(?:living\s+in|held\s+in|held\s+by|of|in|for|at)\s+"
                r"(?:(?:an?|the|that|this|its|their)\s+)?(?:(?!record\b|and\b|or\b)\w+[\s,]+){0,3}?"
                r"(?:external[\s-]+\w+|foreign[\s-]+\w+|rail|merchant|checkout|system)\b",
                "",
            ),
            (
                r"\b(?:external|foreign)[\s-]+record[a-z']*\b",
                "",
            ),
            (
                r"\b(?:rail|merchant|checkout|system)'s\s+record[a-z']*\b",
                "",
            ),
            (
                r"\brecord[a-z']*\b\s+(?:an?\s+|the\s+|that\s+|this\s+)?"
                r"(?:external[\s-]+\w+|foreign[\s-]+\w+|rail|merchant|checkout|system)\s+"
                r"(?:holds?|keeps?|has|carries?|owns?)\b",
                "",
            ),
        ],
    },
}


@dataclass(frozen=True)
class Ban:
    term: str  # as written in vocabulary.md
    pattern: re.Pattern[str]
    sense: str  # "" for Never; the Not-for sense text otherwise
    entry: str  # the ### heading the item sits under
    files: frozenset[str] | None = None  # None = every scanned file; else only these filenames


@dataclass(frozen=True)
class Hit:
    ban: Ban
    file: str
    line_no: int
    text: str


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    parts = [re.escape(p) for p in phrase.strip().split()]
    body = r"[\s-]+".join(parts)
    return re.compile(rf"(?<![\w_])(?:{body})(?![\w_])", re.IGNORECASE)


def _items(list_text: str, *, first_quoted_only: bool) -> list[tuple[str, re.Pattern[str]]]:
    """The banned items in one Never or Not-for list.

    Every ``/regex/`` is an item. In a Never list every quoted phrase is an item. In a Not-for list the
    items are the regexes plus, for each ``;``-separated clause without a regex, its FIRST quoted
    phrase; later quoted phrases in a clause are the sense text (``"ticket" for a task``), as are all
    quoted phrases in a clause that carries a regex.
    """
    out: list[tuple[str, re.Pattern[str]]] = []
    for m in _REGEX_ITEM_RE.finditer(list_text):
        raw = m.group(1).replace("\\/", "/")
        try:
            out.append((f"/{raw}/", re.compile(raw, re.IGNORECASE)))
        except re.error as exc:  # a bad regex in the vocabulary is a defect in the vocabulary
            raise SystemExit(f"vocabulary.md: bad regex /{raw}/: {exc}") from exc
    # Split clauses on ';' outside regex items (a regex may itself contain ';').
    masked = _REGEX_ITEM_RE.sub(lambda m: "\x00" * len(m.group(0)), list_text)
    clauses = masked.split(";") if first_quoted_only else [masked]
    for clause in clauses:
        if first_quoted_only and "\x00" in clause:
            continue  # the regex is the item; quoted words in that clause are its sense
        quoted = [q.strip() for q in _QUOTED_RE.findall(clause) if q.strip() and q.strip() != "—"]
        if first_quoted_only:
            quoted = quoted[:1]
        for q in quoted:
            out.append((q, _phrase_pattern(q)))
    return out


def _lists(text: str) -> list[tuple[str, str, str]]:
    """(kind, list_text, entry) for every Never / Not-for list, joined across wrapped lines."""
    found: list[tuple[str, str, str]] = []
    entry = ""
    current_kind = ""
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf, current_kind
        if current_kind and buf:
            found.append((current_kind, " ".join(buf), entry))
        buf, current_kind = [], ""

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("### "):
            flush()
            entry = line[4:].strip()
            continue
        if line.startswith(_NEVER_MARK):
            flush()
            current_kind = "never"
            buf = [line[len(_NEVER_MARK):]]
            continue
        if line.startswith(_NOT_FOR_MARK):
            flush()
            current_kind = "not_for"
            buf = [line[len(_NOT_FOR_MARK):]]
            continue
        if current_kind:
            if not line or line.startswith("**") or line.startswith("#") or line.startswith("|"):
                flush()
            else:
                buf.append(line)
    flush()
    return found


def _sense_for(list_text: str, item: str) -> str:
    """The clause (split on ';') that carries this Not-for item."""
    for clause in list_text.split(";"):
        if item.strip("/") in clause or item in clause:
            return " ".join(clause.split())
    return " ".join(list_text.split())


def parse_bans(vocab_text: str) -> tuple[list[Ban], list[Ban]]:
    never: list[Ban] = []
    not_for: list[Ban] = []
    for kind, list_text, entry in _lists(vocab_text):
        for term, pattern in _items(list_text, first_quoted_only=(kind == "not_for")):
            if kind == "never":
                never.append(Ban(term, pattern, "", entry))
            else:
                not_for.append(Ban(term, pattern, _sense_for(list_text, term), entry))
    # PATTERNS is attached per entry, not per list, so an entry whose prose list is empty (or absent)
    # still carries its regex bans. An entry the text does not declare contributes nothing: the table
    # describes this vocabulary, and ``missing_pattern_entries`` is what reports a key gone stale.
    entries = _entry_headings(vocab_text)
    for entry, by_kind in PATTERNS.items():
        if entry not in entries:
            continue
        for kind, items in by_kind.items():
            for item in items:
                # Each item is (source, sense) or, to scope the ban to a subset of documents,
                # (source, sense, files). files is a frozenset of filenames; omitted means every file.
                source, sense, *rest = item
                files = rest[0] if rest else None
                try:
                    pattern = re.compile(source, re.IGNORECASE)
                except re.error as exc:  # a bad regex in the table is a defect in the table
                    raise SystemExit(
                        f"check_foundation_vocabulary.py: bad PATTERNS regex /{source}/: {exc}"
                    ) from exc
                ban = Ban(f"/{source}/", pattern, "" if kind == "never" else sense, entry, files)
                (never if kind == "never" else not_for).append(ban)
    return never, not_for


def _entry_headings(vocab_text: str) -> set[str]:
    """Every ``### heading`` in the vocabulary, as written."""
    return {
        line.strip()[4:].strip() for line in vocab_text.splitlines() if line.strip().startswith("### ")
    }


def missing_pattern_entries(vocab_text: str) -> list[str]:
    """PATTERNS keys that no longer name a ``###`` entry in vocabulary.md.

    A term rename that leaves a key behind silently un-lints that term's regex bans, because nothing in
    the vocabulary carries them any more. ``test_foundation.py`` asserts this list is empty against the
    real document; ``main`` refuses to report a pass with a stale key.
    """
    entries = _entry_headings(vocab_text)
    return sorted(k for k in PATTERNS if k not in entries)


_INLINE_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
_EMPHASIS_RE = re.compile(r"\*\*([^*]+)\*\*|\*([^*]+)\*")


def as_read(line: str) -> str:
    """The line as a reader sees it: inline links and bold/italic emphasis reduced to their display text.

    A ban is about the words in the prose, not the markup around them. ``[step owner](#step-owner)`` reads
    as "step owner", but matched raw it puts a ``[`` in front of "step" and a ``](#step-`` in front of
    "owner", which defeats every lookbehind in ``PATTERNS`` and reports the phrase the vocabulary
    explicitly permits. Linking a term must not change the verdict on the sentence containing it. The same
    is true of ``**bold**`` or ``*italic*`` landing between two words a pattern spans (found live at
    ``data_model.md``'s "a thing in an **external** system", which a foreign-noun match missed until the
    asterisks were stripped): a person reads "external", not "**external**", and a ban's verdict on a
    sentence must not change because the author emphasized one word in it.
    """
    line = _INLINE_LINK_RE.sub(lambda m: m.group(1), line)
    return _EMPHASIS_RE.sub(lambda m: m.group(1) or m.group(2), line)


def _scannable(line: str, *, is_vocab: bool) -> bool:
    if _NEVER_MARK in line or _NOT_FOR_MARK in line:
        return False
    if "retired" in line.lower():
        return False
    if is_vocab and line.lstrip().startswith("|"):
        return False
    return True


def scan(root: Path, never: list[Ban], not_for: list[Ban]) -> tuple[list[Hit], list[Hit]]:
    never_hits: list[Hit] = []
    advisory: list[Hit] = []
    fdir = root / FOUNDATION_DIR
    for path in sorted(fdir.glob("*.md")):
        if path.name in SKIPPED_FILES:
            continue
        is_vocab = path.name == VOCABULARY
        rel = str(path.relative_to(root))
        skip_section = False
        in_ban_list = False
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith("## "):
                skip_section = is_vocab and "retired" in line.lower()
            if is_vocab:
                # A Never / Not-for list may wrap; its continuation lines are part of the list.
                stripped = line.strip()
                if stripped.startswith((_NEVER_MARK, _NOT_FOR_MARK)):
                    in_ban_list = True
                    continue
                if in_ban_list and stripped and not stripped.startswith(("**", "#", "|")):
                    continue
                in_ban_list = False
                if stripped.startswith("**Related:**") or (stripped.startswith("[") and "](#" in stripped):
                    continue  # link lists name terms, not prose
            if skip_section or not _scannable(line, is_vocab=is_vocab):
                continue
            prose = as_read(line)
            for ban in never:
                if ban.files is not None and path.name not in ban.files:
                    continue
                if ban.pattern.search(prose):
                    never_hits.append(Hit(ban, rel, no, line.strip()))
            for ban in not_for:
                if ban.files is not None and path.name not in ban.files:
                    continue
                if ban.pattern.search(prose):
                    advisory.append(Hit(ban, rel, no, line.strip()))
    return never_hits, advisory


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--quiet-advisory", action="store_true", help="print only the advisory summary")
    ap.add_argument("--top", type=int, default=5, help="most common advisory terms to list")
    ap.add_argument(
        "--no-undefined-words",
        action="store_true",
        help="skip the undefined-word candidate report (advisory only; never affects the exit code)",
    )
    args = ap.parse_args(argv)

    fdir = args.root / FOUNDATION_DIR
    vocab_path = fdir / VOCABULARY
    # Fail closed on a missing corpus. Exiting 0 here reported a pass for a check that never ran —
    # the "reports without binding" defect these documents name. Name the root so a wrong --root is
    # distinguishable from a genuinely absent directory.
    if not fdir.is_dir():
        print(
            f"vocabulary check: no {fdir} (looked under --root {args.root}); nothing was checked. "
            f"Run from the repo checkout, or pass --root pointing at one."
        )
        return 1
    if not vocab_path.is_file():
        print(
            f"vocabulary check: no {vocab_path} (looked under --root {args.root}); nothing was "
            f"checked. Run from the repo checkout, or pass --root pointing at one."
        )
        return 1
    vocab_text = vocab_path.read_text(encoding="utf-8")
    # A stale key means a term's regex bans are silently not applied: the check is incomplete, and an
    # incomplete check is not a pass. Report every stale key, run the rest so the author sees the whole
    # picture, and fail below regardless of what the prose scan finds.
    stale = missing_pattern_entries(vocab_text)
    for key in stale:
        print(f"PATTERNS key {key!r} names no ### entry in {VOCABULARY}; its regex bans are not applied")
    never, not_for = parse_bans(vocab_text)
    if not never:
        print("vocabulary.md declares no Never items; the check would pass vacuously")
        return 1
    never_hits, advisory = scan(args.root, never, not_for)

    for h in never_hits:
        print(f"NEVER {h.file}:{h.line_no}: {h.ban.term} (entry: {h.ban.entry}): {h.text[:120]}")
    if not args.quiet_advisory:
        for h in advisory:
            print(f"not-for {h.file}:{h.line_no}: {h.ban.term} — {h.ban.sense[:80]}: {h.text[:100]}")
    counts = Counter(h.ban.term for h in advisory)
    top = ", ".join(f"{t} ({n})" for t, n in counts.most_common(args.top))
    print(
        f"vocabulary check: {len(never)} Never items, {len(not_for)} Not-for items; "
        f"{len(never_hits)} Never hit(s); {len(advisory)} Not-for advisory hit(s)"
        + (f"; most common: {top}" if top else "")
    )
    if stale:
        print(
            f"vocabulary check: {len(stale)} stale PATTERNS key(s); the check is incomplete and does not pass"
        )
    if not args.no_undefined_words:
        candidates = undefined_word_candidates(args.root)
        if candidates:
            listed = ", ".join(f"{w} ({n})" for w, n in candidates)
            print(
                f"undefined-word candidates (advisory, threshold {UNDEFINED_WORD_THRESHOLD}): "
                f"{len(candidates)} word(s) used often with no vocabulary entry: {listed}"
            )
        else:
            print(
                f"undefined-word candidates (advisory, threshold {UNDEFINED_WORD_THRESHOLD}): none"
            )
    return 1 if never_hits or stale else 0


if __name__ == "__main__":
    sys.exit(main())
