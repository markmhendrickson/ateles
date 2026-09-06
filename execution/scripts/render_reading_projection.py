#!/usr/bin/env python3
"""render_reading_projection.py — generate the reading projection from the conformance matrix.

Decision 66 (2026-09-06, ``docs/foundation/conformance.md#what-a-review-reads-is-a-projection-of-\
these-documents-not-a-shortened-copy-of-them``) ruled that the canonical documents under
``docs/foundation/`` keep their full argument and are bounded by nothing mechanical, and that what a
review prompt inlines is a **reading projection**: generated, never authored, one entry per rule —
the rule's own statement and its anchor, the argument left at the anchor. ``foundation.py``'s
``MAX_DOC_CHARS`` and ``MAX_BLOCK_CHARS`` constrain the projection, not the sources.

The extraction key is ``conformance_suite.md``'s conformance matrix. Every id-bearing row already
pairs one rule with the anchor of the heading that owns it — that pairing is the matrix's first two
cells, and ``check_foundation_rule_coverage.py`` (a contract) fails on a rule-bearing heading with no
row and on a row whose pointer resolves to nothing. So the mapping this generator needs exists and is
already maintained, and generating from it gives the forcing function decision 66 states plainly: a
rule missing from the matrix is also missing from what agents read.

Direction of truth is the render-target pattern of ``render_plan_docs.py`` and
``render_agent_docs.py``: a canonical source is authored, a mirror is generated, and ``--check``
holds them equal. The projection is never edited in place — regenerate it.

Layout under ``docs/foundation/projection/``:

    README.md          the index: every home document, its row count, its size, and how to select
    <document>.md      one file per canonical document, named for it, holding that document's rules
    lenses.md          an index by conformance class (M / R / U / P / D) — row ids and the file each
                       lives in, so a lens can select by what a rule is testable by. An index and
                       not a second copy: no rule statement appears twice in this directory

The projected ``conformance.md`` also carries the reading list's own tables, verbatim (its prose,
which argues why the kernel is three documents, stays canonical). That makes this directory a
foundation root in its own right: ``reading_block()`` pointed at it selects the same documents and
inlines their projections.

One file per canonical document is what makes the projection substitutable in
``foundation.py``'s reading list: ``select_readings()`` keys on document paths, so a projected
document whose basename matches its canonical one drops into the same slot. The whole set is the
directory; a per-document read is one file; a per-lens read is ``lenses.md``.

Nothing here is authored. Every projected line is the matrix row's own text, verbatim, with its
anchor rendered as a link back to the canonical home. Where a row's rule cell is a continuation
("the same: …", "rule 2: …", "obligation 3: …"), it inherits the anchor of the nearest preceding row
in its section, which is how the matrix itself reads.

Usage:
    render_reading_projection.py             # matrix → docs/foundation/projection/
    render_reading_projection.py --check     # exit 1 if disk differs from what the sources generate
    render_reading_projection.py --measure   # print the sizes against the two caps and exit 0

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FOUNDATION_DIR = Path("docs/foundation")
SUITE = "conformance_suite.md"
PROJECTION_DIR = FOUNDATION_DIR / "projection"

MATRIX_HEADING = "## The conformance matrix"

# The two caps decision 66 re-aimed at this artefact. Kept in step with
# execution/daemons/apis/foundation.py, which is where a review prompt reads them from; duplicated
# here as literals only so this script stays stdlib-only and importable without the daemon package.
MAX_DOC_CHARS = 12_000
MAX_BLOCK_CHARS = 40_000

# Files in the projection that no reading-list row can select, so MAX_DOC_CHARS — a cap on what one
# *selected* document costs a review prompt — does not apply to them. README.md is the index,
# and lenses.md is an index of row ids; a review is handed the documents the list selects, never
# these two.
NOT_SELECTABLE = ("README.md", "lenses.md")

# A matrix row id: two letters, a number, optionally a letter suffix (DM-10b, GW-3a, PY-5a).
_ROW_ID_RE = re.compile(r"^[A-Z]{2}-[0-9]+[a-z]?$")
# A backticked foundation citation inside a cell: `document.md#anchor`.
_ANCHOR_RE = re.compile(r"`([A-Za-z0-9_]+\.md)#([A-Za-z0-9_\-]+)`")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")


class MissingCorpus(Exception):
    """The sources this generator reads are absent.

    Exiting 0 on a missing corpus would report a pass for a generation that never ran — the
    "reports without binding" defect the foundation names, and the same failure mode
    ``check_foundation_anchors.py`` refuses. This fails closed instead, naming what it looked for.
    """


def anchor(heading: str) -> str:
    """GitHub-style anchor for a heading — the same derivation check_foundation_anchors.py uses."""
    text = re.sub(r"`", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def heading_text(path: Path) -> dict[str, str]:
    """Anchor → the heading's own text, duplicates suffixed as GitHub suffixes them.

    The text matters and not only the anchor: most foundation headings *are* the rule statement
    ("a mechanism that does not bind is not a control"), which is why a matrix row whose rule cell
    is nothing but a pointer is not actually terse — the statement is at the far end of the pointer.
    The projection carries that text verbatim rather than paraphrasing it or emitting a placeholder.
    """
    out: dict[str, str] = {}
    seen: dict[str, int] = {}
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        a = anchor(m.group(2))
        n = seen.get(a, 0)
        seen[a] = n + 1
        out[a if n == 0 else f"{a}-{n}"] = m.group(2).strip()
    return out


def headings(path: Path) -> set[str]:
    """Every anchor a document defines."""
    return set(heading_text(path))


@dataclass(frozen=True)
class Rule:
    """One projected rule: the matrix row, reduced to what a review prompt needs."""

    row_id: str
    section: str  # the matrix's own `### ` group — the document that owns the rule
    rule: str  # the row's second cell, verbatim
    anchors: tuple[tuple[str, str], ...]  # (document, anchor) pairs, possibly inherited
    klass: str  # the row's last cell: M / R / U / P / D, with its parenthetical
    inherited: bool  # True when the rule cell named no anchor of its own

    @property
    def home(self) -> str:
        """The document the rule's anchor points into — the file this rule is projected under."""
        return self.anchors[0][0] if self.anchors else ""


