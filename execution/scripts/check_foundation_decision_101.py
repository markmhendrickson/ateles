#!/usr/bin/env python3
"""Check that decision 101's ruled shape is registered in data_model.md.

Decision 101 rules that ``principal_binding`` carries credential fields on the
edge (one edge per credential). Marking the register row **ruled** without
amending ``data_model.md#relationships`` is false readiness for G17 sequencing:
the status token would unblock stage-1 registration before a writable shape
exists. This check binds the two effects.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")
_DECISION_ROW_RE = re.compile(r"^\|\s*101\s*\|")
_PRINCIPAL_BINDING_ROW_RE = re.compile(
    r"^\|\s*`principal_binding`\s*\|(?P<body>.*)\|\s*$"
)

# Tokens the relationships row must carry when row 101 is ruled.
REQUIRED_FIELD_TOKENS = (
    "credential_kind",
    "credential_value",
    "credential_issuer",
)
# Expiry may appear as expires_at or the word expiry.
EXPIRY_TOKEN_RE = re.compile(r"expires_at|\bexpiry\b", re.I)
# One edge per credential / several edges cardinality.
CARDINALITY_RE = re.compile(
    r"one edge per credential|several edges|many edges", re.I
)
# Resolution: match kind+value → principal.
#
# Every alternative must carry a RESOLUTION verb or the explicit
# credential-to-principal phrase. An earlier revision allowed a bare
# `.{0,80}principal` tail, which the word "attribution" elsewhere in the same
# row satisfied -- so deleting the resolution language left the check green.
RESOLUTION_RE = re.compile(
    r"kind\+value(?:\[\+issuer\])?\s*(?:→|->)\s*principal"
    r"|match(?:es|ing)?\s+live\s+edges\s+on\s+kind"
    r"|resolv\w*\s+(?:a\s+)?credential\w*\s+to\s+(?:a\s+|the\s+)?principal"
    r"|credential-to-principal\s+resolution",
    re.I,
)
LEGACY_ENDPOINTS_RE = re.compile(r"agent\s*→\s*principal", re.I)
LEGACY_MEANING_RE = re.compile(
    r"the principal the agent acts as", re.I
)


class CorpusProblem(Exception):
    """The decision-101 corpus files are missing or unreadable."""


def decision_101_row(conformance_text: str) -> tuple[int, list[str]] | None:
    for no, line in enumerate(conformance_text.splitlines(), 1):
        if not _DECISION_ROW_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return no, cells
    return None


def principal_binding_relationships_row(
    data_model_text: str,
) -> tuple[int, str] | None:
    in_relationships = False
    for no, line in enumerate(data_model_text.splitlines(), 1):
        if line.startswith("## Relationships"):
            in_relationships = True
            continue
        if in_relationships and line.startswith("## "):
            break
        if not in_relationships:
            continue
        match = _PRINCIPAL_BINDING_ROW_RE.match(line)
        if match:
            return no, match.group("body")
    return None


def row_is_legacy_only(row_body: str) -> bool:
    """True when the row still matches the pre-101 fieldless acts-as shape."""
    has_legacy = bool(LEGACY_ENDPOINTS_RE.search(row_body)) and bool(
        LEGACY_MEANING_RE.search(row_body)
    )
    has_fields = all(token in row_body for token in REQUIRED_FIELD_TOKENS)
    return has_legacy and not has_fields


def check_data_model_row(path: Path, row_no: int, row_body: str) -> list[str]:
    problems: list[str] = []
    if row_is_legacy_only(row_body):
        problems.append(
            f"{path}:{row_no}: decision-101-data-model — principal_binding "
            "row still matches the legacy agent → principal / "
            '"the principal the agent acts as" shape with no credential fields'
        )
        return problems

    for token in REQUIRED_FIELD_TOKENS:
        if token not in row_body:
            problems.append(
                f"{path}:{row_no}: decision-101-data-model — principal_binding "
                f"row missing `{token}`"
            )
    if not EXPIRY_TOKEN_RE.search(row_body):
        problems.append(
            f"{path}:{row_no}: decision-101-data-model — principal_binding "
            "row missing expiry / `expires_at`"
        )
    if not CARDINALITY_RE.search(row_body):
        problems.append(
            f"{path}:{row_no}: decision-101-data-model — principal_binding "
            "row missing one-edge-per-credential cardinality language"
        )
    if not RESOLUTION_RE.search(row_body):
        problems.append(
            f"{path}:{row_no}: decision-101-data-model — principal_binding "
            "row missing kind+value → principal resolution language"
        )
    return problems


AUTHORITY_HEADING = (
    "### What the credential binding carries, and what a check reads "
    "to resolve a credential to a principal"
)

# The one paragraph that states the endpoint assignment. Scoping to this
# paragraph (rather than the whole ruling section) is deliberate: the section
# also carries the field table, the two open-question paragraphs about the
# acts-as edge's kind and endpoints, and a forward-looking summary — all of
# which legitimately use the words "AAuth", "acts-as", "agent", and
# "operator" together without asserting the resolver's endpoint rule. Widening
# the scope to the whole section produces false positives on that legitimate
# prose; narrowing it to this one paragraph is where decision 101 actually
# states the rule (`**Endpoint / source.**`).
ENDPOINT_PARAGRAPH_LEAD = "**Endpoint / source.**"

# The endpoint assignment decision 101 settles, and the defect class that makes
# it worth a mechanical assertion: swapping the two endpoints (AAuth → operator,
# acts-as → agent) leaves every field, cardinality, and resolution token intact,
# so every other assertion here stays green on a corpus that says the opposite
# of the ruling. Registration is one-way under G26 (`migration.md`), so a swap
# that reaches stage 1 is not correctable afterwards. Attribution requires the
# AAuth edge to end at the agent (or a write attributes to the operator and
# A-for-B is unrecordable); decision 48's counting rule requires the acts-as
# edge to end at the operator (or two agents under one operator count as two
# interests). One edge cannot end at both, which is why there are two.
#
# Two generations of this check both matched anywhere in the ruling body, on
# whole sentences, and both broke on that:
#
#   (1) "contradiction by addition" — a plain-English sentence stating the
#       inverse assignment, appended to the SAME paragraph after the canonical
#       sentence, coexisted with it; the positive regex kept firing off the
#       untouched original and the appended sentence used no phrase the old
#       regexes recognized at all, so it was invisible either way.
#   (2) "regex evasion by markdown" — bold-splitting the words in the
#       canonical sentence (`op**e**rator`) broke the regex on the one
#       sentence that mattered, while an incidental, unrelated mention of the
#       correct word elsewhere in the (wider, whole-section) scan kept the
#       positive regex satisfied.
#
# The fix has three parts. First, scope to the one paragraph that states the
# rule (above) rather than the whole section, which is what makes the third
# part precise enough to use without drowning in the section's other
# legitimate prose. Second, normalize markdown emphasis out of that paragraph
# before matching, so a marker run cannot hide a word from any pattern.
# Third, read the paragraph at CLAUSE granularity (split on sentence and
# clause boundaries) rather than whole-sentence or whole-body: for each
# clause that names a kind ("AAuth" / "acts-as"), collect every principal
# this corpus recognizes as naming the agent or the operator — "operator" and
# "human" are the same principal under this corpus's own settled vocabulary
# ("The human principal is an `operator` entity", `authority_model.md#principals`)
# — and require the UNION across every such clause, for each kind, to be
# exactly the one expected principal. A clause the old regexes did not
# recognize but that still names a kind and a principal is caught by this,
# because the union test does not depend on the clause matching a known
# phrase shape — it only needs the two words to co-occur in a clause about
# that kind. Zero clauses naming a kind reads as "not stated"; a union
# containing the wrong principal, or both, reads as "swapped" or "ambiguous"
# and is refused either way, since decision 101 states each endpoint once,
# not "at least once and never the other".
_EMPHASIS_RE = re.compile(r"[*_]+")
# Sentence and clause boundaries: full stops/semicolons, and the two
# coordinating words this corpus's own prose uses to join two independent
# clauses about different kinds in one sentence ("... so the AAuth kind
# yields the agent ... and the acts-as kind yields the operator ...").
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.;])\s+|\s+so\s+|\s+and\s+(?=the\s)")

# Anchored to a PREDICATE next to the kind name, not bare co-occurrence in the
# clause. A first version of this fix used co-occurrence alone (does the
# clause mention "acts-as" and a principal anywhere?) and false-positived on
# this corpus's own prose: "...reached through that agent's separate acts-as
# `principal_binding`, ... whose endpoint is the `operator`" legitimately
# mentions "agent" (possessive, naming the OTHER edge's principal) and
# "acts-as" in one clause while still correctly stating the acts-as endpoint
# is the operator. Requiring the principal to be the object of an
# endpoint-assigning verb *phrase* that itself sits next to the kind name
# (either order, within a bounded span) rejects that incidental mention while
# still catching a clause the old rigid phrase regexes did not recognize —
# recall over precision was the wrong axis; precision on the predicate,
# recall on the phrasing, is the combination that closes both attacks without
# reopening false positives on this corpus's real prose.
#
# "human"/"human operator"/"human principal" is this corpus's own settled
# synonym for `operator` (`authority_model.md#principals`: "The human
# principal is an `operator` entity"), not an invented alias — recognizing it
# closes the specific evasion of naming the operator by that word instead of
# "operator" itself, without opening the check to arbitrary paraphrase.
_KIND_PREDICATE_RE = {
    "AAuth": re.compile(
        r"AAuth[^.;]{0,80}?(?:kind\s+yields|resolves?[^.;]{0,60}?to)\s+"
        r"(?:that\s+|the\s+)?`?(agent|operator)\b"
        r"|AAuth[^.;]{0,160}?identifies\s+(?:that\s+|the\s+)?`?(human)\b",
        re.I,
    ),
    "acts-as": re.compile(
        r"acts-as[^.;]{0,60}?(?:kind\s+yields|whose\s+endpoint\s+is|"
        r"edge[^.;]{0,20}?ends\s+at)\s+(?:that\s+|the\s+)?`?(agent|operator)\b"
        r"|(?:yields|ends\s+at|endpoint\s+is)\s+(?:that\s+|the\s+)?`?"
        r"(agent|operator)\b[^.;]{0,80}?acts-as",
        re.I,
    ),
}
# "human" reads as `operator` under this corpus's own settled vocabulary.
_PRINCIPAL_ALIAS = {"agent": "agent", "operator": "operator", "human": "operator"}
EXPECTED_PRINCIPAL = {"AAuth": "agent", "acts-as": "operator"}

# A `*` marker landing between two letters of what reads as one word is not
# valid emphasis on either side of it (CommonMark's own left/right-flanking
# rule already refuses to open or close emphasis mid-word for this reason) —
# it is markdown that cannot render as bold or italic at all, only text with
# stray asterisks in it. That is exactly the shape of bold-splitting a word
# to dodge a regex (`op**e**rator`, `ag**e**nt`): normalizing emphasis away
# (above) recovers the word for matching, but the split itself is still a
# defect in the corpus independent of what the recovered word says, so it is
# reported on its own rather than only being silently repaired. `_` is
# deliberately excluded: this corpus quotes snake_case identifiers constantly
# (`credential_kind`, `principal_binding`, ...) and CommonMark itself
# suppresses intraword `_` emphasis, so an underscore between letters is
# ordinary code, never a hiding vector here.
_WORD_INTERNAL_STAR_RE = re.compile(r"[A-Za-z]\*{1,3}[A-Za-z]")


def check_no_word_internal_emphasis(path: Path, heading_no: int, body: str) -> list[str]:
    """Flag a `*` marker splitting a word, in the decision-101 ruling body."""
    hits = sorted({m.group(0) for m in _WORD_INTERNAL_STAR_RE.finditer(body)})
    if not hits:
        return []
    quoted = ", ".join(repr(h) for h in hits)
    return [
        f"{path}:{heading_no}: decision-101-markdown — the ruling section "
        f"contains emphasis markers splitting a word ({quoted}); this renders "
        "as stray asterisks, not bold or italic text, and can hide a word "
        "from a plain-text reader or reviewer diff even where it does not "
        "change what a normalized match reads"
    ]


def _strip_markdown_emphasis(text: str) -> str:
    """Remove `*`/`_` run markers so bold/italic-splitting cannot hide text.

    `op**e**rator` and `ag**e**nt` still read as "operator" and "agent" to a
    person and to any renderer; stripping the marker characters (never
    alphanumeric, so no word boundary is lost) makes them read that way to
    the checks below too.
    """
    return _EMPHASIS_RE.sub("", text)


def authority_ruling_body(text: str) -> str | None:
    """The body of the decision-101 ruling section, heading to next heading.

    The endpoint assignment is stated inside this section, so the assertion
    reads the section rather than the whole document — a correct sentence
    elsewhere must not vouch for a swapped one here.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() != AUTHORITY_HEADING:
            continue
        body: list[str] = []
        for follow in lines[i + 1 :]:
            if follow.startswith("## ") or follow.startswith("### "):
                break
            body.append(follow)
        return "\n".join(body)
    return None


