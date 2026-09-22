from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_102 as decision_102  # noqa: E402


CONFORMANCE = "| 102 | declaration | pointer | dependency | **ruled** |\n"
MIGRATION = """\
**Stage 1 — the registry (operator act).** Every relationship type declares
`acyclic` or `cycles_admitted`; a missing declaration is refused.

**Stage 2 — next.**

| a schema registration (stage 1) | no | one recognized acyclicity declaration;
absence is a refused precheck |

## Verification

| 1 | each relationship type has the written `acyclic` or `cycles_admitted`
declaration; omitting or corrupting it is refused; a cycle-closing edge is
refused; a declared-cycles-admitted test type accepts its cycle |
| 2 | next |
"""
SUITE = """\
| 1 | the registry: every relationship type declares `acyclic` or
`cycles_admitted` | reason | status | every acyclicity declaration present;
a registration omitting it is refused |
| 2 | next |
"""


def write_corpus(root: Path, *, migration: str = MIGRATION, suite: str = SUITE) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    (fdir / "conformance.md").write_text(CONFORMANCE, encoding="utf-8")
    (fdir / "migration.md").write_text(migration, encoding="utf-8")
    (fdir / "conformance_suite.md").write_text(suite, encoding="utf-8")


def test_complete_carry_passes(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    assert decision_102.check(tmp_path) == []


def test_missing_stage_one_declaration_fails(tmp_path: Path) -> None:
    mutant = MIGRATION.replace("`acyclic` or `cycles_admitted`", "a mode", 1)
    write_corpus(tmp_path, migration=mutant)
    assert any("migration-registration" in p for p in decision_102.check(tmp_path))


def test_missing_migration_readback_refusal_fails(tmp_path: Path) -> None:
    mutant = MIGRATION.replace("omitting or corrupting it is refused", "it is read")
    write_corpus(tmp_path, migration=mutant)
    assert any("migration-readback" in p for p in decision_102.check(tmp_path))


def test_missing_bootstrap_readback_refusal_fails(tmp_path: Path) -> None:
    mutant = SUITE.replace("a registration omitting it is refused", "it is read")
    write_corpus(tmp_path, suite=mutant)
    assert any("bootstrap-readback" in p for p in decision_102.check(tmp_path))


def test_missing_precheck_refusal_fails(tmp_path: Path) -> None:
    mutant = MIGRATION.replace("absence is a refused precheck", "presence is checked")
    write_corpus(tmp_path, migration=mutant)
    assert any("migration-precheck" in p for p in decision_102.check(tmp_path))