def _cells(line: str) -> list[str] | None:
    m = _TABLE_ROW_RE.match(line)
    if not m:
        return None
    cells = [c.strip() for c in m.group(1).split("|")]
    if all(not c or set(c) <= set("-: ") for c in cells):
        return None
    return cells


def parse_matrix(text: str) -> list[Rule]:
    """Every id-bearing row of the conformance matrix, in document order.

    A row's first cell is its id, its second the rule as a pointer, its last the class. A rule cell
    that names no anchor is a continuation of the row above it — "the same: …", "rule 2: …",
    "obligation 3: …", or a bare clause — and inherits the nearest preceding anchor in its own
    ``###`` group, which is how the matrix reads and how its own coverage check resolves it.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == MATRIX_HEADING)
    except StopIteration:
        raise MissingCorpus(
            f"{SUITE} has no '{MATRIX_HEADING}' section; nothing was projected."
        ) from None
    end = next(
        (i for i, ln in enumerate(lines) if i > start and ln.startswith("## ")),
        len(lines),
    )

    rules: list[Rule] = []
    section = ""
    carried: tuple[tuple[str, str], ...] = ()
    for line in lines[start:end]:
        if line.startswith("### "):
            section = line[4:].strip()
            carried = ()  # anchors never carry across a document group
            continue
        cells = _cells(line)
        if not cells or not _ROW_ID_RE.match(cells[0]):
            continue
        found = tuple(_ANCHOR_RE.findall(cells[1]))
        inherited = not found
        if found:
            carried = found
        rules.append(
            Rule(
                row_id=cells[0],
                section=section,
                rule=cells[1],
                anchors=found or carried,
                klass=cells[-1] if len(cells) > 2 else "",
                inherited=inherited,
            )
        )
    if not rules:
        raise MissingCorpus(
            f"{SUITE}'s matrix section holds no id-bearing rows; nothing was projected."
        )
    return rules


def _slug(doc: str) -> str:
    return doc[:-3] if doc.endswith(".md") else doc


def _entry(rule: Rule, group_anchor: str = "") -> str:
    """One projected entry: the rule statement, its class, and its anchor.

    The rule cell is carried verbatim. The anchor is the entry's link back to the canonical home,
    and it is carried once: where the entry sits under a heading that already links that anchor,
    the citation the rule cell repeats is elided to the bare fragment rather than re-linked. A rule
    whose cell cites a *different* document (a cross-reference) keeps that citation as a link. The
    argument itself always stays where it is authored.
    """
    seen: list[str] = []

    def link(m: re.Match[str]) -> str:
        doc, frag = m.group(1), m.group(2)
        seen.append(f"{doc}#{frag}")
        if f"{doc}#{frag}" == group_anchor:
            return ""  # the group heading already carries it
        return f"[`{doc}#{frag}`](../{doc}#{frag})"

    body = _ANCHOR_RE.sub(link, rule.rule).strip()
    body = re.sub(r"^[:,]\s*", "", body).strip()
    klass = f" *[{rule.klass}]*" if rule.klass else ""
    if not body:
        # The rule cell was nothing but the pointer. The statement is the heading's own text,
        # which the group heading above already carries verbatim; say so rather than invent one.
        body = "the rule this heading states"
    return f"- **{rule.row_id}** — {body}{klass}\n"


_PREAMBLE = (
    "<!-- GENERATED by execution/scripts/render_reading_projection.py — do not edit. -->\n"
    "<!-- Source: docs/foundation/conformance_suite.md, the conformance matrix. -->\n"
)


def _doc_page(doc: str, rules: list[Rule], titles: dict[str, str]) -> str:
    out = [
        _PREAMBLE,
        f"# Reading projection — `{doc}`\n",
        f"Every rule `{doc}` owns, one entry each: the rule's own statement from "
        f"`conformance_suite.md`'s matrix, and a link to the heading that argues it. The argument, "
        f"the cost, the prior art, and the walkthrough are in [`{doc}`](../{doc}) and are not "
        f"repeated here (decision 66).\n",
        f"{len(rules)} rules.\n",
    ]
    by_anchor: dict[str, list[Rule]] = {}
    for r in rules:
        by_anchor.setdefault(r.anchors[0][1] if r.anchors else "", []).append(r)
    for frag, group in by_anchor.items():
        if frag:
            # The heading's own text, verbatim from the canonical document — for most foundation
            # headings that text IS the rule, which is why a row that is only a pointer still
            # projects a statement rather than a placeholder.
            # A heading whose text already carries a markdown link cannot be nested inside one;
            # those keep the citation form instead.
            title = titles.get(frag, frag)
            head = (
                f"{title} — [`#{frag}`](../{doc}#{frag})"
                if "](" in title
                else f"[{title}](../{doc}#{frag})"
            )
        else:
            head = "Rules"
        out.append(f"## {head}\n")
        key = f"{doc}#{frag}" if frag else ""
        out.append("".join(_entry(r, key) for r in group))
    return "\n".join(out)


_LIST_REGIONS = ("always read", "read when these paths changed")
_LIST_DOC_RE = re.compile(r"`docs/foundation/([A-Za-z0-9_]+\.md)`")


def _reading_list_docs(conformance: str) -> list[str]:
    """Every document basename the reading list can select, in list order."""
    seen: list[str] = []
    for name in _LIST_DOC_RE.findall(reading_list_regions(conformance)):
        if name not in seen:
            seen.append(name)
    return seen


def reading_list_regions(conformance: str) -> str:
    """The reading list's own tables, verbatim, under their own headings.

    ``foundation.py``'s ``parse_reading_list`` reads the kernel and the path-keyed rows out of
    ``conformance.md``, from the same document it also inlines. Carrying those tables into the
    projected ``conformance.md`` is what makes this directory a foundation root in its own right:
    point a review at it and the same list selects the same documents, now in projected form.

    Only the headings and the table rows are copied — the prose around them argues *why* the kernel
    is three documents and why the keying is what it is, which is argument, and argument stays at
    the canonical document under decision 66. Nothing is rewritten: every line here is a line of
    ``conformance.md``.
    """
    out: list[str] = []
    keeping = False
    for raw in conformance.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            keeping = line[3:].strip().lower().startswith(_LIST_REGIONS)
            if keeping:
                if out:
                    out.append("")
                out.append(line)
                out.append("")
            continue
        if line.startswith("# "):
            keeping = False
            continue
        if keeping and (line.startswith("### ") or line.startswith("|")):
            if line.startswith("### ") and out:
                out.append("")
            out.append(line)
            if line.startswith("### "):
                out.append("")
    return "\n".join(out).strip() + "\n"


def _lens_page(rules: list[Rule], by_doc: dict[str, list[Rule]]) -> str:
    """The lens index: class → which rules, and which file each is projected into.

    ``conformance_suite.md`` makes M / R / U / P / D a row's class — what kind of artefact can fail
    on it — and a lens reviewing for one kind wants that selection. This is an *index*, not a second
    copy: it names row ids and the file that holds each, and the rule statements stay in the
    per-document files. A page repeating all 369 statements under a second key would be the two
    copies with nothing holding them equal that `principles.md#9-one-source-defined-once-a-comment-\
claiming-parity-is-not-parity` refuses, and it would be the one projected file over MAX_DOC_CHARS.
    """
    order = ["M", "R", "U", "P", "D"]
    where = {r.row_id: name for name, group in by_doc.items() for r in group}
    buckets: dict[str, list[Rule]] = {k: [] for k in order}
    buckets["other"] = []
    for r in rules:
        buckets[next((k for k in order if r.klass.startswith(k)), "other")].append(r)
    out = [
        _PREAMBLE,
        "# Reading projection — the lens index\n",
        "Which rules a lens reviewing for one kind of failure should select, by the conformance "
        "class the matrix assigns each row "
        "(`conformance_suite.md#how-the-suite-judges-and-what-a-row-is`). This is an index of row "
        "ids and the file each is projected into — the statements themselves are in those files "
        "and are not repeated here, so there is one copy of every rule in this directory.\n",
    ]
    for key in [*order, "other"]:
        group = buckets[key]
        if not group:
            continue
        out.append(f"## Class {key} — {len(group)} rules\n")
        per_file: dict[str, list[str]] = {}
        for r in group:
            per_file.setdefault(where.get(r.row_id, "?"), []).append(r.row_id)
        for name in sorted(per_file):
            out.append(f"- [`{name}`]({name}) — {', '.join(per_file[name])}\n")
        out.append("")
    return "\n".join(out)


def _index_page(pages: dict[str, str], rules: list[Rule], by_doc: dict[str, list[Rule]]) -> str:
    total = sum(len(b) for name, b in pages.items() if name not in NOT_SELECTABLE)
    out = [
        _PREAMBLE,
        "\n# Reading projection\n\n",
        "Generated from `conformance_suite.md`'s conformance matrix by "
        "`execution/scripts/render_reading_projection.py`. One entry per rule: the rule's own "
        "statement and a link to the canonical heading that owns it. The canonical documents under "
        "`docs/foundation/` keep the full argument and are never shortened to fit a prompt "
        "(decision 66, `conformance.md#what-a-review-reads-is-a-projection-of-these-documents-not-"
        "a-shortened-copy-of-them`).\n\n",
        "**Never edit these files.** They are a render target: correct the matrix row in "
        "`conformance_suite.md` (or the rule at its anchor) and regenerate. "
        "`render_reading_projection.py --check` fails in the lint path when they drift.\n\n",
        "## How to select\n\n",
        "- **One document** — read `projection/<document>.md`. Its basename matches the canonical "
        "document, so it drops into the slot `foundation.py`'s reading list keys by path.\n"
        "- **One lens** — read `projection/lenses.md`, an index by conformance class naming which "
        "rules to pull from which file.\n"
        "- **The whole set** — read every file in this directory.\n\n",
        "## What is here\n\n",
        "| Projected document | Rules | Chars | Against `MAX_DOC_CHARS` |\n|---|---|---|---|\n",
    ]
    for name in sorted(pages):
        if name == "README.md":
            continue
        n = len(by_doc.get(name, []))
        size = len(pages[name])
        if name in NOT_SELECTABLE:
            verdict = "n/a — an index, never a selected reading"
        elif size <= MAX_DOC_CHARS:
            verdict = "under"
        else:
            verdict = f"**OVER by {size - MAX_DOC_CHARS:,}**"
        out.append(f"| `{name}` | {n if n else '—'} | {size:,} | {verdict} |\n")
    out.append(
        f"\n{len(rules)} rules projected, {total:,} chars in total across the set. "
        f"`MAX_DOC_CHARS` = {MAX_DOC_CHARS:,} bounds each file above; `MAX_BLOCK_CHARS` = "
        f"{MAX_BLOCK_CHARS:,} bounds one review's block, which is the kernel plus the documents "
        f"keyed to the changed paths — never the whole set.\n"
    )
    return "".join(out)


def build(root: Path) -> tuple[dict[str, str], list[Rule]]:
    """The projection every file would hold right now, keyed by filename under PROJECTION_DIR."""
    suite = root / FOUNDATION_DIR / SUITE
    if not suite.exists():
        raise MissingCorpus(
            f"no {suite} (looked under --root {root}); nothing was projected. "
            f"Run from the repo checkout, or pass --root pointing at one."
        )
    rules = parse_matrix(suite.read_text(encoding="utf-8"))

    # A row whose anchor resolves to nothing is the rule-coverage check's failure, and it is also a
    # projection that would point a reviewer at a heading that is not there. Fail on it here too.
    fdir = root / FOUNDATION_DIR
    cache: dict[str, dict[str, str]] = {}
    dangling: list[str] = []
    for r in rules:
        for doc, frag in r.anchors:
            path = fdir / doc
            if not path.exists():
                dangling.append(f"{r.row_id}: no such document {doc}")
                continue
            if doc not in cache:
                cache[doc] = heading_text(path)
            if frag not in cache[doc]:
                dangling.append(f"{r.row_id}: {doc} has no heading #{frag}")
    if dangling:
        raise MissingCorpus(
            "matrix rows point at anchors that do not resolve; the projection would send a "
            "reviewer nowhere:\n  " + "\n  ".join(dangling)
        )

    by_doc: dict[str, list[Rule]] = {}
    for r in rules:
        # VO-* rows carry no anchor at all: their rule cell states a class of Never item and the
        # section prose names the document. They belong to vocabulary.md by their matrix group.
        home = r.home or (r.section.strip("`") if r.section.endswith("`") else "vocabulary.md")
        by_doc.setdefault(f"{_slug(home)}.md", []).append(r)

    pages = {
        name: _doc_page(name, group, cache.get(name, {}))
        for name, group in sorted(by_doc.items())
    }
    # The reading list's own tables ride the projected conformance.md, where parse_reading_list
    # expects to find them. That is what makes this directory a foundation root: reading_block()
    # pointed at it selects the same documents and inlines their projections.
    conformance_text = (fdir / "conformance.md").read_text(encoding="utf-8")
    if "conformance.md" in pages:
        pages["conformance.md"] += "\n" + reading_list_regions(conformance_text)

    # Every document the reading list can select needs a file here, or reading_block() over this
    # directory reports it "not yet written" — a projection that silently drops a document a review
    # was supposed to read. conformance_suite.md is the one such document with no rows of its own:
    # it is the extraction source, and no matrix row points at it. It gets a page naming that,
    # rather than being absent.
    for doc in _reading_list_docs(conformance_text):
        pages.setdefault(
            doc,
            _PREAMBLE
            + f"\n# Reading projection — `{doc}`\n\n"
            + f"`{doc}` is on the reading list and owns no rule of its own in the conformance "
            + "matrix, so nothing projects into it. It is the matrix's home: every entry in this "
            + f"directory is extracted from it. Read [`{doc}`](../{doc}) directly when a change "
            + "touches the suite itself.\n",
        )
    pages["lenses.md"] = _lens_page(rules, by_doc)
    pages["README.md"] = _index_page(pages, rules, by_doc)
    return pages, rules


def render(root: Path, pages: dict[str, str]) -> int:
    out_dir = root / PROJECTION_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, body in sorted(pages.items()):
        (out_dir / name).write_text(body, encoding="utf-8")
        print(f"wrote {PROJECTION_DIR / name} ({len(body):,} chars)")
    for stale in sorted(out_dir.glob("*.md")):
        if stale.name not in pages:
            stale.unlink()
            print(f"  pruned orphan {PROJECTION_DIR / stale.name}")
    return 0


def check(root: Path, pages: dict[str, str]) -> int:
    out_dir = root / PROJECTION_DIR
    failures: list[str] = []
    for name, body in sorted(pages.items()):
        path = out_dir / name
        if not path.exists():
            failures.append(f"{PROJECTION_DIR / name}: missing (not generated)")
            continue
        if path.read_text(encoding="utf-8") != body:
            failures.append(f"{PROJECTION_DIR / name}: differs from the matrix")
    orphans = (
        [p.name for p in sorted(out_dir.glob("*.md")) if p.name not in pages]
        if out_dir.is_dir()
        else []
    )
    if failures or orphans:
        print("READING PROJECTION CHECK FAILED — disk differs from the conformance matrix:")
        for f in failures:
            print(f"  {f}")
        for o in orphans:
            print(f"  {PROJECTION_DIR / o}: stale file no rule projects into")
        print("  regenerate: python execution/scripts/render_reading_projection.py")
        return 1
    print(f"reading projection OK — {len(pages)} files match the matrix")
    return 0


def measure(pages: dict[str, str], rules: list[Rule]) -> int:
    """Report the projection against the two caps decision 66 re-aimed at it."""
    docs = {n: b for n, b in pages.items() if n not in NOT_SELECTABLE}
    total = sum(len(b) for b in docs.values())
    worst = max(docs.items(), key=lambda kv: len(kv[1]))
    print(f"reading projection: {len(rules)} rules, {len(docs)} files, {total:,} chars in total")
    for name, body in sorted(docs.items(), key=lambda kv: -len(kv[1])):
        flag = "" if len(body) <= MAX_DOC_CHARS else f"  OVER MAX_DOC_CHARS by {len(body) - MAX_DOC_CHARS:,}"
        print(f"  {name:28} {len(body):7,}{flag}")
    print(
        f"worst document: {worst[0]} at {len(worst[1]):,} against MAX_DOC_CHARS={MAX_DOC_CHARS:,} "
        f"({'under' if len(worst[1]) <= MAX_DOC_CHARS else 'OVER'})"
    )
    print(
        f"whole set: {total:,} against MAX_BLOCK_CHARS={MAX_BLOCK_CHARS:,} — note the block cap "
        f"bounds one review's kernel-plus-keyed selection, not the whole set; "
        f"execution/daemons/apis/test_foundation.py measures the real blocks"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=REPO_ROOT)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 if disk differs from the matrix")
    mode.add_argument("--measure", action="store_true", help="print sizes against the two caps")
    args = ap.parse_args(argv)

    try:
        pages, rules = build(args.root)
    except MissingCorpus as exc:
        print(f"reading projection: {exc}")
        return 1
    if args.check:
        return check(args.root, pages)
    if args.measure:
        return measure(pages, rules)
    return render(args.root, pages)


if __name__ == "__main__":
    sys.exit(main())