def endpoint_paragraph(body: str) -> str | None:
    """The ``**Endpoint / source.**`` paragraph within the ruling body.

    A paragraph is bounded by a blank line, matching this corpus's own
    convention of one bold-lead paragraph per claim (`**Ruled**`,
    `**Rotation is staged...**`, `**Endpoint / source.**`, ...). Matched on
    the raw body so the lead phrase's own emphasis markers still match;
    callers normalize emphasis afterward, on the paragraph text only.
    """
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith(ENDPOINT_PARAGRAPH_LEAD):
            para: list[str] = []
            for follow in lines[i:]:
                if follow.strip() == "":
                    break
                para.append(follow)
            return " ".join(para)
    return None


def _principal_union(paragraph: str, kind: str) -> set[str]:
    """Every principal a predicate next to ``kind`` assigns, across clauses.

    The paragraph is split into clauses first so that a compound sentence
    naming both kinds together — "so the AAuth kind yields the agent ... and
    the acts-as kind yields the operator" — attributes each principal to its
    own kind's clause rather than to both. Within each clause, the match is
    anchored to an endpoint-assigning predicate ("kind yields", "resolves ...
    to", "endpoint is", "ends at", "identifies") sitting next to the kind
    name, not to bare co-occurrence — a clause naming the kind and, via an
    unrelated predicate, some other principal (describing a different edge)
    does not count. This is deliberately broader than the old rigid whole-
    phrase regexes it replaces, since a phrasing the checker does not
    recognize is exactly what defeated it before; it stays precise by
    requiring the predicate, not just the words, to sit beside the kind name.
    """
    predicate_re = _KIND_PREDICATE_RE[kind]
    found: set[str] = set()
    for clause in _CLAUSE_SPLIT_RE.split(paragraph):
        for match in predicate_re.finditer(clause):
            for group in match.groups():
                if group:
                    found.add(_PRINCIPAL_ALIAS[group.lower()])
    return found


