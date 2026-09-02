"""
Tests for execution/daemons/apis/foundation.py — the design-foundation
reading list and the mechanisms that make docs/foundation/ BIND.

The load-bearing tests here are the ones that prove the binding FIRES when a
document appears: a fixture document dropped into docs/foundation/ is inlined
into the reading block, named in the dispatched-prompt contract, and citable
as a design basis — with no wiring beyond the file landing. A test that only
asserted the file was *listed* would pass while nothing read it, which is the
"registered in a script no workflow invoked" failure this whole task exists
to avoid.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import foundation  # noqa: E402
from foundation import (  # noqa: E402
    CONFORMANCE_DOC,
    KeyedEntry,
    check_design_basis,
    design_basis_block,
    foundation_contract,
    kernel_status,
    load_reading_list,
    parse_reading_list,
    reading_block,
    select_readings,
)

SENTINEL = "SENTINEL-INVARIANT: a mechanism that does not bind is not a control."
PURPOSE = "SENTINEL-PURPOSE: state the invariants the swarm holds."

FIXTURE_CONFORMANCE = """\
# Conformance

## Purpose

Fixture reading list.

## Scope

Fixture.

## Always read

| Doc | What it states |
|-----|----------------|
| `docs/foundation/principles.md` | The invariants. |
| `docs/foundation/work_model.md` | How work moves. |

## Read when these paths changed

### Dispatch

| Changed path | Read |
|---|---|
| `execution/daemons/apis/swarm_dispatch`, `review_panel` | `docs/foundation/gates_and_workflows.md` |
| `lib/daemon_runtime/task_claim` | `docs/foundation/work_model.md`, `docs/foundation/gates_and_workflows.md` |

## Design basis

Prose the parser must ignore, with a `docs/foundation/ignored.md` mention.
"""

PRINCIPLES = f"""\
# Principles

## Purpose

{PURPOSE}

## Scope

P1.

## What fires it

{SENTINEL}
"""

GATES = """\
# Gates and workflows

## Purpose

SENTINEL-GATES-PURPOSE.

## Body

