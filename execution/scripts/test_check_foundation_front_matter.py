"""Regression tests for check_foundation_front_matter.py (decision 74).

Each case below is a distinct assertion against a named branch so a silent
drift in is_rule / rule_count / EXEMPT / main() cannot hide behind an
aggregate fixture. Run with:

    pytest execution/scripts/test_check_foundation_front_matter.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "execution" / "scripts"))

import check_foundation_front_matter as cfm  # noqa: E402


def _foundation(tmp_path: Path) -> Path:
    root = tmp_path / "docs" / "foundation"
    root.mkdir(parents=True)
    return root


def _write(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.write_text(body, encoding="utf-8")
    return path


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", name.lower())


# ---------------------------------------------------------------------------
# 1–3, 8: check() front-matter paths
# ---------------------------------------------------------------------------


def test_front_matter_chain(tmp_path: Path) -> None:
    """Case 1 — Revised-by clause in front matter → front-matter-chain."""
    root = _foundation(tmp_path)
    name = "sample.md"
    _write(
        root,
        name,
        "Title\n\nRevised by Alice 2026-01-01 for wording.\n\n## Section\n\nBody.\n",
    )
    problems = cfm.check(root)
    assert len(problems) == 1
    assert "front-matter-chain" in problems[0]
    assert f"revisions.md#{_slug(name)}" in problems[0]


def test_missing_pointer(tmp_path: Path) -> None:
    """Case 2 — no Revised clause and no revisions.md# pointer → missing-pointer."""
    root = _foundation(tmp_path)
    _write(
        root,
        "orphan.md",
        "Title\n\nSome front matter without history.\n\n## Section\n\nBody.\n",
    )
    problems = cfm.check(root)
    assert len(problems) == 1
    assert "missing-pointer" in problems[0]
    assert problems[0].startswith(f"{root / 'orphan.md'}:1:")


def test_pass_when_pointer_present(tmp_path: Path) -> None:
    """Case 3 — revisions.md# pointer and no chain → []."""
    root = _foundation(tmp_path)
    _write(
        root,
        "ok.md",
        "Title\n\nHistory: see revisions.md#okmd.\n\n## Section\n\nBody.\n",
    )
    assert cfm.check(root) == []


def test_exempt_documents_skip_front_matter_rules(tmp_path: Path) -> None:
    """Case 8 — status.md / revisions.md with Revised-by are exempt → []."""
    root = _foundation(tmp_path)
    _write(
        root,
        "status.md",
        "Status\n\nRevised by Bob 2026-02-01 for status.\n\n## Report\n\nBody.\n",
    )
    _write(
        root,
        "revisions.md",
        "Revisions\n\nRevised by Carol 2026-02-02 for table.\n\n## Table\n\nBody.\n",
    )
    assert cfm.check(root) == []


# ---------------------------------------------------------------------------
# 4–5: missing-index / index present
# ---------------------------------------------------------------------------

_THREE_RULES = """\
**Agents must write every observation to the record before acting further.**

**The workflow engine never reads an external system directly at runtime.**

**Every external signal must become a recorded observation before a step advances.**
"""


def test_missing_index(tmp_path: Path) -> None:
    """Case 4 — ≥3 is_rule leads and no INDEX_LEAD → missing-index."""
    root = _foundation(tmp_path)
    _write(
        root,
        "indexed.md",
        f"Title\n\nSee revisions.md#indexedmd.\n\n## Boundary rules\n\n{_THREE_RULES}\n",
    )
    problems = cfm.check(root)
    assert len(problems) == 1
    assert "missing-index" in problems[0]
    assert '## Boundary rules"' in problems[0] or '"## Boundary rules"' in problems[0]
    assert "states 3 rules" in problems[0]


def test_pass_when_index_present(tmp_path: Path) -> None:
    """Case 5 — same three rules but body opens with INDEX_LEAD → []."""
    root = _foundation(tmp_path)
    _write(
        root,
        "indexed.md",
        "Title\n\nSee revisions.md#indexedmd.\n\n"
        "## Boundary rules\n\n"
        f"{cfm.INDEX_LEAD}\n\n"
        "- first\n- second\n- third\n\n"
        f"{_THREE_RULES}\n",
    )
    assert cfm.check(root) == []


