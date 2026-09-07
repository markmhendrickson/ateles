from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_78 as decision_78  # noqa: E402


CONFORMANCE = """\
# Conformance

## The register of open design decisions

| # | Question | Pointer | Dependencies | Status |
|---|---|---|---|---|
| 78 | whether the instance of the record serving a swarm is an external system | `adapters.md#whether-the-instance-of-the-record-serving-a-swarm-is-an-external-system-when-the-swarm-operates-it` | decision 45 | **ruled** |
"""

ADAPTERS = """\
# Adapters

### Whether the instance of the record serving a swarm is an external system when the swarm operates it

**Ruled (decision 78, 2026-09-07): the instance is an external system when the swarm operates it.**
"""


def write_corpus(
    root: Path, conformance: str = CONFORMANCE, adapters: str = ADAPTERS
) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    (fdir / "conformance.md").write_text(conformance, encoding="utf-8")
    (fdir / "adapters.md").write_text(adapters, encoding="utf-8")


def test_passes_when_register_and_adapters_section_are_ruled(tmp_path: Path) -> None:
    write_corpus(tmp_path)

    assert decision_78.check(tmp_path) == []


def test_fails_when_register_row_is_not_ruled(tmp_path: Path) -> None:
    write_corpus(tmp_path, conformance=CONFORMANCE.replace("**ruled**", "**open**"))

    problems = decision_78.check(tmp_path)

    assert len(problems) == 1
    assert "decision-78-register" in problems[0]
    assert "status cell must contain" in problems[0]


def test_fails_when_adapters_section_still_opens_as_open(tmp_path: Path) -> None:
    write_corpus(tmp_path, adapters=ADAPTERS.replace("**Ruled", "**Open"))

    problems = decision_78.check(tmp_path)

    assert len(problems) == 1
    assert "decision-78-adapters" in problems[0]
    assert 'must open with "**Ruled"' in problems[0]
