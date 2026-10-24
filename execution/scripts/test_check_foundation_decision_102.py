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

| DM-32 | current record-owned qualification | no-effect refusal on conflict;
fresh complete authorized preflight for recovery |
| DM-32a | complete-state preservation |

### Next section
"""
DATA_MODEL = """\
## Proposed first-flow instance cycle-policy fixture

Complete proof is required at the append and for the current effective head.
Preserve opaque user and registry metadata. Conflict HOLDS with no new registry
version, source row, event or graph effect. Until verified, both branches HOLD.

## Next section
"""


def write_corpus(
    root: Path,
    *,
    migration: str = MIGRATION,
    suite: str = SUITE,
    data_model: str = DATA_MODEL,
) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    (fdir / "conformance.md").write_text(CONFORMANCE, encoding="utf-8")
    (fdir / "migration.md").write_text(migration, encoding="utf-8")
    (fdir / "conformance_suite.md").write_text(suite, encoding="utf-8")
    (fdir / "data_model.md").write_text(data_model, encoding="utf-8")


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


def test_missing_at_write_no_effect_hold_fails(tmp_path: Path) -> None:
    mutant = DATA_MODEL.replace(
        "Conflict HOLDS with no new registry\nversion, source row, event or graph effect.",
        "Conflict may be retried.",
    )
    assert mutant != DATA_MODEL
    write_corpus(tmp_path, data_model=mutant)
    assert any("first-flow-at-write" in p for p in decision_102.check(tmp_path))


def test_missing_effect_qualification_fails(tmp_path: Path) -> None:
    mutant = SUITE.replace("current record-owned qualification", "earlier readback")
    assert mutant != SUITE
    write_corpus(tmp_path, suite=mutant)
    assert any("first-flow-effect" in p for p in decision_102.check(tmp_path))