# ---------------------------------------------------------------------------
# 6: is_rule() exclusion / carve-out branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lead", "expected"),
    [
        ("Definition: a term in the vocabulary entry", False),  # _FIELD_LABEL_RE
        ("So the rule holds.", False),  # _CONNECTIVE_RE
        ("Why.", False),  # _LABEL_WORDS
        ("Which document wins?", False),  # interrogative, no claim verb
        ("Who must confirm this is settled.", True),  # interrogative carve-out (must)
        ("Matrix.", False),  # short / label (len < 5 and/or _LABEL_WORDS)
    ],
)
def test_is_rule_guards(lead: str, expected: bool) -> None:
    """Case 6 — each is_rule exclusion branch has its own assertion."""
    assert cfm.is_rule(lead) is expected


# ---------------------------------------------------------------------------
# 7: rule_count() subsection carve-outs
# ---------------------------------------------------------------------------


def test_rule_count_numbered_subsections_returns_zero() -> None:
    """Case 7a — ≥2 numbered ### subsections → rule_count 0."""
    body = "\nintro\n"
    subs = ["1. Foo", "2. Bar", "3. Baz"]
    assert cfm.rule_count(body, subs) == 0


def test_rule_count_short_term_subsections_returns_zero() -> None:
    """Case 7b — short-term subsections (≥60% ≤3 words) → rule_count 0."""
    body = "\nintro\n"
    subs = ["Tenant", "Ownership", "The cost", "Matrix"]
    assert cfm.rule_count(body, subs) == 0


def test_missing_index_does_not_fire_for_numbered_subsections(tmp_path: Path) -> None:
    """Case 7 — numbered ### carve-out suppresses missing-index even at INDEX_MIN."""
    root = _foundation(tmp_path)
    _write(
        root,
        "series.md",
        "Title\n\nSee revisions.md#seriesmd.\n\n"
        "## Numbered series\n\n"
        "### 1. First item\n\nBody.\n\n"
        "### 2. Second item\n\nBody.\n\n"
        "### 3. Third item\n\nBody.\n",
    )
    assert cfm.check(root) == []


def test_missing_index_does_not_fire_for_short_term_subsections(tmp_path: Path) -> None:
    """Case 7 — short-term ### carve-out suppresses missing-index."""
    root = _foundation(tmp_path)
    _write(
        root,
        "terms.md",
        "Title\n\nSee revisions.md#termsmd.\n\n"
        "## Vocabulary\n\n"
        "### Tenant\n\nBody.\n\n"
        "### Ownership\n\nBody.\n\n"
        "### The cost\n\nBody.\n\n"
        "### Matrix\n\nBody.\n",
    )
    assert cfm.check(root) == []


# ---------------------------------------------------------------------------
# 9–10: main() CLI paths
# ---------------------------------------------------------------------------


def test_main_error_path_non_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Case 9 — --root that is not a directory → exit 1 + stderr message."""
    not_a_dir = tmp_path / "not_a_dir"
    not_a_dir.write_text("file", encoding="utf-8")
    rc = cfm.main(["--root", str(not_a_dir)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "front matter check:" in captured.err
    assert "is not a directory" in captured.err


def test_main_exit_codes(tmp_path: Path) -> None:
    """Case 10 — main() returns 0 on clean tree, 1 when check() finds problems."""
    clean = _foundation(tmp_path / "clean")
    _write(clean, "ok.md", "Title\n\nSee revisions.md#okmd.\n\n## Section\n\nBody.\n")
    assert cfm.main(["--root", str(clean)]) == 0

    dirty = _foundation(tmp_path / "dirty")
    _write(dirty, "bad.md", "Title\n\nNo pointer here.\n\n## Section\n\nBody.\n")
    assert cfm.main(["--root", str(dirty)]) == 1