def _check_one_endpoint(
    path: Path,
    heading_no: int,
    paragraph: str,
    *,
    kind: str,
    label: str,
) -> list[str]:
    expected = EXPECTED_PRINCIPAL[kind]
    other = next(p for p in ("agent", "operator") if p != expected)
    found = _principal_union(paragraph, kind)

    if not found:
        return [
            f"{path}:{heading_no}: decision-101-endpoints — the "
            f"**Endpoint / source.** paragraph does not state that {label} "
            f"resolves to the **{expected}**"
        ]

    if found == {expected}:
        return []

    if found == {other}:
        return [
            f"{path}:{heading_no}: decision-101-endpoints — {label} is "
            f"stated to resolve to the {other}; decision 101 ends that edge "
            f"at the **{expected}**"
            + (
                " (attribution records a write as A-for-B, which an operator "
                "endpoint makes unrecordable)"
                if kind == "AAuth"
                else " (decision 48's counting rule reads it, and an agent "
                "endpoint would make two agents under one operator two "
                "interests)"
            )
        ]

    # found is a superset containing both — some clause naming this kind
    # names the wrong principal (or both), alongside a clause that names the
    # right one. Correct and swapped/ambiguous cannot coexist in one
    # paragraph without the paragraph contradicting itself.
    return [
        f"{path}:{heading_no}: decision-101-endpoints — {label} is stated "
        f"inconsistently in the **Endpoint / source.** paragraph: at least "
        f"one clause names {kind}'s endpoint as the **{expected}** and at "
        f"least one other names it as the **{other}**. Decision 101 states "
        "this once; a second, disagreeing clause is not corroboration, it is "
        "a contradiction — whether it repeats, paraphrases, or reverses the "
        "canonical statement"
    ]


