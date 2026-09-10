from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_101 as decision_101  # noqa: E402


CONFORMANCE = """\
# Conformance

## The register of open design decisions

| # | Question | Pointer | Dependencies | Status |
|---|---|---|---|---|
| 101 | what fields the credential-binding edge carries | `authority_model.md#what-the-credential-binding-carries` | stage 1 | **ruled** |
"""

DATA_MODEL_OK = """\
# Data model

## Relationships

| Edge type | Source → target | Meaning | What derives from it |
|---|---|---|---|
| `principal_binding` | credential (edge-keyed; no credential entity) → principal | binds one presented credential; one edge per credential; fields: `credential_kind`, `credential_value`, `credential_issuer`, `expires_at` | credential-to-principal resolution (match live edges on kind+value[+issuer] → principal endpoint) |
"""

DATA_MODEL_LEGACY = """\
# Data model

## Relationships

| Edge type | Source → target | Meaning | What derives from it |
|---|---|---|---|
| `principal_binding` | agent → principal | the principal the agent acts as | attribution; delegation chains |
"""

DATA_MODEL_NO_EXPIRY = """\
# Data model

## Relationships

| Edge type | Source → target | Meaning | What derives from it |
|---|---|---|---|
| `principal_binding` | credential → principal | one edge per credential; fields: `credential_kind`, `credential_value`, `credential_issuer` | credential-to-principal resolution (match kind+value → principal) |
"""


def write_corpus(
    root: Path,
    conformance: str = CONFORMANCE,
    data_model: str = DATA_MODEL_OK,
) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    (fdir / "conformance.md").write_text(conformance, encoding="utf-8")
    (fdir / "data_model.md").write_text(data_model, encoding="utf-8")


def test_passes_when_ruled_and_data_model_row_carries_fields(tmp_path: Path) -> None:
    write_corpus(tmp_path)

    assert decision_101.check(tmp_path) == []


def test_fails_on_legacy_principal_binding_row(tmp_path: Path) -> None:
    write_corpus(tmp_path, data_model=DATA_MODEL_LEGACY)

    problems = decision_101.check(tmp_path)

    assert problems
    assert any("principal_binding" in p for p in problems)
    assert any("legacy" in p for p in problems)


def test_fails_when_ruled_but_row_missing_expiry(tmp_path: Path) -> None:
    write_corpus(tmp_path, data_model=DATA_MODEL_NO_EXPIRY)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "expiry" in problems[0] or "expires_at" in problems[0]


def test_fails_when_register_row_101_absent(tmp_path: Path) -> None:
    conformance_without_row = "\n".join(
        line
        for line in CONFORMANCE.splitlines()
        if not line.startswith("| 101 |")
    )
    write_corpus(tmp_path, conformance=conformance_without_row)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "decision-101-register" in problems[0]
    assert 'no register row beginning "| 101 |"' in problems[0]


def test_noop_shape_when_register_not_ruled(tmp_path: Path) -> None:
    write_corpus(
        tmp_path,
        conformance=CONFORMANCE.replace("**ruled**", "**open**"),
        data_model=DATA_MODEL_LEGACY,
    )

    assert decision_101.check(tmp_path) == []


def test_raises_when_conformance_file_is_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    (tmp_path / "docs" / "foundation" / "conformance.md").unlink()

    with pytest.raises(decision_101.CorpusProblem, match="conformance.md"):
        decision_101.check(tmp_path)


def test_raises_when_data_model_file_is_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    (tmp_path / "docs" / "foundation" / "data_model.md").unlink()

    with pytest.raises(decision_101.CorpusProblem, match="data_model.md"):
        decision_101.check(tmp_path)
