"""Every regex in the files that read review and comment text is swept for
super-linear running time.

The scans run on text any commenter can write, so a regex that backtracks
quadratically or worse on a long run of one character class is an availability
problem: two were found and fixed one at a time before this sweep existed. This
file finds them in one pass instead, and keeps finding them.

How it works, so a future regex is covered without anyone remembering to add it:

* every `re.compile(...)` and inline `re.<function>(...)` call in the files in
  `SWEPT_FILES` is found by parsing the source, and its pattern is evaluated in
  the module's own namespace;
* a call whose pattern is built at run time from a caller's argument cannot be
  evaluated that way; it must be listed in `DYNAMIC_SITES` with a provider that
  returns the patterns it is called with, or the test fails;
* for each pattern, adversarial inputs are generated from the pattern itself,
  using the parsed pattern's literal characters, character classes and literal
  strings: a run of each, alternating pairs, an unclosed opener followed by a
  run, repeated groups, each with and without a failing tail;
* a short probe catches exponential growth without risking a hang, a 8 KB probe
  nominates anything slow, and nominated inputs are timed at 16, 32 and 64 KB.

A flagged input is slow in absolute terms at 64 KB, or grows super-linearly
between 32 and 64 KB. The sweep is also run against regexes known to be bad,
because a sweep that cannot find a known fault proves nothing (principle 3).
"""

import ast
import importlib
import re
import sys
import time
import unicodedata
from pathlib import Path

import pytest

import swarm_dispatch

try:  # Python 3.11+
    from re import _parser as sre_parse
except ImportError:  # pragma: no cover
    import sre_parse

