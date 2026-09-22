from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_115 as decision_115  # noqa: E402


CONFORMANCE = f"""\
# Conformance

| # | Question | Pointer | Dependencies | Status |
|---|---|---|---|---|
| 115 | agent inventory | `authority_model.md#{decision_115.ANCHOR}` | — | **ruled** |
"""

AUTHORITY = (
    f"""\
# Authority

## {decision_115.HEADING}

"""
    + "\n\n".join(decision_115.REQUIRED_AUTHORITY_MARKERS)
    + "\n"
)

SUITE = (
    "# Suite\n\n"
    + "\n".join(
        f"| AU-{number} | `authority_model.md#{decision_115.ANCHOR}` | setup | act | red | M |"
        for number in range(22, 27)
    )
    + "\n"
)

VOCABULARY = """\
# Vocabulary

### agent inventory
**Definition:** a [derived read](#derived-read) over two grains that answers one mechanism-specific decision.
"""


def write_corpus(root: Path, **overrides: str) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    values = {
        "conformance.md": CONFORMANCE,
        "authority_model.md": AUTHORITY,
        "conformance_suite.md": SUITE,
        "vocabulary.md": VOCABULARY,
        **overrides,
    }
    for name, value in values.items():
        (fdir / name).write_text(value, encoding="utf-8")


def test_passes_when_all_reader_visible_parts_are_bound(tmp_path: Path) -> None:
    write_corpus(tmp_path)

    assert decision_115.check(tmp_path) == []


@pytest.mark.parametrize(
    ("filename", "old", "new", "problem_kind"),
    [
        ("conformance.md", "**ruled**", "**open**", "register"),
        (
            "authority_model.md",
            "**Two grains, not one flattened row.**",
            "",
            "authority",
        ),
        (
            "authority_model.md",
            "| interactive session |",
            "| session omitted |",
            "authority",
        ),
        ("authority_model.md", "`FOLLOWS`", "`NEXT`", "authority"),
        (
            "authority_model.md",
            "**Reconciliation is predicate-scoped and fail-closed.**",
            "",
            "authority",
        ),
        (
            "authority_model.md",
            "**Disablement and retirement are different derived outcomes.**",
            "",
            "authority",
        ),
        ("conformance_suite.md", "| AU-25 |", "| AU-X |", "suite"),
        ("vocabulary.md", "two grains", "one row", "vocabulary"),
    ],
)
def test_planted_mutants_turn_the_check_red(
    tmp_path: Path, filename: str, old: str, new: str, problem_kind: str
) -> None:
    original = {
        "conformance.md": CONFORMANCE,
        "authority_model.md": AUTHORITY,
        "conformance_suite.md": SUITE,
        "vocabulary.md": VOCABULARY,
    }[filename]
    write_corpus(tmp_path, **{filename: original.replace(old, new, 1)})

    problems = decision_115.check(tmp_path)

    assert problems
    assert any(f"decision-115-{problem_kind}" in problem for problem in problems)


def test_missing_authority_section_turns_the_check_red(tmp_path: Path) -> None:
    write_corpus(tmp_path, **{"authority_model.md": "# Authority\n"})

    problems = decision_115.check(tmp_path)

    assert any("missing section" in problem for problem in problems)


def test_missing_corpus_file_is_an_error(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    (tmp_path / "docs" / "foundation" / "vocabulary.md").unlink()

    with pytest.raises(decision_115.CorpusProblem, match="vocabulary.md"):
        decision_115.check(tmp_path)