def check_authority_endpoints(path: Path, heading_no: int, body: str) -> list[str]:
    """Assert which principal each of the two binding kinds resolves to.

    Reads the single ``**Endpoint / source.**`` paragraph out of the ruling
    body, strips markdown emphasis, and — per kind — requires every clause
    that names it to agree on exactly the one principal decision 101 assigns.
    No clause naming a kind reads as "not stated"; clauses that disagree
    (whether the disagreement is a clean swap or a mix of correct and
    incorrect) are refused with a diagnostic naming both sides.
    """
    paragraph = endpoint_paragraph(body)
    if paragraph is None:
        return [
            f"{path}:{heading_no}: decision-101-endpoints — the ruling "
            f"section carries no {ENDPOINT_PARAGRAPH_LEAD!r} paragraph "
            "stating which principal each binding kind resolves to"
        ]

    normalized = _strip_markdown_emphasis(paragraph)
    problems: list[str] = []
    problems.extend(
        _check_one_endpoint(
            path,
            heading_no,
            normalized,
            kind="AAuth",
            label="the AAuth credential's endpoint",
        )
    )
    problems.extend(
        _check_one_endpoint(
            path,
            heading_no,
            normalized,
            kind="acts-as",
            label="the acts-as binding's endpoint",
        )
    )
    return problems


