"""
execution/daemons/apis/foundation.py — the design-foundation reading list.

``docs/foundation/`` holds the design documents issue-based work conforms to.
``docs/foundation/conformance.md`` is the reading list: a small always-read
kernel plus documents keyed to the paths a change touches, in the format of
Neotoma's ``docs/developer/pr_review_reading_list.md``.

This module is what makes those documents BIND rather than merely exist. It is
consumed by three existing mechanisms, none new:

  1. the arch gate and the review lenses (``swarm_dispatch._spec_section_prompt``
     and ``_panelist_prompt``) inline ``reading_block()`` — the kernel, plus the
     documents keyed to the changed files — so the reviewer reads the document
     it is asked to enforce;
  2. the dispatched-prompt path (``skill_runner.build_system_prompt``) carries
     ``foundation_contract()`` beside ``SWARM_PRIOR_ART_CONTRACT``, naming the
     kernel and the rule that a PR states its design basis;
  3. the issue spec's design-basis section is checked mechanically by
     ``check_design_basis()`` before the pm and arch gates judge it.

Everything here is keyed off the files actually on disk. A document listed in
``conformance.md`` that is not yet written is reported as such, never invented;
a ``docs/foundation/`` directory with no ``conformance.md`` fires nothing. So
the wiring is complete before the prose exists, and each document lands into a
slot that already fires — the point of building the binding first.

Pure functions; the only I/O is reading markdown under ``foundation_root()``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("apis.foundation")

FOUNDATION_DIR = "docs/foundation"
CONFORMANCE_DOC = f"{FOUNDATION_DIR}/conformance.md"

# Headings in conformance.md the parser keys on. Both are H2; the keyed table
# may be split into H3 groups beneath its heading (as Neotoma's list is).
_ALWAYS_HEADING = "always read"
_KEYED_HEADING = "read when these paths changed"

# Budget. Neotoma's reading list records that an always-read set of six
# documents consumed 6+ turns before any diff was read, so reviews ran out of
# budget. The kernel is capped at three documents by the list itself; these
# caps bound what one document, and the whole block, may cost.
MAX_DOC_CHARS = 12_000
MAX_BLOCK_CHARS = 40_000

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")

# A foundation citation: a document under docs/foundation/, optionally with a
# section anchor. This is what a design-basis statement must contain unless it
# declares that no design applies.
FOUNDATION_CITATION_RE = re.compile(
    r"docs/foundation/[A-Za-z0-9_\-]+\.md(?:#[A-Za-z0-9_\-]+)?"
)
NO_DESIGN_APPLIES = "no design applies"


def foundation_root() -> Path:
    """Repo root the foundation is read from.

    ``ATELES_FOUNDATION_ROOT`` overrides it (tests point it at a fixture tree);
    otherwise the checkout this module runs from. A dispatched agent runs the
    checkout it was started in, so this is the foundation it must conform to.
    """
    override = os.environ.get(
        "ATELES_FOUNDATION_ROOT"
    )  # config-source-ok: test/deploy override of a repo-relative path
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent.parent


# ── Reading list ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KeyedEntry:
    """One row of the path-keyed table: regexes → documents."""

    patterns: tuple[str, ...]
    docs: tuple[str, ...]


@dataclass(frozen=True)
class ReadingList:
    kernel: tuple[str, ...]
    keyed: tuple[KeyedEntry, ...]


def _table_cells(line: str) -> list[str] | None:
    m = _TABLE_ROW_RE.match(line)
    if not m:
        return None
    cells = [c.strip() for c in m.group(1).split("|")]
    # Header separator rows (`|---|---|`) and empty rows carry no data.
    if all(not c or set(c) <= set("-: ") for c in cells):
        return None
    return cells


def _doc_paths(cell: str) -> tuple[str, ...]:
    return tuple(
        tok.strip() for tok in _BACKTICK_RE.findall(cell) if tok.strip().endswith(".md")
    )


def parse_reading_list(text: str) -> ReadingList:
    """Parse ``conformance.md`` into its kernel and path-keyed entries.

    Only two regions are read: the table under ``## Always read`` (first
    backticked ``.md`` path per row is a kernel document) and the tables under
    ``## Read when these paths changed`` (first cell: backticked regexes matched
    against changed paths; second cell: backticked ``.md`` paths to read).
    Prose around the tables is ignored, so the document stays readable by
    people without the parser caring.
    """
    kernel: list[str] = []
    keyed: list[KeyedEntry] = []
    region = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            title = line[3:].strip().lower()
            if title.startswith(_ALWAYS_HEADING):
                region = "kernel"
            elif title.startswith(_KEYED_HEADING):
                region = "keyed"
            else:
                region = ""
            continue
        if not region:
            continue
        cells = _table_cells(line)
        if not cells:
            continue
        if region == "kernel":
            docs = _doc_paths(cells[0])
            if docs and docs[0] not in kernel:
                kernel.append(docs[0])
        elif region == "keyed" and len(cells) >= 2:
            patterns = tuple(p.strip() for p in _BACKTICK_RE.findall(cells[0]))
            docs = _doc_paths(cells[1])
            if patterns and docs:
                keyed.append(KeyedEntry(patterns=patterns, docs=docs))
    return ReadingList(kernel=tuple(kernel), keyed=tuple(keyed))


def _read(root: Path, rel: str) -> str | None:
    path = root / rel
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("[apis.foundation] could not read %s: %s", path, exc)
        return None


def load_reading_list(root: Path | None = None) -> ReadingList | None:
    """The reading list on this checkout, or None when there is none yet."""
    root = root or foundation_root()
    text = _read(root, CONFORMANCE_DOC)
    if text is None:
        return None
    return parse_reading_list(text)


@dataclass(frozen=True)
class Reading:
    """One document the reviewer is asked to read, and why."""

    doc: str
    reason: str  # "kernel" or the changed path that keyed it
    content: str | None  # None: listed but not yet written on this checkout


def select_readings(
    reading: ReadingList, changed_files: list[str], root: Path
) -> list[Reading]:
    """Kernel first, then keyed documents in list order, each at most once."""
    out: list[Reading] = []
    seen: set[str] = set()
    for doc in reading.kernel:
        if doc in seen:
            continue
        seen.add(doc)
        out.append(Reading(doc, "kernel", _read(root, doc)))
    for entry in reading.keyed:
        matched = next(
            (
                path
                for pattern in entry.patterns
                for path in changed_files
                if re.search(pattern, path)
            ),
            None,
        )
        if matched is None:
            continue
        for doc in entry.docs:
            if doc in seen:
                continue
            seen.add(doc)
            out.append(Reading(doc, matched, _read(root, doc)))
    return out


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return (
        text[:limit].rstrip()
        + f"\n\n[… truncated at {limit} characters; read the file for the rest]"
    )


def reading_block(
    changed_files: list[str] | None = None,
    *,
    root: Path | None = None,
    max_doc_chars: int = MAX_DOC_CHARS,
    max_block_chars: int = MAX_BLOCK_CHARS,
) -> str:
    """The markdown block a reviewing lens receives: the documents, inlined.

    Empty when the checkout has no ``conformance.md`` — nothing to bind to, so
    nothing is injected and the prompt is byte-identical to before. When the
    list exists, every document it names is either inlined (present) or named
    as not yet written (absent), so the reviewer knows which invariants are
    stated and which domains are still reviewed on standing lens criteria.
    """
    root = root or foundation_root()
    reading = load_reading_list(root)
    if reading is None:
        return ""
    readings = select_readings(reading, changed_files or [], root)

    lines: list[str] = [
        f"## Design foundation — reading list (`{CONFORMANCE_DOC}`)",
        "",
        "These are the design documents this change is reviewed against. A "
        "finding that rests on one cites the document and section by path "
        "(for example `docs/foundation/principles.md#what-fires-it`); a "
        "change that contradicts a stated invariant is a `[BLOCKING]` "
        "finding. A document listed as *not yet written* states nothing: "
        "review that domain on your standing lens criteria and do not block "
        "a change for lacking a citation to it.",
        "",
    ]
    for r in readings:
        state = "inlined below" if r.content is not None else "not yet written"
        why = (
            "kernel, always read" if r.reason == "kernel" else f"keyed by `{r.reason}`"
        )
        lines.append(f"- `{r.doc}` — {why}; {state}")
    lines.append("")

    budget = max_block_chars - sum(len(line) + 1 for line in lines)
    for r in readings:
        if r.content is None:
            continue
        body = _clip(r.content, max_doc_chars)
        section = f"### `{r.doc}`\n\n{body}\n"
        if len(section) > budget:
            lines.append(
                f"### `{r.doc}`\n\n[omitted: reading-list budget of "
                f"{max_block_chars} characters exhausted; read the file]\n"
            )
            continue
        budget -= len(section) + 1
        lines.append(section)
    return "\n".join(lines).rstrip() + "\n"


# ── Kernel status (dispatched-prompt contract) ───────────────────────────────


def _purpose_line(content: str) -> str:
    """First non-empty paragraph line under ``## Purpose``, or ''."""
    in_purpose = False
    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            in_purpose = line[3:].strip().lower().startswith("purpose")
            continue
        if in_purpose and line and not line.startswith("#"):
            return line
    return ""


