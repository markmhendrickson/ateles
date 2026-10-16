#!/usr/bin/env python3
"""Check a plan's cited blockers against the decision register and the corpus.

Phase A's exit gate asks two things of every plan that steers foundation work:
**every cited blocker resolves to a live register row**, and **no plan cites a
document that does not exist**. This checks both, plus the third failure the
register's own history supplies: a blocker whose *stated status* disagrees with
the register.

**Why the third matters most.** The predecessor plan carried decisions 73, 76,
77 and 78 in a hand-maintained ``decision_blockers`` array. A union-reducer
defect stacked three generations of entries, and several had gone false —
recording decisions as "NOT YET MERGED" that had merged. Nothing read that field
against the register, so nothing caught it. A citation that resolves but lies
about what it resolves to is worse than a dangling one: a dangling citation
announces itself, and a stale status reads as current.

**This reads; it never writes.** A mismatch is reported for the operator to
resolve on the entity. Correcting a plan field from a script would be the same
class of unreviewed write the corrupted array came from — and ``correct``
replaces a field wholesale, so a generator racing a session would drop entries
it never read.

**Input.** JSON on stdin or at ``--plan``: either one entity object or a list of
them. Each needs ``entity_id``/``id`` and any subset of the text-bearing fields;
every string field is scanned, at any depth, so a citation in a field this script
has never heard of is still read. That matters because the fields differ per
plan — ``decision_blockers``, ``todos``, ``next_steps``, ``body`` — and a
checker that named them exhaustively would go stale the first time a plan grew
one.

**What it reports.**

``unknown-decision``
    a cited decision number with no row in the register on ``origin/main``.
    Reported at one of two severities, because two very different things produce
    it. A number some open branch's register **does** carry is a citation of a
    row that exists but has not merged — a plan reading ahead of the corpus,
    which is legitimate when the plan says so and dangerous when it does not, so
    it is reported as ``pending`` with the branch named. A number no ref carries
    anywhere is ``dangling``: a citation of a row that does not exist at all.

``missing-document``
    a cited ``docs/foundation/<name>.md`` that does not exist on disk.

``status-disagreement``
    a citation stating a decision's status — "decision 78 is open", "76 NOT YET
    MERGED", "101 is ruled" — that the register contradicts. Merge claims are
    judged against the register on ``origin/main``, since that is what "merged"
    means.

**What it deliberately does not report.** A citation with no status claim
attached is not a disagreement — most are simply pointers, and demanding a status
of each would flood the output. And a *past-tense* status claim is history, which
may be correct as written; only present-tense claims are judged, the same
distinction ``check_foundation_register_narrative.py`` draws for the register's
own prose.

Stdlib only. Companion to ``render_decision_state.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")
REGISTER_DOC = "conformance.md"
MAIN_REF = "origin/main"

ROW_RE = re.compile(r"^\|\s*(\d+(?:[–-]\d+)?)\s*\|(.*)\|\s*$")
STATUS_LEAD_RE = re.compile(r"^\s*\*\*([^*]+?)\*\*")
STATUS_ORDER = (
    "ruled in part",
    "not a decision",
    "withdrawn",
    "reopened",
    "open",
    "ruled",
)
RULED_STATUSES = frozenset({"ruled", "ruled in part"})

# "decision 78", "decisions 73, 76, 77 and 78", "#101". The plural form is
# expanded so a list cited once is checked entry by entry.
DECISION_RE = re.compile(r"\bdecisions?\s+((?:\d+)(?:\s*(?:,|and|&|/)\s*\d+)*)", re.I)
NUM_RE = re.compile(r"\d+")

# A foundation document path, however it is written.
DOC_RE = re.compile(r"\bdocs/foundation/([A-Za-z0-9_.-]+\.md)\b")

# A status claim attached to a decision. Captures the number and the claim.
STATUS_CLAIM_RE = re.compile(
    r"\bdecisions?\s+(\d+)\b[^.;\n]{0,80}?"
    r"\b(is|are|remains?|stays?|still)\s+"
    r"(open|unruled|ruled|settled|merged|unmerged|withdrawn|blocked)\b",
    re.I,
)
# "decision 76 — NOT YET MERGED", "101: not merged". The corrupted array's own
# shape, which the prose form above does not catch.
NOT_MERGED_RE = re.compile(
    r"\bdecisions?\s+(\d+)\b[^.;\n]{0,60}?\bnot[\s-]+(?:yet[\s-]+)?merged\b", re.I
)

PAST_FRAMING = re.compile(
    r"\b(?:was|were|had\s+been|used\s+to\s+be|as\s+of\s+\d{4}-\d{2}-\d{2})\b", re.I
)


def run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        args, capture_output=True, text=True, errors="replace", check=False
    )
    return proc.returncode, proc.stdout


def register_rows(ref: str = MAIN_REF) -> dict[str, str]:
    """{row number: status} from the register on `ref`, falling back to disk."""
    code, out = run(["git", "show", f"{ref}:{FOUNDATION_DIR}/{REGISTER_DOC}"])
    if code != 0 or not out.strip():
        path = FOUNDATION_DIR / REGISTER_DOC
        if not path.is_file():
            return {}
        out = path.read_text(encoding="utf-8")
    rows: dict[str, str] = {}
    for line in out.split("\n"):
        m = ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) < 4:
            continue
        lead = STATUS_LEAD_RE.match(cells[-1])
        if not lead:
            continue
        status_lead = lead.group(1).strip().lower()
        rows[m.group(1)] = next(
            (k for k in STATUS_ORDER if status_lead.startswith(k)), status_lead
        )
    return rows


def rows_on_branches() -> dict[str, list[str]]:
    """{row number: branches whose register carries it} for rows absent from main.

    Lets a citation of a row that exists on a branch be reported as reading
    ahead of the corpus rather than as a dangling reference to nothing.
    """
    code, out = run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/remotes/"]
    )
    if code != 0:
        return {}
    found: dict[str, list[str]] = {}
    for line in out.split("\n"):
        ref = line.strip().removeprefix("refs/remotes/")
        if not ref or ref.endswith("/HEAD") or ref == "origin/main":
            continue
        code, text = run(["git", "show", f"{ref}:{FOUNDATION_DIR}/{REGISTER_DOC}"])
        if code != 0 or not text.strip():
            continue
        for line2 in text.split("\n"):
            m = ROW_RE.match(line2)
            if m:
                found.setdefault(m.group(1), []).append(ref)
    return found


def covered(number: str, rows: dict[str, str]) -> str | None:
    """The status of `number`, resolving the combined "1–12" row."""
    if number in rows:
        return rows[number]
    n = int(number)
    for key, status in rows.items():
        m = re.match(r"^(\d+)[–-](\d+)$", key)
        if m and int(m.group(1)) <= n <= int(m.group(2)):
            return status
    return None


def walk_strings(node: object, path: str = "") -> list[tuple[str, str]]:
    """Every string in the entity, with the field path that reaches it."""
    out: list[tuple[str, str]] = []
    if isinstance(node, str):
        out.append((path or "(root)", node))
    elif isinstance(node, dict):
        for key, value in node.items():
            out.extend(walk_strings(value, f"{path}.{key}" if path else str(key)))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out.extend(walk_strings(value, f"{path}[{i}]"))
    return out


def sentence_around(text: str, start: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    right = text.find(".", start)
    return text[left : right if right != -1 else len(text)]


def check_entity(
    entity: dict, rows: dict[str, str], on_branches: dict[str, list[str]]
) -> list[str]:
    eid = entity.get("entity_id") or entity.get("id") or "(no id)"
    title = entity.get("title") or entity.get("name") or ""
    label = f"{eid} ({title})" if title else str(eid)
    problems: list[str] = []
    seen: set[tuple] = set()

    for fieldpath, text in walk_strings(entity):
        # --- cited decision numbers ---
        for m in DECISION_RE.finditer(text):
            for num in NUM_RE.findall(m.group(1)):
                if covered(num, rows) is not None:
                    continue
                key = ("unknown-decision", num)
                if key in seen:
                    continue
                seen.add(key)
                branches = on_branches.get(num, [])
                if branches:
                    problems.append(
                        f"{label}: field {fieldpath}: unknown-decision (pending) "
                        f"— cites decision {num}, which has no row on {MAIN_REF} "
                        f"but exists on {len(branches)} branch(es), e.g. "
                        f"{branches[0]}. The plan is reading ahead of the corpus; "
                        f"this is legitimate only where the plan says so."
                    )
                else:
                    problems.append(
                        f"{label}: field {fieldpath}: unknown-decision (dangling) "
                        f"— cites decision {num}, which has no row on {MAIN_REF} "
                        f"nor on any branch scanned."
                    )

        # --- cited foundation documents ---
        for m in DOC_RE.finditer(text):
            name = m.group(1)
            if (FOUNDATION_DIR / name).is_file():
                continue
            key = ("missing-document", name)
            if key in seen:
                continue
            seen.add(key)
            problems.append(
                f"{label}: field {fieldpath}: missing-document — cites "
                f"docs/foundation/{name}, which does not exist."
            )

        # --- status claims ---
        for m in STATUS_CLAIM_RE.finditer(text):
            num, claim = m.group(1), m.group(3).lower()
            if PAST_FRAMING.search(sentence_around(text, m.start())):
                continue  # history, correct as written
            actual = covered(num, rows)
            if actual is None:
                continue  # already reported as unknown-decision
            problems.extend(
                _judge(label, fieldpath, num, claim, actual, seen)
            )

        for m in NOT_MERGED_RE.finditer(text):
            num = m.group(1)
            if PAST_FRAMING.search(sentence_around(text, m.start())):
                continue
            actual = covered(num, rows)
            if actual is None:
                continue
            problems.extend(
                _judge(label, fieldpath, num, "unmerged", actual, seen)
            )

    return problems


def _judge(
    label: str,
    fieldpath: str,
    num: str,
    claim: str,
    actual: str,
    seen: set[tuple],
) -> list[str]:
    """One status claim against the register's row."""
    is_ruled = actual in RULED_STATUSES
    wrong = False
    if claim in {"open", "unruled", "blocked"} and is_ruled:
        wrong = True
    elif claim in {"ruled", "settled", "merged"} and not is_ruled:
        wrong = True
    elif claim == "unmerged" and is_ruled:
        # The register on origin/main is the definition of merged.
        wrong = True
    elif claim == "withdrawn" and actual != "withdrawn":
        wrong = True

    if not wrong:
        return []
    key = ("status-disagreement", num, claim)
    if key in seen:
        return []
    seen.add(key)
    return [
        f"{label}: field {fieldpath}: status-disagreement — claims decision "
        f"{num} is {claim}; the register on {MAIN_REF} says {actual!r}."
    ]