def authority_ruling_section(text: str) -> tuple[int, str] | None:
    """The decision-101 ruling section in ``authority_model.md``, with its opener.

    Returns (line number of the heading, the first non-blank line beneath it),
    or None when the heading is absent.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == AUTHORITY_HEADING:
            for follow in lines[i + 1 :]:
                if follow.strip():
                    return i + 1, follow
            return i + 1, ""
    return None


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    conformance_path = fdir / "conformance.md"
    data_model_path = fdir / "data_model.md"
    authority_path = fdir / "authority_model.md"
    if not conformance_path.is_file() or not data_model_path.is_file():
        raise CorpusProblem(
            f"expected {conformance_path} and {data_model_path} under --root {root}"
        )

    problems: list[str] = []
    conformance_text = conformance_path.read_text(encoding="utf-8")
    row = decision_101_row(conformance_text)
    if row is None:
        problems.append(
            f"{conformance_path}:1: decision-101-register — no register row "
            'beginning "| 101 |"'
        )
        return problems

    row_no, cells = row
    status = cells[4] if len(cells) > 4 else ""
    ruled = "**ruled**" in status.lower()

    data_model_text = data_model_path.read_text(encoding="utf-8")
    binding = principal_binding_relationships_row(data_model_text)
    if binding is None:
        if ruled:
            problems.append(
                f"{data_model_path}:1: decision-101-data-model — missing "
                "`principal_binding` relationships row while register row 101 "
                "is **ruled**"
            )
        return problems

    binding_no, binding_body = binding
    if ruled:
        problems.extend(
            check_data_model_row(data_model_path, binding_no, binding_body)
        )

    # The document that STATES the ruling. Without this the whole of
    # authority_model.md's field table, endpoint rule, and resolver outcomes
    # could be deleted or reverted to `**Open.**` and this check would stay
    # green on the data_model row alone -- the ruled-but-not-implemented
    # divergence, on the document that is the implementation of the ruling.
    if ruled:
        if not authority_path.is_file():
            problems.append(
                f"{authority_path}:1: decision-101-authority — missing while "
                "register row 101 is **ruled**"
            )
        else:
            authority_text = authority_path.read_text(encoding="utf-8")
            section = authority_ruling_section(authority_text)
            if section is None:
                problems.append(
                    f"{authority_path}:1: decision-101-authority — no section "
                    f'"{AUTHORITY_HEADING.lstrip("# ")}" while register row 101 '
                    "is **ruled**"
                )
            else:
                heading_no, opener = section
                if not opener.lstrip().startswith("**Ruled"):
                    problems.append(
                        f"{authority_path}:{heading_no}: decision-101-authority "
                        "— the ruling section must open with \"**Ruled\" while "
                        "register row 101 is **ruled**; it opens "
                        f"{opener.strip()[:40]!r}"
                    )
                body = authority_ruling_body(authority_text)
                if body is not None:
                    problems.extend(
                        check_no_word_internal_emphasis(
                            authority_path, heading_no, body
                        )
                    )
                    problems.extend(
                        check_authority_endpoints(
                            authority_path, heading_no, body
                        )
                    )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    args = parser.parse_args(argv)

    try:
        problems = check(args.root)
    except CorpusProblem as exc:
        print(f"decision 101 check: {exc}", file=sys.stderr)
        return 1

    for problem in problems:
        print(problem)
    print(f"decision 101 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
