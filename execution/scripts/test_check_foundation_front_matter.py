"""Fail-then-pass coverage for the decision-74 front-matter checker.

An unverified guard is not a control (`principles.md`, invariant 1): a checker
that has never been shown to fail proves nothing when it passes, because a
checker that returns no problems unconditionally passes too. Every test here
therefore asserts a bad document is REPORTED and the corresponding good
document is CLEAN, so each of decision 74's three reports is shown to bind.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_front_matter as fm  # noqa: E402


GOOD = """\
# Work Model

**Keyed document.** What a task is and how it moves.
Amendment history: `revisions.md#work_modelmd`.

## Scope

What work is, and what it is not.
"""

# The pattern decision 74 retired: the amendment history inlined ahead of the
# first rule the document states, growing one clause per pass.
CHAIN = """\
# Work Model

**Keyed document.** What a task is and how it moves. Revised by the
simplification pass of 2026-09-05 (revision 29). Revised by the memo-gap pass
of 2026-09-06 (revision 31).

## Scope

What work is, and what it is not.
"""

# Neither a chain nor a pointer: the history has no home a reader can reach.
ORPHAN = """\
# Work Model

**Keyed document.** What a task is and how it moves.

## Scope

What work is, and what it is not.
"""


def _write(root: Path, name: str, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text, encoding="utf-8")
    return root


def _problems(root: Path) -> list[str]:
    return fm.check(root)


def test_good_document_is_clean(tmp_path: Path) -> None:
    """The pass half: a pointer, no chain, no over-long section."""
    _write(tmp_path, "work_model.md", GOOD)
    assert _problems(tmp_path) == []


def test_front_matter_chain_is_reported(tmp_path: Path) -> None:
    """Rule 1, fail half: a revision chain left in the front matter."""
    _write(tmp_path, "work_model.md", CHAIN)
    problems = _problems(tmp_path)
    assert len(problems) == 1
    assert "front-matter-chain" in problems[0]
    assert "2 revision clause(s)" in problems[0]


def test_chain_moved_out_then_passes(tmp_path: Path) -> None:
    """Rule 1, pass half: the same document, history moved to revisions.md."""
    _write(tmp_path, "work_model.md", CHAIN)
    assert _problems(tmp_path)
    _write(tmp_path, "work_model.md", GOOD)
    assert _problems(tmp_path) == []


def test_missing_pointer_is_reported(tmp_path: Path) -> None:
    """Rule 1's other half: no chain, but no pointer to the history either."""
    _write(tmp_path, "work_model.md", ORPHAN)
    problems = _problems(tmp_path)
    assert len(problems) == 1
    assert "missing-pointer" in problems[0]


def test_missing_index_is_reported(tmp_path: Path) -> None:
    """Rule 2, fail half: three full-sentence rules with no opening index."""
    body = "\n\n".join(
        f"**A {w} is refused where its grant does not name it.** Prose about it."
        for w in ("read", "write", "action")
    )
    _write(tmp_path, "work_model.md", f"{GOOD}\n## Rules\n\n{body}\n")
    problems = _problems(tmp_path)
    assert len(problems) == 1
    assert "missing-index" in problems[0]
    assert "states 3 rules" in problems[0]


def test_index_present_then_passes(tmp_path: Path) -> None:
    """Rule 2, pass half: the same section, opened by the index list."""
    body = "\n\n".join(
        f"**A {w} is refused where its grant does not name it.** Prose about it."
        for w in ("read", "write", "action")
    )
    section = f"## Rules\n\n{fm.INDEX_LEAD}\n\n{body}\n"
    _write(tmp_path, "work_model.md", f"{GOOD}\n{section}")
    assert _problems(tmp_path) == []


def test_two_rules_need_no_index(tmp_path: Path) -> None:
    """The threshold binds: INDEX_MIN is 3, so two rules are not indexed."""
    body = "\n\n".join(
        f"**A {w} is refused where its grant does not name it.** Prose about it."
        for w in ("read", "write")
    )
    _write(tmp_path, "work_model.md", f"{GOOD}\n## Rules\n\n{body}\n")
    assert _problems(tmp_path) == []


def test_labels_are_not_rules(tmp_path: Path) -> None:
    """A bold LABEL lead is not a rule; indexing labels is the noise the rule guards against."""
    body = "**Tenant.** One.\n\n**Why.** Two.\n\n**Matrix.** Three.\n"
    _write(tmp_path, "work_model.md", f"{GOOD}\n## Notes\n\n{body}")
    assert _problems(tmp_path) == []


def test_exempt_documents_are_skipped(tmp_path: Path) -> None:
    """status.md and revisions.md carry chains legitimately and are never reported."""
    for name in sorted(fm.EXEMPT):
        _write(tmp_path, name, CHAIN)
    assert _problems(tmp_path) == []