_APIS = Path(__file__).resolve().parent
_REPO = _APIS.parents[2]
_SCRIPTS = _REPO / "execution" / "scripts"
for _p in (str(_REPO), str(_APIS), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import approve_pr_as_app  # noqa: E402

# module name -> source file, relative to the repository root
SWEPT_FILES = {
    "swarm_dispatch": "execution/daemons/apis/swarm_dispatch.py",
    "lens_authors": "execution/daemons/apis/lens_authors.py",
    "skill_runner": "execution/daemons/apis/skill_runner.py",
    "approve_pr_as_app": "execution/scripts/approve_pr_as_app.py",
}

_RE_FUNCTIONS = {
    "compile",
    "match",
    "search",
    "sub",
    "subn",
    "findall",
    "finditer",
    "fullmatch",
    "split",
}

PROBE_LENGTHS = (
    16,
    24,
    32,
    40,
    48,
)  # ascending, so an exponential regex stops at the first
PROBE_LIMIT = 0.01
NOMINATE_LENGTH = 8192
NOMINATE_LIMIT = 0.001
SIZES = (16384, 32768, 65536)
ABSOLUTE_LIMIT = 0.25  # seconds, at the largest size
GROWTH_LIMIT = (
    3.2  # doubling the input may at most triple the time (linear is 2, quadratic 4)
)
GROWTH_FLOOR = 0.02  # ignore the ratio while the time is within timer noise
ALPHABET_CAP = 12

# ── finding the regexes ──────────────────────────────────────────────────────


def _dynamic_pr_reference(module):
    return [rf"#\b{12345}\b"]


def _dynamic_delivery_signatures(module):
    return [pattern for pattern, _ in module._DELIVERY_DENIAL_SIGNATURES]


def _dynamic_wildcard_pairs(module):
    return [
        module._wildcard_pairs(token)[0].pattern
        for token in (*module._BLOCKING_VERDICT_WORDS, "[BLOCKING]")
    ]


def _dynamic_lens_diff_patterns(module):
    from review_panel import LENSES

    return [pattern for lens in LENSES for pattern in lens.diff_patterns]


# Patterns the sweep reaches through a provider but that live in a file outside
# SWEPT_FILES. They are reported, not fixed here; each is skipped by the timing
# test with its reason, and `test_out_of_scope_findings_are_still_present` fails
# once one is no longer swept so the entry is removed with it.
OUT_OF_SCOPE_FINDINGS = {
    r"requirements.*\.txt$": (
        "review_panel diff pattern, applied to changed file paths (at most a few "
        "KB each), not to comment text; reported in the PR as a note"
    ),
}


# (module, enclosing function) -> provider of the patterns that call is given.
# A call site that cannot be evaluated statically and is not listed here fails
# `test_every_regex_call_site_is_swept`, so a new one cannot go unread.
DYNAMIC_SITES = {
    ("swarm_dispatch", "_find_open_pr_for_issue"): _dynamic_pr_reference,
    ("skill_runner", "_delivery_failure_reasons"): _dynamic_delivery_signatures,
    ("approve_pr_as_app", "_why_lens_selected"): _dynamic_lens_diff_patterns,
    ("swarm_dispatch", "_wildcard_pairs"): _dynamic_wildcard_pairs,
}


class Site:
    def __init__(self, module, function, lineno, pattern, flags=0):
        self.module = module
        self.function = function
        self.lineno = lineno
        self.pattern = pattern
        self.flags = flags

    @property
    def label(self):
        shown = self.pattern if len(self.pattern) <= 60 else self.pattern[:57] + "..."
        return f"{self.module}:{self.lineno} {self.function} {shown!r}"


def _enclosing_function(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "<module>"


def _flags_of(call, module, relative):
    """The `re` flags a call passes, positionally or as `flags=`."""
    flags = 0
    extra = [*call.args[1:], *[kw.value for kw in call.keywords if kw.arg == "flags"]]
    for node in extra:
        try:
            value = eval(
                compile(ast.Expression(node), relative, "eval"), module.__dict__
            )
        except Exception:
            continue
        if isinstance(value, re.RegexFlag):
            flags |= value
    return flags


def find_sites():
    """(sites, unresolved): every pattern used by an `re.` call in SWEPT_FILES,
    and the (module, function, lineno) of each call whose pattern could not be
    evaluated from the module namespace."""
    sites, unresolved, provided = [], [], set()
    for name, relative in SWEPT_FILES.items():
        module = importlib.import_module(name)
        source = (_REPO / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "re"
                and node.func.attr in _RE_FUNCTIONS
                and node.args
            ):
                continue
            function = _enclosing_function(node, parents)
            try:
                value = eval(
                    compile(ast.Expression(node.args[0]), relative, "eval"),
                    module.__dict__,
                )
            except Exception:
                value = None
            if isinstance(value, str):
                sites.append(
                    Site(
                        name,
                        function,
                        node.lineno,
                        value,
                        _flags_of(node, module, relative),
                    )
                )
            elif isinstance(value, re.Pattern):
                sites.append(
                    Site(name, function, node.lineno, value.pattern, value.flags)
                )
            else:
                key = (name, function)
                unresolved.append((name, function, node.lineno))
                if key in DYNAMIC_SITES and key not in provided:
                    provided.add(key)
                    for pattern in DYNAMIC_SITES[key](module):
                        sites.append(Site(name, function, node.lineno, pattern))
    return sites, unresolved


# ── generating the adversarial inputs ────────────────────────────────────────

_CATEGORY_SAMPLES = {
    "CATEGORY_SPACE": [" ", "\t", "\n"],
    "CATEGORY_NOT_SPACE": ["a"],
    "CATEGORY_DIGIT": ["1"],
    "CATEGORY_NOT_DIGIT": ["a"],
    "CATEGORY_WORD": ["a", "_"],
    "CATEGORY_NOT_WORD": ["-", " "],
}
_GLOBAL_SAMPLES = [
    " ",
    "\t",
    "\n",
    "a",
    "A",
    "1",
    "_",
    "-",
    "*",
    "#",
    ">",
    "<",
    "[",
    "]",
    ":",
    "x",
]


def _walk(subpattern, chars, strings, run):
    """Collect literal characters, literal strings and class samples from a
    parsed pattern. *run* is the literal string being built at this level."""

    def flush():
        if len(run) >= 2:
            strings.append("".join(run))
        run.clear()

    for op, arg in subpattern:
        name = str(op)
        if name == "LITERAL":
            run.append(chr(arg))
            chars.append(chr(arg))
            continue
        flush()
        if name == "NOT_LITERAL":
            chars.append("x" if chr(arg) != "x" else "y")
        elif name == "ANY":
            chars.extend(["a", "\n"])
        elif name == "IN":
            for member_op, member in arg:
                member_name = str(member_op)
                if member_name == "LITERAL":
                    chars.append(chr(member))
                elif member_name == "RANGE":
                    lo, hi = member
                    chars.extend([chr(lo), chr(hi)])
                elif member_name == "CATEGORY":
                    chars.extend(_CATEGORY_SAMPLES.get(str(member), ["a"]))
                elif member_name == "NEGATE":
                    chars.extend(["a", " ", "\n"])
        elif name == "SUBPATTERN":
            _walk(arg[-1], chars, strings, [])
        elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            _walk(arg[-1], chars, strings, [])
        elif name == "BRANCH":
            for branch in arg[1]:
                _walk(branch, chars, strings, [])
        elif name in ("ASSERT", "ASSERT_NOT", "ATOMIC_GROUP"):
            _walk(arg[-1], chars, strings, [])
        elif name == "GROUPREF_EXISTS":
            for branch in arg[1:]:
                if branch is not None:
                    _walk(branch, chars, strings, [])
    flush()


def alphabet_of(pattern, flags=0):
    """(elements, literal strings): up to ALPHABET_CAP single characters and
    short strings that the pattern itself mentions, then generic samples."""
    chars, strings = [], []
    try:
        _walk(sre_parse.parse(pattern, flags), chars, strings, [])
    except Exception:
        pass
    seen, elements = set(), []
    for item in [*chars, *_CATEGORY_SAMPLES["CATEGORY_SPACE"], *_GLOBAL_SAMPLES]:
        if item not in seen:
            seen.add(item)
            elements.append(item)
    strings = list(dict.fromkeys(strings))
    # Prefer the pattern's own characters, but always keep whitespace and one
    # letter: the faults found so far were all long runs of one of those.
    must = [" ", "\t", "\n", "a"]
    chosen = list(dict.fromkeys([*must, *[e for e in elements if e not in must]]))[
        :ALPHABET_CAP
    ]
    return chosen, strings[:4]


def _repeat(unit, length):
    return (unit * (length // max(1, len(unit)) + 1))[:length]


def adversarial_inputs(pattern, flags, length, seeds=()):
    """(label, text) pairs of about *length* characters.

    *seeds* are texts the pattern matches; a run is pumped into each at every
    boundary between tokens, which reaches the structured patterns whose faults
    need a valid prefix before the run."""
    elements, strings = alphabet_of(pattern, flags)
    tails = ("", "x", "\n!")
    out = []
    for element in elements:
        for tail in tails:
            out.append(
                (f"run {element!r} tail {tail!r}", _repeat(element, length) + tail)
            )
    for i, first in enumerate(elements[:8]):
        for second in elements[i + 1 : 8]:
            out.append(
                (
                    f"alternate {first!r} {second!r}",
                    _repeat(first + second, length) + "x",
                )
            )
    # an unclosed opener (a literal string, or a punctuation character the
    # pattern mentions) followed by a run, or by alternating pairs
    openers = list(strings)
    openers += [c for c in elements if not c.isalnum() and not c.isspace()][:3]
    for prefix in openers:
        for element in elements[:6]:
            out.append(
                (
                    f"opener {prefix!r} then {element!r}",
                    prefix + _repeat(element, length) + "x",
                )
            )
        for i, first in enumerate(elements[:5]):
            for second in elements[i + 1 : 5]:
                out.append(
                    (
                        f"opener {prefix!r} then alternate {first!r} {second!r}",
                        prefix + _repeat(first + second, length) + "x",
                    )
                )
    for prefix in strings:
        for element in elements[:4]:
            out.append(
                (
                    f"repeated {prefix!r} + {element!r}",
                    _repeat(prefix + element, length),
                )
            )
    for seed in seeds:
        boundaries = [
            i
            for i in range(len(seed) + 1)
            if i in (0, len(seed))
            or seed[i - 1].isalnum() != seed[i].isalnum()
            or seed[i - 1].isspace() != seed[i].isspace()
        ]
        pumps = [
            " ",
            "\t",
            "a",
            *[c for c in seed if not c.isalnum() and not c.isspace()][:2],
        ]
        for i in boundaries:
            for element in dict.fromkeys(pumps):
                run = _repeat(element, length)
                # the rest of the seed intact, with and without a failing
                # tail; and the run followed by a failure in place of the rest
                for kind, text in (
                    ("whole", seed[:i] + run + seed[i:]),
                    ("whole+x", seed[:i] + run + seed[i:] + "x"),
                    ("cut", seed[:i] + run + "x"),
                ):
                    out.append(
                        (f"seed {seed[:12]!r} pumped {element!r} at {i} {kind}", text)
                    )
    return out


# ── timing ───────────────────────────────────────────────────────────────────


def _operations(compiled):
    return (
        ("search", compiled.search),
        ("match", compiled.match),
        ("sub", lambda text: compiled.sub("", text)),
    )


def _best_time(fn, text, repeats=2):
    best = None
    for _ in range(repeats):
        start = time.perf_counter()
        fn(text)
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    return best


def sweep_pattern(pattern, flags=0, *, report=False, seeds=()):
    """(flagged, worst): the inputs that are super-linear, and the worst time
    seen at the largest size (only measured for every input when *report*)."""
    compiled = re.compile(pattern, flags)
    flagged = []
    worst = 0.0
    # 1. short probes, shortest first: exponential growth shows here, and the
    # first length at which anything is slow ends the probe before it can hang
    for length in PROBE_LENGTHS:
        for label, text in adversarial_inputs(pattern, flags, length, seeds):
            for op_name, op in _operations(compiled):
                if (
                    _best_time(op, text, 1) > PROBE_LIMIT
                    and _best_time(op, text, 2) > PROBE_LIMIT
                ):
                    reason = (
                        f"over {PROBE_LIMIT * 1000:.0f} ms at {len(text)} characters"
                    )
                    return [(f"{op_name} {label}", reason)], float("inf")
    # 2. an 8 KB probe nominates anything slow
    nominated = []
    for label, text in adversarial_inputs(pattern, flags, NOMINATE_LENGTH, seeds):
        for op_name, op in _operations(compiled):
            elapsed = _best_time(op, text, 1)
            if elapsed > NOMINATE_LIMIT:
                nominated.append((op_name, label))
            if elapsed > ABSOLUTE_LIMIT / 2 and not report:
                # linear text of this size takes a millisecond or two: this is
                # slow enough to call, and larger sizes would only cost time
                reason = f"{elapsed:.2f} s at {len(text)} characters"
                return [(f"{op_name} {label}", reason)], elapsed
    # 3. nominated inputs (every input, when reporting) at three sizes
    by_size = (
        {size: dict(adversarial_inputs(pattern, flags, size, seeds)) for size in SIZES}
        if (nominated or report)
        else {}
    )
    candidates = nominated
    if report:
        candidates = [
            (op_name, label)
            for label in by_size[SIZES[-1]]
            for op_name, _ in _operations(compiled)
        ]
    ops = dict(_operations(compiled))
    for op_name, label in candidates:
        times = []
        for size in SIZES:
            times.append(_best_time(ops[op_name], by_size[size][label], 1))
            if times[-1] > ABSOLUTE_LIMIT:
                break  # slow already; larger sizes would only take longer
        worst = max(worst, times[-1])
        if times[-1] > ABSOLUTE_LIMIT:
            flagged.append(
                (
                    f"{op_name} {label}",
                    f"{times[-1]:.2f} s at {SIZES[len(times) - 1]} characters",
                )
            )
        elif (
            len(times) == len(SIZES)
            and times[-2] > GROWTH_FLOOR
            and times[-1] / times[-2] > GROWTH_LIMIT
        ):
            flagged.append(
                (
                    f"{op_name} {label}",
                    f"{times[-2]:.3f} s at {SIZES[-2]} -> {times[-1]:.3f} s at {SIZES[-1]} characters",
                )
            )
        if flagged and not report:
            break
    return flagged, worst


# Texts each structured regex matches. A run is pumped into each at every token
# boundary, which reaches faults that need a valid prefix (a header whose name
# is followed by spaces, a verdict keyword followed by a delimiter and spaces).
# Every seed is checked to match its regex, so a seed cannot silently go stale.
SEEDS = {
    "swarm_dispatch._OWN_HEADER_RE": [
        "**\U0001f916 Waxwing \u2014 Ateles swarm, arch review**"
    ],
    "swarm_dispatch._VERDICT_LINE_RE": ["**SIGNED_OFF**"],
    "swarm_dispatch._VERDICT_LIKE_RE": ["### Verdict: **COMMENT**", "__APPROVE__"],
    "swarm_dispatch._BLOCKING_VERDICT_BOLD_RE": ["** REQUEST_CHANGES **"],
    "swarm_dispatch._BLOCKING_VERDICT_BARE_RE": ["verdict BLOCKED here"],
    "swarm_dispatch._BLOCKING_MARKER_RE": ["**[BLOCKING] scope:** summary"],
    "swarm_dispatch._SPACED_LETTER_RUN_RE": [
        "[ B L O C K I N G ]",
        "[N O N - B L O C K I N G]",
    ],
    "swarm_dispatch._LEADING_REVIEW_MARKER_RE": [
        "<!-- review:pm commit=0123456789abcdef0123456789abcdef01234567 -->"
    ],
    "swarm_dispatch._LENS_MARKER_RE": [
        "<!-- review:pm commit=0123456789abcdef0123456789abcdef01234567 -->"
    ],
    "swarm_dispatch._LENS_SUPERSEDED_RE": [
        "<!-- review:pm-superseded by=0123456789abcdef0123456789abcdef01234567 -->"
    ],
    "swarm_dispatch._AGGREGATION_MARKER_RE": [
        "<!-- vanellus-aggregation commit=0123456789abcdef0123456789abcdef01234567 -->"
    ],
    "swarm_dispatch._AGGREGATION_SUPERSEDED_RE": [
        "<!-- vanellus-aggregation-superseded by=0123456789abcdef0123456789abcdef01234567 -->"
    ],
    "swarm_dispatch._POSTED_ACK_LINE_RE": [
        "Posted: https://github.com/o/r/pull/1#issuecomment-123"
    ],
    "swarm_dispatch._LINE_DECORATION_RE": ["> | <b> x"],
    "swarm_dispatch._KNOWN_TERMINAL_FOOTER_RE": [
        "\n\U0001f916 Generated with [ClaudeCode](https://claude.com/claude-code)"
        "\n\nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
    ],
    "swarm_dispatch._NOT_RECEIVED_RE": ["pm: NOT RECEIVED"],
    "swarm_dispatch._MERGE_REFUSED_RE": ["MERGE REFUSED: a reason"],
    "swarm_dispatch._REVIEWED_COMMIT_RE": ["Reviewed commit: `0123456789abcdef`"],
    "swarm_dispatch._GATE_PENDING": ["GATE_PENDING: pm, arch"],
    "swarm_dispatch._CLOSURE_VERB": ["fixes #12"],
    "lens_authors._VERDICT_SHAPED_RE": ["<!-- review:pm commit=0123 -->"],
    "skill_runner._DIAGNOSTIC_PREFIX_RE": ["error: fatal: x"],
    "skill_runner._SSH_AUTH_DIAGNOSTIC_RE": [
        "git@github.com: permission denied (publickey)."
    ],
    "approve_pr_as_app._EXPECTATION_MARKER_RE": [
        "**review_expectation (pm)** \u2014 what Pavo will verify"
    ],
}


def _seeds_for(module_name, pattern, flags):
    found = []
    module = importlib.import_module(module_name)
    for key, texts in SEEDS.items():
        owner, attr = key.split(".", 1)
        if owner == module_name:
            compiled = getattr(module, attr)
            if (compiled.pattern, compiled.flags) == (
                pattern,
                re.compile(pattern, flags).flags,
            ):
                found.extend(texts)
    return found


# ── the tests ────────────────────────────────────────────────────────────────

SITES, UNRESOLVED = find_sites()
_SWEPT = {}
for _site in SITES:
    _SWEPT.setdefault((_site.pattern, _site.flags), _site)


def test_the_sweep_found_the_regexes():
    """Validate the instrument: it must find a known number of kinds of call."""
    modules = {site.module for site in SITES}
    assert modules == set(SWEPT_FILES)
    assert len(_SWEPT) >= 60
    assert any(site.function == "<module>" for site in SITES)
    assert any(site.function != "<module>" for site in SITES)


def test_every_regex_call_site_is_swept():
    """A call whose pattern cannot be read from the module namespace must be
    listed with a provider; a listed provider must still have a call site."""
    unlisted = [(m, f, n) for m, f, n in UNRESOLVED if (m, f) not in DYNAMIC_SITES]
    assert not unlisted, (
        f"regex call sites with a run-time pattern and no provider: {unlisted}"
    )
    stale = set(DYNAMIC_SITES) - {(m, f) for m, f, _ in UNRESOLVED}
    assert not stale, f"providers with no call site: {stale}"
    for key in DYNAMIC_SITES:
        assert any((site.module, site.function) == key for site in SITES), key


@pytest.mark.parametrize(
    "pattern_and_flags",
    sorted(_SWEPT, key=lambda k: (_SWEPT[k].module, _SWEPT[k].lineno)),
    ids=lambda k: _SWEPT[k].label if k in _SWEPT else str(k),
)
def test_regex_is_not_super_linear_on_adversarial_text(pattern_and_flags):
    pattern, flags = pattern_and_flags
    if pattern in OUT_OF_SCOPE_FINDINGS:
        pytest.skip(OUT_OF_SCOPE_FINDINGS[pattern])
    site = _SWEPT[pattern_and_flags]
    flagged, _ = sweep_pattern(
        pattern, flags, seeds=_seeds_for(site.module, pattern, flags)
    )
    assert not flagged, (
        _SWEPT[pattern_and_flags].label
        + ": "
        + "; ".join(f"{a} ({b})" for a, b in flagged[:3])
    )


# Regexes with known faults: the sweep must flag each of them.
KNOWN_BAD = {
    "exponential spaced letters (the original shape)": r"\[(?:\s*[A-Za-z\-]\s*){2,}\]",
    "quadratic header name and role": (
        "^\\*\\*\U0001f916\\s*(?P<name>[^\\s\u2014\u2013-][^\u2014\u2013\\-\\n]*?)\\s*[\u2014\u2013-]+\\s*Ateles swarm,"
        "\\s*(?P<role>[^\\n]*?\\S)\\s*\\*\\*\\s*$"
    ),
    "quadratic verdict-like line (two adjacent classes)": (
        r"^[#*_\s]*(?:(?i:verdict)\s*[:=-]\s*)?[#*_\s]*(?:APPROVE|COMMENT)"
    ),
    "quadratic verdict-like line (delimiter, then whitespace twice)": (
        r"^[#*_\s]*(?:(?i:verdict)\s*[:=-]\s*[#*_\s]*)?(?:APPROVE|COMMENT)"
    ),
}


KNOWN_BAD_SEEDS = {
    "quadratic header name and role": [
        "**\U0001f916 Waxwing \u2014 Ateles swarm, arch**"
    ],
    "quadratic verdict-like line (two adjacent classes)": ["Verdict: COMMENT"],
    "quadratic verdict-like line (delimiter, then whitespace twice)": [
        "Verdict: COMMENT"
    ],
}


@pytest.mark.parametrize("name", list(KNOWN_BAD))
def test_the_sweep_flags_a_regex_known_to_be_slow(name):
    flagged, _ = sweep_pattern(KNOWN_BAD[name], seeds=KNOWN_BAD_SEEDS.get(name, ()))
    assert flagged, name


def test_out_of_scope_findings_are_still_present():
    swept = {pattern for pattern, _ in _SWEPT}
    for pattern in OUT_OF_SCOPE_FINDINGS:
        assert pattern in swept, f"{pattern!r} is no longer swept: remove its entry"


def test_every_seed_matches_its_regex():
    for key, texts in SEEDS.items():
        owner, attr = key.split(".", 1)
        compiled = getattr(importlib.import_module(owner), attr)
        for text in texts:
            assert compiled.search(text), (key, text)


if __name__ == "__main__":  # pragma: no cover - prints the table for the PR
    rows = []
    for (pattern, flags), site in sorted(
        _SWEPT.items(), key=lambda kv: (kv[1].module, kv[1].lineno)
    ):
        flagged, worst = sweep_pattern(
            pattern, flags, report=True, seeds=_seeds_for(site.module, pattern, flags)
        )
        rows.append((site.module, site.lineno, site.function, worst, len(flagged)))
        print(
            f"{site.module}:{site.lineno} {site.function} worst={worst * 1000:.1f}ms flagged={len(flagged)}",
            flush=True,
        )


# ── rewritten regexes accept exactly what the originals accepted ─────────────
#
# Each `_ORIGINAL_*` below is a FROZEN COPY of the expression as it stood before
# it was rewritten for linear running time. It is never edited: it is the
# reference the rewrite is compared against on 120,000 random strings.

RANDOM_STRINGS = 120_000


def _strings(tokens, seed, longest=12):
    import random

    rng = random.Random(seed)
    for _ in range(RANDOM_STRINGS):
        yield "".join(rng.choice(tokens) for _ in range(rng.randint(1, longest)))


def _same_search(original, rewritten, text):
    a, b = original.search(text), rewritten.search(text)
    assert (a is None) == (b is None), text
    if a:
        assert (a.span(), a.groups()) == (b.span(), b.groups()), text


def test_closure_verb_rewrite_matches_the_original():
    original = re.compile(
        rf"\b(?:{swarm_dispatch._CLOSING_KEYWORDS})\s*:?\s+{swarm_dispatch._ISSUE_REF}",
        re.I,
    )
    tokens = [
        "close",
        "Closes",
        "fixed",
        "fix",
        "resolves",
        ":",
        " ",
        "  ",
        "\t",
        "\n",
        "#12",
        "o/r#3",
        "x",
        "owner/repo#4",
        "\u2028",
    ]
    for text in _strings(tokens, 11):
        _same_search(original, swarm_dispatch._CLOSURE_VERB, text)


def test_not_received_rewrite_matches_the_original():
    names = "|".join(sorted(swarm_dispatch._KNOWN_LENS_NAMES))
    original = re.compile(
        r"\b(?P<lens>"
        + names
        + r")\b\s*\**\s*[:=]\s*\**\s*"
        + swarm_dispatch.NOT_RECEIVED_TOKEN,
        re.IGNORECASE,
    )
    tokens = [
        "pm",
        "arch",
        "security",
        "*",
        "**",
        ":",
        "=",
        " ",
        "\t",
        "\n",
        "NOT RECEIVED",
        "not received",
        "x",
        "\u00a0",
    ]
    for text in _strings(tokens, 12):
        _same_search(original, swarm_dispatch._NOT_RECEIVED_RE, text)


def test_verdict_like_rewrite_matches_the_original():
    alternation = swarm_dispatch._VERDICT_TOKEN_ALT
    original = re.compile(
        r"^[#*_\s]*(?:(?i:verdict)\s*[:=\u2014\u2013-]\s*[#*_\s]*)?(?:"
        + alternation
        + r")(?![A-Za-z0-9]|_[A-Za-z0-9])"
    )
    tokens = [
        "*",
        "#",
        "_",
        " ",
        "  ",
        "\t",
        "\n",
        "Verdict",
        "verdict",
        ":",
        "=",
        "\u2014",
        "-",
        "COMMENT",
        "APPROVE",
        "SIGNED_OFF",
        "x",
        "1",
    ]
    for text in _strings(tokens, 13):
        a, b = original.match(text), swarm_dispatch._VERDICT_LIKE_RE.match(text)
        assert (a is None) == (b is None), text
        if a:
            assert a.span() == b.span(), text


def test_aggregation_marker_rewrite_matches_the_original():
    original = re.compile(
        r"<!--\s*vanellus-aggregation(?P<attrs>(?:\s+[^>]*)?)-->", re.IGNORECASE
    )
    tokens = [
        "<!--",
        "vanellus-aggregation",
        " ",
        "  ",
        "\t",
        "\n",
        "commit=",
        "abc",
        "-->",
        ">",
        "x",
        "--",
        "block_kind=content",
    ]
    for text in _strings(tokens, 14):
        _same_search(original, swarm_dispatch._AGGREGATION_MARKER_RE, text)


def test_verdict_findall_rewrite_matches_the_original():
    """The harness reads its verdict with `findall` and requires exactly one."""
    original = re.compile(
        r"(?im)^\s*Verdict\s*:\s*(APPROVE|REQUEST_CHANGES|COMMENT)\s*$"
    )
    rewritten = re.compile(
        r"(?im)^[^\S\n]*Verdict\s*:\s*(APPROVE|REQUEST_CHANGES|COMMENT)\s*$"
    )
    tokens = [
        "Verdict",
        "verdict",
        ":",
        "APPROVE",
        "COMMENT",
        "REQUEST_CHANGES",
        " ",
        "\t",
        "\n",
        "\r\n",
        "\r",
        "x",
        "\x0b",
        "\u2028",
        "\u00a0",
        "\n\n",
    ]
    for text in _strings(tokens, 15, longest=14):
        assert original.findall(text) == rewritten.findall(text), text
    source = (_REPO / SWEPT_FILES["skill_runner"]).read_text(encoding="utf-8")
    assert rewritten.pattern in source


# ── the verdict-like regex, through every entry point a commenter reaches ────

TIME_LIMIT = 0.5
WHITESPACE_RUN = 20_000  # the old regex takes seconds here and still finishes
OTHER_LOGIN = "some-other-account"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
RUNS = {
    "spaces": " ",
    "tabs": "\t",
    "stars": "*",
    "underscores": "_",
    "hashes": "#",
    "mixed": " *_#\t",
}


def _lens_reply(tail, *, lens="arch", agent_header="Waxwing"):
    marker = swarm_dispatch.compose_lens_review_marker(lens, HEAD_SHA)
    return (
        f"{marker}\n**\U0001f916 {agent_header} \u2014 Ateles swarm, {lens} review**\n"
        f"**SIGNED_OFF**\n\n{tail}\n"
    )


def _entry_points():
    def own(body):
        return swarm_dispatch.lens_own_verdict(body, lens_agent="waxwing")

    def explicit(body):
        return swarm_dispatch.lens_explicit_objection(body, lens_agent="waxwing")

    def sign_off(body):
        return swarm_dispatch.sign_off_is_warranted(body, lens_agent="waxwing")

    def approval_tool(body):
        comment = {
            "id": 1,
            "body": body,
            "user": {"login": OTHER_LOGIN},
            "html_url": "https://github.com/o/r/pull/1#issuecomment-1",
        }
        return approve_pr_as_app.find_unadmitted_objections(
            comments=[comment],
            head_sha=HEAD_SHA,
            authors=frozenset({"swarm-lens-account"}),
        )

    return {
        "lens_own_verdict": own,
        "lens_explicit_objection": explicit,
        "sign_off_is_warranted": sign_off,
        "find_unadmitted_objections": approval_tool,
    }


@pytest.mark.parametrize("run", list(RUNS))
@pytest.mark.parametrize("entry", list(_entry_points()))
def test_verdict_keyword_delimiter_then_a_long_run_is_fast(entry, run):
    for delimiter in (":", "="):
        body = _lens_reply("Verdict" + delimiter + RUNS[run] * WHITESPACE_RUN + "x")
        start = time.perf_counter()
        _entry_points()[entry](body)
        assert time.perf_counter() - start < TIME_LIMIT, (entry, run, delimiter)


# ── cost of normalising a large text ─────────────────────────────────────────
#
# Every lens-marked comment from any account goes through the scans, so the
# cost of normalising a large text matters as much as a regex's shape. The
# text below is NFKC-expanding (ligatures) but pure ASCII afterwards.

# U+FDFA expands to eighteen non-ASCII letters under NFKC, the worst expansion
# the scans meet; the mix adds the ligatures and compatibility forms that expand
# a little. 65,000 characters is just under GitHub's comment limit.
EXPANDING_UNITS = {
    "arabic ligature": chr(0xFDFA),
    "mixed ligatures": chr(0xFB01)
    + "nd "
    + chr(0xFB02)
    + "ow "
    + chr(0xFB03)
    + "x "
    + chr(0xFDFA)
    + " "
    + chr(0x33FF)
    + " ",
}


def _expanding_reply(unit, size=65_000):
    return _lens_reply((unit * (size // len(unit) + 1))[:size])


@pytest.mark.parametrize("unit", list(EXPANDING_UNITS))
@pytest.mark.parametrize("entry", list(_entry_points()))
def test_a_compatibility_form_heavy_comment_is_scanned_quickly(entry, unit):
    body = _expanding_reply(EXPANDING_UNITS[unit])
    start = time.perf_counter()
    _entry_points()[entry](body)
    assert time.perf_counter() - start < 1.5, (entry, unit)


@pytest.mark.parametrize("unit", list(EXPANDING_UNITS))
def test_the_veto_predicates_on_an_expanding_text_are_each_quick(unit):
    body = _expanding_reply(EXPANDING_UNITS[unit])
    for fn in (
        swarm_dispatch.output_has_blocking_verdict,
        swarm_dispatch.body_has_blocking_findings,
    ):
        start = time.perf_counter()
        fn(body)
        assert time.perf_counter() - start < 1.5, fn.__name__


def test_the_forms_of_a_text_are_built_once_for_the_predicates_that_share_it():
    body = _expanding_reply(EXPANDING_UNITS["mixed ligatures"], 2048)
    first = swarm_dispatch._veto_scan_forms(body)
    assert swarm_dispatch._veto_scan_forms(body) is first


def _reference_forms(text):
    """FROZEN COPY of `_veto_scan_forms` before it shared its normalisation:
    every form built independently, with the per-character mark strip."""

    def fold(reading, underscores):
        sd = swarm_dispatch
        t = sd._SPACE_THEN_LOW_LINE_RE.sub("_", text) if underscores else text
        t = t.translate(sd._VETO_PRE_NFKC_TRANSLATION)
        decomposed = unicodedata.normalize("NFKD", t)
        stripped = "".join(
            ch
            for ch in decomposed
            if ch not in sd._ZERO_WIDTH_CHARS
            and unicodedata.category(ch) not in sd._STRIPPED_UNICODE_CATEGORIES
        )
        folded = unicodedata.normalize("NFKC", stripped).translate(
            sd._VETO_TRANSLATIONS[reading]
        )
        return sd._SPACED_LETTER_RUN_RE.sub(sd._collapse_spaced_run, folded)

    forms = [
        swarm_dispatch._normalize_for_blocking_scan(text),
        unicodedata.normalize("NFKC", text),
    ]
    for reading in ("primary", "alternate", "keep"):
        for underscores in (True, False):
            forms.append(fold(reading, underscores))
    return set(forms)


def test_shared_normalisation_builds_the_same_forms_as_building_each_alone():
    tokens = [
        "[BLOCKING]", "[", "]", "BLOCKED", "REQUEST_CHANGES", "_", " ", "\t", "\n", "a", "B",
        "\ufb01", "\ufb03", "\u2460", "\uff22", "\u00e9", "e\u0301", "\u200d", "\u00ad",
        "\u03f2", "\u03f9", "\u2017", " \u0332", "\u00a0\u0333", "\u0392", "\u0415", "\u1d05",
        "\u04c0", "\u2581", "\u2045", "\u0416", "-", "*", "\u2024", "\u2025",
    ]  # fmt: skip
    for text in _strings(tokens, 21, longest=10):
        assert set(swarm_dispatch._veto_scan_forms(text)) == _reference_forms(text), (
            text
        )


def test_mark_strip_helper_matches_the_per_character_strip():
    sd = swarm_dispatch
    tokens = [
        "a",
        "e\u0301",
        "\u200d",
        "\u00ad",
        "\u0332",
        "\u20dd",
        "\u0915\u093f",
        "\u0416",
        " ",
        "\ufeff",
        "-",
    ]
    for text in _strings(tokens, 22, longest=12):
        decomposed = unicodedata.normalize("NFKD", text)
        expected = "".join(
            ch
            for ch in decomposed
            if ch not in sd._ZERO_WIDTH_CHARS
            and unicodedata.category(ch) not in sd._STRIPPED_UNICODE_CATEGORIES
        )
        assert sd._strip_marks_and_format(decomposed) == expected, text


def _reference_wildcard_hit(text, token, marker):
    """FROZEN COPY of the wildcard scan before it looked only at windows around
    an aligned pair of the token's letters: every window around a non-ASCII
    character is tried."""
    sd = swarm_dispatch
    n = len(token)
    if len(text) < n or not sd._NON_ASCII_RE.search(text):
        return False
    seen = set()
    for m in sd._NON_ASCII_RE.finditer(text):
        for start in range(
            max(0, m.start() - n + 1), min(m.start(), len(text) - n) + 1
        ):
            if start in seen:
                continue
            seen.add(start)
            if sd._wildcard_window_matches(text, start, token, marker=marker):
                return True
    return False


def test_pair_anchored_wildcard_scan_agrees_with_trying_every_window():
    tokens_by_scan = [(word, False) for word in swarm_dispatch._BLOCKING_VERDICT_WORDS]
    tokens_by_scan.append(("[BLOCKING]", True))
    alphabet = [
        "BLOCKED", "BLOCK", "ED", "BL", "REQUEST_CHANGES", "CHANGES_REQUESTED", "_",
        "[BLOCKING]", "BLOCKING", "[", "]", "NON-", "NON", "-", "**", " ", "\n",
        "a", "e", "b", "o", "\u0416", "\u03a9", "\u2212", "\u0392", "\u2581", "x",
    ]  # fmt: skip
    seen_hit = seen_miss = 0
    for text in _strings(alphabet, 31, longest=8):
        for token, marker in tokens_by_scan:
            got = swarm_dispatch._wildcard_token_hit(text, token, marker=marker)
            want = _reference_wildcard_hit(text, token, marker)
            assert got == want, (text, token)
            seen_hit += got
            seen_miss += not got
    assert seen_hit > 100 and seen_miss > 100  # both outcomes are exercised