def kernel_status(root: Path | None = None) -> str:
    """One line per kernel document: present (with its purpose) or not yet written.

    Empty when there is no reading list. This is the dynamic half of the
    dispatched-prompt contract: the static text names the rule; this names
    what is actually on the checkout, so the prompt changes the day a kernel
    document lands.
    """
    root = root or foundation_root()
    reading = load_reading_list(root)
    if reading is None:
        return ""
    lines = ["Kernel documents on this checkout:"]
    for doc in reading.kernel:
        content = _read(root, doc)
        if content is None:
            lines.append(f"- `{doc}` — not yet written")
            continue
        purpose = _purpose_line(content)
        lines.append(f"- `{doc}` — {purpose}" if purpose else f"- `{doc}` — present")
    return "\n".join(lines)


SWARM_FOUNDATION_CONTRACT = f"""\
## Design-basis contract — the foundation this work conforms to

`{FOUNDATION_DIR}/` holds the design documents issue-based work in this repo
conforms to. `{CONFORMANCE_DOC}` is the reading list: a kernel read on every
review, plus documents keyed to the paths a change touches. The arch gate and
the review lenses load that list at review time, so a change that contradicts
a stated invariant is blocked citing the document by path. Read the kernel
documents that exist, and the documents the list keys to the paths you touch,
before you change code.

**State the design basis.** A PR you open names, in its body, the foundation
document and section it conforms to:

    Design basis: docs/foundation/work_model.md#claim-and-lease

or states `Design basis: no design applies` with one line saying why. An issue
you file does the same. A citation is not conformance — the reviewer reads the
document — but a change with no stated basis cannot be reviewed against one,
and a basis that names a document which does not exist is a blocking finding.

**When the foundation is silent or wrong, say so; do not work around it.** A
document gains a sentence through a PR that cites the plan decision it
consolidates, and that PR is the review. Building against a principle you
believe wrong, without saying so, is how four divergent copies of one gate
set came to exist.\
"""