def load(source: str | None) -> list[dict]:
    raw = Path(source).read_text(encoding="utf-8") if source else sys.stdin.read()
    data = json.loads(raw)
    if isinstance(data, dict):
        # Tolerate a wrapper -- {"entities": [...]} is what a retrieval returns.
        for key in ("entities", "results", "data"):
            if isinstance(data.get(key), list):
                return [e for e in data[key] if isinstance(e, dict)]
        return [data]
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    raise SystemExit("plan citations: input is neither an entity nor a list of them")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--plan",
        action="append",
        default=None,
        help="path to a JSON file holding a plan entity (repeatable); "
        "default reads stdin",
    )
    args = parser.parse_args(argv)

    rows = register_rows()
    if not rows:
        print(
            f"plan citations: no register table at {MAIN_REF}:"
            f"{FOUNDATION_DIR}/{REGISTER_DOC} nor on disk",
            file=sys.stderr,
        )
        return 1

    on_branches = rows_on_branches()

    entities: list[dict] = []
    for source in args.plan or [None]:
        entities.extend(load(source))

    problems: list[str] = []
    for entity in entities:
        problems.extend(check_entity(entity, rows, on_branches))

    for p in problems:
        print(p)
    print(
        f"plan citations: {len(problems)} problem(s) across "
        f"{len(entities)} entity/entities, against {len(rows)} register rows"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