SENTINEL-GATES-BODY: one gate-set constant.
"""


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    """A fixture checkout with a reading list and ONE kernel doc present."""
    fdir = tmp_path / "docs" / "foundation"
    fdir.mkdir(parents=True)
    (fdir / "conformance.md").write_text(FIXTURE_CONFORMANCE)
    (fdir / "principles.md").write_text(PRINCIPLES)
    monkeypatch.setenv("ATELES_FOUNDATION_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def bare_root(tmp_path: Path, monkeypatch) -> Path:
    """A checkout with NO docs/foundation/ at all."""
    monkeypatch.setenv("ATELES_FOUNDATION_ROOT", str(tmp_path))
    return tmp_path


# ── Parsing ──────────────────────────────────────────────────────────────────


class TestParseReadingList:
    def test_kernel_and_keyed_rows(self) -> None:
        rl = parse_reading_list(FIXTURE_CONFORMANCE)
        assert rl.kernel == (
            "docs/foundation/principles.md",
            "docs/foundation/work_model.md",
        )
        assert rl.keyed == (
            KeyedEntry(
                ("execution/daemons/apis/swarm_dispatch", "review_panel"),
                ("docs/foundation/gates_and_workflows.md",),
            ),
            KeyedEntry(
                ("lib/daemon_runtime/task_claim",),
                (
                    "docs/foundation/work_model.md",
                    "docs/foundation/gates_and_workflows.md",
                ),
            ),
        )

    def test_prose_outside_tables_is_ignored(self) -> None:
        rl = parse_reading_list(FIXTURE_CONFORMANCE)
        assert "docs/foundation/ignored.md" not in rl.kernel
        assert all("ignored" not in d for e in rl.keyed for d in e.docs)

    def test_real_conformance_doc_parses_to_a_three_doc_kernel(self) -> None:
        """The committed reading list keeps the kernel at three (the budget
        Neotoma's list learned the hard way) and keys the dispatcher to the
        gates document."""
        rl = load_reading_list(_REPO_ROOT)
        assert rl is not None
        assert len(rl.kernel) == 3
        assert rl.kernel[0] == "docs/foundation/principles.md"
        readings = select_readings(
            rl, ["execution/daemons/apis/swarm_dispatch.py"], _REPO_ROOT
        )
        assert "docs/foundation/gates_and_workflows.md" in [r.doc for r in readings]
        # conformance.md keys itself, so a PR editing the foundation reads it.
        readings = select_readings(rl, ["docs/foundation/principles.md"], _REPO_ROOT)
        assert any(r.doc == CONFORMANCE_DOC and r.content for r in readings)

    def test_no_reading_list_is_none(self, bare_root: Path) -> None:
        assert load_reading_list(bare_root) is None


# ── Selection ────────────────────────────────────────────────────────────────


class TestSelectReadings:
    def test_kernel_first_then_keyed_each_once(self, root: Path) -> None:
        rl = load_reading_list(root)
        assert rl is not None
        readings = select_readings(
            rl,
            [
                "lib/daemon_runtime/task_claim.py",
                "execution/daemons/apis/review_panel.py",
            ],
            root,
        )
        docs = [r.doc for r in readings]
        assert docs == [
            "docs/foundation/principles.md",
            "docs/foundation/work_model.md",  # kernel wins; keyed dup dropped
            "docs/foundation/gates_and_workflows.md",
        ]
        assert readings[0].reason == "kernel"
        # First keyed row (swarm_dispatch|review_panel) matched review_panel.py.
        assert readings[2].reason == "execution/daemons/apis/review_panel.py"

    def test_presence_is_per_file(self, root: Path) -> None:
        rl = load_reading_list(root)
        assert rl is not None
        readings = select_readings(rl, [], root)
        by_doc = {r.doc: r.content for r in readings}
        assert SENTINEL in (by_doc["docs/foundation/principles.md"] or "")
        assert by_doc["docs/foundation/work_model.md"] is None


# ── The binding fires when a document appears ────────────────────────────────


class TestReadingBlockFires:
    def test_present_doc_is_inlined_absent_doc_is_named(self, root: Path) -> None:
        block = reading_block(["execution/daemons/apis/swarm_dispatch.py"])
        assert SENTINEL in block  # the CONTENT, not just the path
        assert (
            "`docs/foundation/work_model.md` — kernel, always read; not yet written"
            in block
        )
        assert (
            "`docs/foundation/gates_and_workflows.md` — keyed by `execution/daemons/apis/swarm_dispatch.py`; not yet written"
            in block
        )
        assert "SENTINEL-GATES-BODY" not in block

    def test_fires_the_moment_a_keyed_doc_lands(self, root: Path) -> None:
        """Drop the file in; the next block carries its content. No wiring."""
        before = reading_block(["execution/daemons/apis/swarm_dispatch.py"])
        assert "SENTINEL-GATES-BODY" not in before
        (root / "docs" / "foundation" / "gates_and_workflows.md").write_text(GATES)
        after = reading_block(["execution/daemons/apis/swarm_dispatch.py"])
        assert "SENTINEL-GATES-BODY" in after
        assert (
            "`docs/foundation/gates_and_workflows.md` — keyed by `execution/daemons/apis/swarm_dispatch.py`; inlined below"
            in after
        )

    def test_unkeyed_change_gets_kernel_only(self, root: Path) -> None:
        (root / "docs" / "foundation" / "gates_and_workflows.md").write_text(GATES)
        block = reading_block(["README.md"])
        assert SENTINEL in block
        assert "gates_and_workflows" not in block

    def test_nothing_fires_without_a_reading_list(self, bare_root: Path) -> None:
        assert reading_block(["execution/daemons/apis/swarm_dispatch.py"]) == ""

    def test_budget_truncates_a_long_doc(self, root: Path) -> None:
        big = "# Big\n\n## Purpose\n\np\n\n" + ("x" * 5000)
        (root / "docs" / "foundation" / "work_model.md").write_text(big)
        block = reading_block([], max_doc_chars=1000)
        assert "truncated at 1000 characters" in block
        assert "x" * 1001 not in block
        assert SENTINEL in block  # the short doc is untouched

    def test_budget_omits_a_doc_past_the_block_cap(self, root: Path) -> None:
        big = "# Big\n\n## Purpose\n\np\n\n" + ("x" * 5000)
        (root / "docs" / "foundation" / "work_model.md").write_text(big)
        # Header + principles fit; the second document does not.
        block = reading_block([], max_doc_chars=1000, max_block_chars=1300)
        assert SENTINEL in block
        assert "reading-list budget of 1300 characters exhausted" in block
        assert "xxxx" not in block


# ── Dispatched-prompt contract ────────────────────────────────────────────────


class TestFoundationContract:
    def test_names_rule_and_kernel_status(self, root: Path) -> None:
        text = foundation_contract()
        assert "Design basis:" in text
        assert "no design applies" in text
        assert f"`docs/foundation/principles.md` — {PURPOSE}" in text
        assert "`docs/foundation/work_model.md` — not yet written" in text

    def test_status_changes_when_a_kernel_doc_lands(self, root: Path) -> None:
        assert "`docs/foundation/work_model.md` — not yet written" in kernel_status()
        (root / "docs" / "foundation" / "work_model.md").write_text(
            "# W\n\n## Purpose\n\nSENTINEL-WORK-PURPOSE.\n"
        )
        assert (
            "`docs/foundation/work_model.md` — SENTINEL-WORK-PURPOSE."
            in kernel_status()
        )

    def test_empty_without_a_reading_list(self, bare_root: Path) -> None:
        assert foundation_contract() == ""
        assert kernel_status() == ""


# ── Design basis ─────────────────────────────────────────────────────────────


class TestCheckDesignBasis:
    def test_missing(self, root: Path) -> None:
        c = check_design_basis("")
        assert not c.ok and c.summary.startswith("MISSING")
        assert not check_design_basis(None).ok

    def test_citation_to_present_doc_is_ok(self, root: Path) -> None:
        c = check_design_basis(
            "Design basis: docs/foundation/principles.md#what-fires-it"
        )
        assert c.ok
        assert c.citations == ("docs/foundation/principles.md#what-fires-it",)
        assert c.missing == ()

    def test_citation_to_absent_doc_is_invalid(self, root: Path) -> None:
        c = check_design_basis(
            "Design basis: docs/foundation/principles.md and "
            "docs/foundation/failure_posture.md#halt"
        )
        assert not c.ok
        assert c.missing == ("docs/foundation/failure_posture.md#halt",)
        assert "INVALID" in c.summary

    def test_citation_becomes_valid_when_the_doc_lands(self, root: Path) -> None:
        text = "Design basis: docs/foundation/work_model.md#claim-and-lease"
        assert not check_design_basis(text).ok
        (root / "docs" / "foundation" / "work_model.md").write_text("# W\n")
        assert check_design_basis(text).ok

    def test_no_design_applies_is_ok(self, root: Path) -> None:
        c = check_design_basis("Design basis: No design applies — typo fix.")
        assert c.ok and c.citations == ()

    def test_neither_is_invalid(self, root: Path) -> None:
        c = check_design_basis("This follows our usual approach.")
        assert not c.ok and "INVALID" in c.summary

    def test_block_says_what_the_gate_must_do(self, root: Path) -> None:
        ok = design_basis_block(
            "docs/foundation/principles.md#what-fires-it", where="the PR body"
        )
        assert "mechanical check of the PR body" in ok
        assert "A present citation is a claim, not conformance" in ok
        missing = design_basis_block("", where="the issue's Design basis section")
        assert "[BLOCKING] design-basis" in missing
        declared = design_basis_block("no design applies — docs typo", where="x")
        assert "the declaration is false" in declared


def test_default_root_is_the_repo_checkout(monkeypatch) -> None:
    monkeypatch.delenv("ATELES_FOUNDATION_ROOT", raising=False)
    assert foundation.foundation_root() == _REPO_ROOT
    assert (foundation.foundation_root() / CONFORMANCE_DOC).is_file()