def foundation_contract(root: Path | None = None) -> str:
    """The contract string ``build_system_prompt`` injects, or '' when the
    checkout has no reading list to bind to.

    Static rule plus the dynamic kernel status, so the injected text names
    exactly what a dispatched agent can read on this checkout.
    """
    status = kernel_status(root)
    if not status:
        return ""
    return f"{SWARM_FOUNDATION_CONTRACT}\n\n{status}"


# ── Design basis (issue spec + PR body) ──────────────────────────────────────


@dataclass(frozen=True)
class DesignBasisCheck:
    """Mechanical pre-check of a design-basis statement.

    ``ok`` is what the pm and arch gates start from, never what they end with:
    a citation to an existing document is a claim the gate then reads the
    document to test. The check exists so that a MISSING or FALSE basis is
    visible before judgement, not so that a present one passes.
    """

    ok: bool
    citations: tuple[str, ...]
    missing: tuple[str, ...]
    summary: str


def check_design_basis(text: str | None, root: Path | None = None) -> DesignBasisCheck:
    root = root or foundation_root()
    body = (text or "").strip()
    if not body:
        return DesignBasisCheck(False, (), (), "MISSING — no design basis stated")
    citations = tuple(dict.fromkeys(FOUNDATION_CITATION_RE.findall(body)))
    if citations:
        missing = tuple(c for c in citations if _read(root, c.split("#", 1)[0]) is None)
        if missing:
            return DesignBasisCheck(
                False,
                citations,
                missing,
                "INVALID — cites a document not on this checkout: "
                + ", ".join(f"`{m}`" for m in missing),
            )
        return DesignBasisCheck(
            True,
            citations,
            (),
            "cites " + ", ".join(f"`{c}`" for c in citations) + " (present)",
        )
    if NO_DESIGN_APPLIES in body.lower():
        return DesignBasisCheck(True, (), (), "declares that no design applies")
    return DesignBasisCheck(
        False,
        (),
        (),
        "INVALID — neither a `docs/foundation/` citation nor the statement "
        f"`{NO_DESIGN_APPLIES}`",
    )


def design_basis_block(
    text: str | None, *, where: str, root: Path | None = None
) -> str:
    """Prompt text reporting the mechanical check to a pm or arch gate.

    ``where`` names what was checked ("the issue's Design basis section",
    "the PR body"). The gate's job — reading the cited document and judging
    whether the change conforms — is stated, not done, here.
    """
    check = check_design_basis(text, root)
    lines = [
        f"## Design basis — mechanical check of {where}",
        "",
        f"Result: {check.summary}.",
        "",
    ]
    if check.ok and check.citations:
        lines.append(
            "A present citation is a claim, not conformance: read the cited "
            "section and judge whether the change conforms to it. A change "
            "that contradicts the cited document is `[BLOCKING]`."
        )
    elif check.ok:
        lines.append(
            "Judge whether that is true: if a kernel or keyed document in the "
            "reading list above does govern this change, the declaration is "
            "false and the finding is `[BLOCKING] design-basis`."
        )
    else:
        lines.append(
            "A missing or invalid design basis is a `[BLOCKING] design-basis` "
            "finding: the change cannot be reviewed against a design it does "
            "not name. Say what it must cite, or that it should state "
            f"`{NO_DESIGN_APPLIES}` with a reason."
        )
    return "\n".join(lines) + "\n"
