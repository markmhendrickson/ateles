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


_PROJECTION_ROOT = _REPO_ROOT / "docs" / "foundation" / "projection"


def _projection_as_root() -> Path:
    """A foundation root whose documents are the projections, laid out where the parser looks.

    ``docs/foundation/projection/`` holds the projected documents and, in its
    ``conformance.md``, the reading list's own tables — everything ``reading_block()``
    needs. It is not shaped like a repo root, so this copies the files into a tmp tree
    at ``docs/foundation/`` and returns the root above it. The indexes (``README.md``,
    ``lenses.md``) are left out: no reading-list row selects them.
    """
    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="foundation-projection-"))
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    for src in sorted(_PROJECTION_ROOT.glob("*.md")):
        if src.name in ("README.md", "lenses.md"):
            continue
        shutil.copy(src, fdir / src.name)
    return root


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


# ── Real-document reading-list budget (QA acceptance for foundation docs) ─────


class TestRealDocumentBudget:
    """Prove what a review actually reads fits the #744 caps without clip/omit.

    Fixture-only budget tests (``test_budget_truncates_a_long_doc``) show ``_clip``
    works. This asserts the *real* artefact a reviewer is handed stays under the
    caps, so review prompts carry the contract they are supposed to enforce.

    What that artefact is was ruled on 2026-09-06 (decision 66,
    ``docs/foundation/conformance.md#what-a-review-reads-is-a-projection-of-these-\
documents-not-a-shortened-copy-of-them``): the **reading projection**, generated
    from ``conformance_suite.md``'s matrix by ``render_reading_projection.py``, not
    the canonical documents. MAX_DOC_CHARS and MAX_BLOCK_CHARS constrain the
    projection; the canonical documents are bounded by nothing mechanical and are
    never shortened to fit a prompt.

    The generator landed on PR #745, so the fallback to the canonical documents that
    the previous xfail marker recorded is gone and these are ordinary assertions.
    The projection directory is itself a foundation root — its ``conformance.md``
    carries the reading list's own tables — so ``reading_block(root=projection)``
    is the artefact measured, and it is measured through the real entry point
    rather than by summing file sizes.
    """

    def test_the_projection_exists_and_is_generated_from_the_matrix(self) -> None:
        """The generator ran and its output is on disk, freshly.

        Measuring a projection that is stale measures the wrong artefact just as
        surely as measuring the canonical documents did, so this asserts ``--check``
        passes before the two budget assertions below read the files.
        """
        assert _PROJECTION_ROOT.is_dir(), str(_PROJECTION_ROOT)
        assert (_PROJECTION_ROOT / "conformance.md").exists()
        sys.path.insert(0, str(_REPO_ROOT / "execution" / "scripts"))
        import render_reading_projection as rrp

        pages, rules = rrp.build(_REPO_ROOT)
        assert rules, "the conformance matrix yielded no rules"
        assert rrp.check(_REPO_ROOT, pages) == 0, (
            "docs/foundation/projection/ is stale; "
            "run python execution/scripts/render_reading_projection.py"
        )

    def test_real_documents_fit_reading_block_budget(self) -> None:
        """Every projected document is <= MAX_DOC_CHARS, and every representative
        reading_block() over the projection has no 'truncated at' / '[omitted:'
        marker and is <= MAX_BLOCK_CHARS.

        Decision 66: the caps are the projection's. The canonical documents keep the
        full argument and are deliberately far over both — that is not a debt, it is
        the ruling, and nothing here measures them.
        """
        rl = load_reading_list(_REPO_ROOT)
        assert rl is not None
        docs = list(
            dict.fromkeys([*rl.kernel, *(d for e in rl.keyed for d in e.docs)])
        )
        for doc in docs:
            projected = _PROJECTION_ROOT / Path(doc).name
            assert projected.exists(), (
                f"{doc} is on the reading list with no projection; "
                "run python execution/scripts/render_reading_projection.py"
            )
            text = projected.read_text(encoding="utf-8")
            assert len(text) <= foundation.MAX_DOC_CHARS, (
                str(projected.relative_to(_REPO_ROOT)),
                len(text),
                foundation.MAX_DOC_CHARS,
            )
        # The projection directory is a foundation root in content but not in shape:
        # reading_block(root=X) looks for X/docs/foundation/. Lay the projected files out
        # that way in a tmp tree and measure through the real entry point, so what is
        # asserted is the block a reviewer would actually be handed.
        block_root = _projection_as_root()
        for paths in (
            [],
            ["lib/daemon_runtime/gating.py"],
            ["lib/daemon_runtime/workflow_resolver.py"],
            ["lib/daemon_runtime/task_claim.py"],
            ["lib/daemon_runtime/agent_loader.py"],
            ["lib/daemon_runtime/neotoma_reachability.py"],
            [".claude/skills/foo/SKILL.md"],
            ["docs/foundation/work_model.md"],
            ["docs/foundation/workflows.md"],
            ["docs/foundation/scenarios.md"],
        ):
            block = reading_block(list(paths), root=block_root)
            assert block, paths
            assert "truncated at" not in block, paths
            assert "[omitted:" not in block, paths
            assert len(block) <= foundation.MAX_BLOCK_CHARS, (
                paths,
                len(block),
                foundation.MAX_BLOCK_CHARS,
            )
    def test_status_md_is_never_selected(self) -> None:
        """docs/foundation/status.md absent from kernel+keyed; select_readings
        never returns it for [] / docs/foundation/ / skill / gating paths."""
        rl = load_reading_list(_REPO_ROOT)
        assert rl is not None
        members = list(
            dict.fromkeys([*rl.kernel, *(d for e in rl.keyed for d in e.docs)])
        )
        assert not any(m.endswith("status.md") for m in members)
        for paths in (
            [],
            ["docs/foundation/x.md"],
            [".claude/skills/foo/SKILL.md"],
            ["lib/daemon_runtime/gating.py"],
        ):
            selected = [r.doc for r in select_readings(rl, paths, _REPO_ROOT)]
            assert not any(d.endswith("status.md") for d in selected), paths

    def test_real_keyed_rows_match_conformance_contracts(self) -> None:
        """After dual-key fix: claim/lifecycle/watchdog/gating paths do NOT
        select scenarios.md; docs/foundation/ selects conformance (and only
        what conformance tables still name); skill path selects vocabulary
        (or its ≤12k split)."""
        rl = load_reading_list(_REPO_ROOT)
        assert rl is not None
        # scenarios / workflows are authored companions — never selected on runtime or foundation paths
        for paths in (
            ["lib/daemon_runtime/task_claim.py"],
            ["lib/daemon_runtime/task_lifecycle.py"],
            ["execution/daemons/apis/task_watchdog.py"],
            ["lib/daemon_runtime/workflow_resolver.py"],
            ["lib/daemon_runtime/gating.py"],
            ["docs/foundation/work_model.md"],
            ["docs/foundation/scenarios.md"],
            ["docs/foundation/workflows.md"],
        ):
            selected = [r.doc for r in select_readings(rl, paths, _REPO_ROOT)]
            assert "docs/foundation/scenarios.md" not in selected, paths
            assert "docs/foundation/workflows.md" not in selected, paths
        foundation_docs = [
            r.doc
            for r in select_readings(
                rl, ["docs/foundation/work_model.md"], _REPO_ROOT
            )
        ]
        assert CONFORMANCE_DOC in foundation_docs
        skill_docs = [
            r.doc
            for r in select_readings(
                rl, [".claude/skills/foo/SKILL.md"], _REPO_ROOT
            )
        ]
        assert "docs/foundation/vocabulary.md" in skill_docs
        members = list(
            dict.fromkeys([*rl.kernel, *(d for e in rl.keyed for d in e.docs)])
        )
        assert "docs/foundation/scenarios.md" not in members
        assert "docs/foundation/workflows.md" not in members


# ── Vocabulary lint (docs/foundation/vocabulary.md Never / Not for) ──────────


class TestVocabularyLint:
    """The banned words in vocabulary.md are a control only if something fails on them
    (principles.md invariant 1): this test runs the lint against the real documents and
    fails on any Never hit."""

    def _lint(self):
        import importlib.util

        script = _REPO_ROOT / "execution" / "scripts" / "check_foundation_vocabulary.py"
        spec = importlib.util.spec_from_file_location("check_foundation_vocabulary", script)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod  # dataclasses resolve annotations through sys.modules
        spec.loader.exec_module(mod)
        return mod

    def test_zero_never_hits_in_foundation_prose(self) -> None:
        mod = self._lint()
        vocab = (_REPO_ROOT / "docs" / "foundation" / "vocabulary.md").read_text(encoding="utf-8")
        never, not_for = mod.parse_bans(vocab)
        assert never, "vocabulary.md declares no Never items; the lint would pass vacuously"
        never_hits, _advisory = mod.scan(_REPO_ROOT, never, not_for)
        assert never_hits == [], [
            f"{h.file}:{h.line_no}: {h.ban.term}" for h in never_hits
        ]

    def test_every_pattern_key_still_names_an_entry(self) -> None:
        """A renamed term must fail here rather than silently un-lint itself.

        The regex bans live in the checker's PATTERNS table, keyed by the ``### heading`` whose prose
        states them. Rename or delete that heading and the key goes stale: the vocabulary would still
        read as if the ban existed while nothing enforced it.
        """
        mod = self._lint()
        vocab = (_REPO_ROOT / "docs" / "foundation" / "vocabulary.md").read_text(encoding="utf-8")
        assert mod.missing_pattern_entries(vocab) == []

    # ── Record/external-system paraphrase gap (PR #745 term audit, second miss) ──────────────────
    #
    # The first cut of PATTERNS["record"] required the literal phrase "external system" and was scoped
    # to six adapter documents by filename. Both restrictions let real violations through: four
    # paraphrases found by hand ("the rail's record of...", "a record of a system the swarm does not
    # own", "the merchant's record of...", "an artifact is a record living in an external system" /
    # "an external record") passed silently on every run. The fix is structural, not another literal
    # phrase: `record` possessed by, or governed by a preposition pointing at, a foreign-system noun.
    # These tests plant the real (pre-fix) sentences — not synthetic ones — so a future edit that
    # narrows the pattern back to a literal phrase fails here first.

    def _record_vocab(self) -> str:
        return (
            "# V\n\n### record\n**Definition:** Neotoma.\n**Never:** —\n"
            '**Not for:** the record for an external system; "database" for the record.\n'
        )

    def _assert_record_paraphrase_is_caught(self, tmp_path: Path, sentence: str) -> None:
        mod = self._lint()
        fdir = tmp_path / "docs" / "foundation"
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / "vocabulary.md").write_text(self._record_vocab())
        (fdir / "adapters.md").write_text(f"# A\n\n{sentence}\n")
        vocab_text = (fdir / "vocabulary.md").read_text()
        never, not_for = mod.parse_bans(vocab_text)
        never_hits, _advisory = mod.scan(tmp_path, never, not_for)
        assert never_hits, f"expected a Never hit on: {sentence!r}"
        assert all(h.file.endswith("adapters.md") for h in never_hits)

    def test_catches_the_rails_record_paraphrase(self, tmp_path: Path) -> None:
        """payments.md:72 (pre-fix): "the rail's record of the instructed transfer"."""
        self._assert_record_paraphrase_is_caught(
            tmp_path,
            "The artifact is the rail's record of the instructed transfer, assigned when accepted.",
        )

    def test_catches_a_system_the_swarm_does_not_own_paraphrase(self, tmp_path: Path) -> None:
        """adapters.md:706 (pre-fix): "each is a record of a system the swarm does not own"."""
        self._assert_record_paraphrase_is_caught(
            tmp_path,
            "The transcript a local model leaves beside its source: each is a record of a system "
            "the swarm does not own, arriving on a surface the adapter polls.",
        )

    def test_catches_the_merchants_record_paraphrase(self, tmp_path: Path) -> None:
        """adapters.md:728 (pre-fix): "the effect is the merchant's record of an order"."""
        self._assert_record_paraphrase_is_caught(
            tmp_path,
            "The swarm does not own it, the effect is the merchant's record of "
            "an order, and the merchant's own confirmation is what reads it back.",
        )

    def test_catches_an_external_record_and_living_in_an_external_system(self, tmp_path: Path) -> None:
        """work_model.md:700,705 (pre-fix): "an external record" / "a record living in an external system"."""
        self._assert_record_paraphrase_is_caught(
            tmp_path,
            "An `artifact` is an external record (issue, PR, release, message) linked by edge.",
        )
        self._assert_record_paraphrase_is_caught(
            tmp_path,
            "An artifact is a record living in an external system, reachable only through that "
            "system's adapter.",
        )

    def test_does_not_catch_the_permitted_ordinary_english_sense(self, tmp_path: Path) -> None:
        """The record entry's own Not-for permits "the record a step owner writes" — must stay silent."""
        mod = self._lint()
        fdir = tmp_path / "docs" / "foundation"
        fdir.mkdir(parents=True)
        (fdir / "vocabulary.md").write_text(self._record_vocab())
        (fdir / "adapters.md").write_text(
            "# A\n\nThe record a step owner writes is the record an effect leaves. "
            "An entry an external system holds is the artifact, never the record.\n"
        )
        never, not_for = mod.parse_bans((fdir / "vocabulary.md").read_text())
        never_hits, _advisory = mod.scan(tmp_path, never, not_for)
        assert never_hits == []

    def test_pattern_bans_reach_the_ban_lists(self) -> None:
        """Every PATTERNS item is compiled into the class it is keyed under."""
        mod = self._lint()
        vocab = (_REPO_ROOT / "docs" / "foundation" / "vocabulary.md").read_text(encoding="utf-8")
        never, not_for = mod.parse_bans(vocab)
        by_kind = {"never": {b.pattern.pattern for b in never}, "not_for": {b.pattern.pattern for b in not_for}}
        for entry, kinds in mod.PATTERNS.items():
            for kind, items in kinds.items():
                for item in items:
                    source = item[0]
                    assert source in by_kind[kind], f"{entry} / {kind}: /{source}/ never reached the ban list"

    def test_the_vocabulary_carries_no_regex_syntax(self) -> None:
        """The vocabulary is prose a person reads; the matching machinery lives in the checker.

        Regression guard for PR #745 revision 11: five Never lines rendered raw regex on GitHub.
        """
        vocab = (_REPO_ROOT / "docs" / "foundation" / "vocabulary.md").read_text(encoding="utf-8")
        offenders = [
            f"{no}: {line.strip()[:100]}"
            for no, line in enumerate(vocab.splitlines(), 1)
            if any(tok in line for tok in ("\\b", "\\w", "\\s", "(?:", "(?!", "(?<"))
        ]
        assert offenders == [], offenders

    # ── Undefined-word candidates (the checker gap: a word with no entry at all) ──────────────────

    def test_undefined_word_candidates_stays_short_enough_to_read(self) -> None:
        """The advisory list is a prompt to a human, not a rule with a failing artefact.

        It must be short enough that a person actually reads it — tens of candidates, not hundreds.
        `role`, `domain`, and `scope`'s qualified compounds were added from a run of this report; the
        bound here is generous so the report can still surface the next one without becoming noise.
        """
        mod = self._lint()
        candidates = mod.undefined_word_candidates(_REPO_ROOT)
        assert len(candidates) < 60, [w for w, _ in candidates]

    def test_undefined_word_candidates_excludes_newly_defined_terms(self) -> None:
        """role, domain, and its compounds must not re-appear now that they have entries.

        Regression guard: this is exactly the gap this revision closed, so the words that motivated the
        checker addition are the first thing a future edit could silently break.
        """
        mod = self._lint()
        candidates = {w for w, _ in mod.undefined_word_candidates(_REPO_ROOT)}
        assert "role" not in candidates
        assert "domain" not in candidates

    def test_undefined_word_candidates_excludes_ordinary_english(self) -> None:
        """A high-frequency function word never appears, however often it is used."""
        mod = self._lint()
        candidates = {w for w, _ in mod.undefined_word_candidates(_REPO_ROOT)}
        for word in ("that", "with", "never", "which", "before", "written", "already"):
            assert word not in candidates, word

    def test_undefined_word_candidates_never_affects_the_exit_code(self, capsys) -> None:
        """Advisory, not a Never: a nonzero candidate list must not fail the check."""
        mod = self._lint()
        rc = mod.main([])
        capsys.readouterr()
        assert rc == 0

    def test_undefined_word_candidates_reads_from_the_real_corpus(self) -> None:
        """A sanity check that the scan is wired to real files and not returning nothing by accident."""
        mod = self._lint()
        candidates = mod.undefined_word_candidates(_REPO_ROOT)
        assert candidates, "expected at least one candidate against the real foundation corpus"

    def test_undefined_word_candidates_missing_corpus_returns_empty(self, tmp_path: Path) -> None:
        """No corpus is not a crash: the function degrades to an empty list, same posture as `scan`."""
        mod = self._lint()
        assert mod.undefined_word_candidates(tmp_path) == []

    def test_no_undefined_words_flag_suppresses_the_report(self, capsys) -> None:
        mod = self._lint()
        rc = mod.main(["--no-undefined-words"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "undefined-word candidates" not in out

    def test_every_first_mention_is_linked(self) -> None:
        """A defined term mentioned in another entry links to its definition.

        The vocabulary states the rule under "Scope": first mention per entry, never inside its own
        entry, never inside a ban list, and the ordinary-English terms in the linker's GENERIC set left
        to the author. This asserts the document satisfies it, so the 328-mention gap that revision 10
        opened cannot reopen unnoticed.
        """
        import importlib.util

        script = _REPO_ROOT / "execution" / "scripts" / "link_vocabulary_terms.py"
        spec = importlib.util.spec_from_file_location("link_vocabulary_terms", script)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        vocab = (_REPO_ROOT / "docs" / "foundation" / "vocabulary.md").read_text(encoding="utf-8")
        _out, unlinked = mod.link(vocab)
        assert unlinked == [], [f"{block}: {term}" for block, term in unlinked][:40]

    def test_linking_a_term_does_not_change_the_lint_verdict(self) -> None:
        """``[step owner](#step-owner)`` must read as "step owner" to the ban patterns.

        Several bans use a negative lookbehind to permit a qualified use (owner is allowed after "step ",
        forbidden alone). Link markup inserts ``](#`` in front of the second word and defeats it, which
        would report the exact phrase the vocabulary permits. ``as_read`` strips the markup first.
        """
        mod = self._lint()
        plain = "the record a step owner writes"
        linked = "the record a [step owner](#step-owner) writes"
        assert mod.as_read(linked) == plain
        never, not_for = mod.parse_bans(
            (_REPO_ROOT / "docs" / "foundation" / "vocabulary.md").read_text(encoding="utf-8")
        )
        for ban in never + not_for:
            assert bool(ban.pattern.search(mod.as_read(linked))) == bool(ban.pattern.search(plain)), ban.term

    def test_bold_emphasis_does_not_change_the_lint_verdict(self, tmp_path: Path) -> None:
        """``**external**`` must read as "external" to a foreign-noun pattern spanning it.

        Found live at `data_model.md`: "the record of a thing in an **external** system" escaped the
        record/foreign-noun ban because the asterisks broke the ``\\w+`` gap between "an" and "external",
        and again between "external" and "system". A person reads "external", not "**external**"; a ban's
        verdict on a sentence must not change because the author emphasized one word in it.
        """
        mod = self._lint()
        plain = "the record of a thing in an external system, identified by"
        bold = "the record of a thing in an **external** system, identified by"
        assert mod.as_read(bold) == plain
        fdir = tmp_path / "docs" / "foundation"
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / "vocabulary.md").write_text(self._record_vocab())
        (fdir / "adapters.md").write_text(f"# A\n\n{bold}\n")
        never, not_for = mod.parse_bans((fdir / "vocabulary.md").read_text())
        never_hits, _advisory = mod.scan(tmp_path, never, not_for)
        assert never_hits, "expected the bold-emphasized foreign-noun sentence to still be caught"

    def test_lint_fails_on_a_planted_never_word(self, tmp_path: Path) -> None:
        """Revert-the-fix check: a Never word in a foundation doc is reported as a hit."""
        mod = self._lint()
        fdir = tmp_path / "docs" / "foundation"
        fdir.mkdir(parents=True)
        (fdir / "vocabulary.md").write_text(
            "# V\n\n### task\n**Definition:** x.\n**Never:** \"work item\", /\\bdispatch\\w*/.\n"
            "**Not for:** \"ticket\" for a task.\n"
        )
        (fdir / "work_model.md").write_text(
            "# W\n\nA work item is dispatched here. A ticket too.\nThe reaper is retired.\n"
        )
        (fdir / "status.md").write_text("# S\n\nwork item work item dispatch\n")
        never, not_for = mod.parse_bans((fdir / "vocabulary.md").read_text())
        never_hits, advisory = mod.scan(tmp_path, never, not_for)
        assert sorted(h.ban.term for h in never_hits) == ["/\\bdispatch\\w*/", "work item"]
        assert all(h.file.endswith("work_model.md") for h in never_hits)  # status.md skipped
        assert [h.ban.term for h in advisory] == ["ticket"]
        assert mod.main(["--root", str(tmp_path), "--quiet-advisory"]) == 1


class TestGitHubKeyedReadings:
    """github.md is keyed to the GitHub gateway, harness, and Vanellus paths.

    conformance.md's keyed table names those paths; a keyed row is a control only if
    ``select_readings()`` actually returns the document for a change touching them. A row that
    matched nothing would read as coverage while no review prompt ever carried the document.
    """

    GATEWAY_PATHS = [
        "execution/daemons/apis/github_gateway.py",
        "execution/daemons/apis/swarm_dispatch.py",
        "execution/daemons/apis/review_panel.py",
    ]
    HARNESS_PATHS = ["execution/mcp/github_harness/index.js"]
    VANELLUS_PATHS = [".claude/skills/vanellus/SKILL.md", "docs/agents/vanellus.md"]
    LANIUS_PATHS = [
        "execution/scripts/lanius_sweep.py",
        ".claude/skills/lanius/SKILL.md",
        "docs/agents/lanius.md",
    ]

    def test_github_md_is_a_reading_list_member(self) -> None:
        """The document is keyed at all — not merely present on disk."""
        rl = load_reading_list(_REPO_ROOT)
        assert rl is not None
        members = list(
            dict.fromkeys([*rl.kernel, *(d for e in rl.keyed for d in e.docs)])
        )
        assert "docs/foundation/github.md" in members

    @pytest.mark.parametrize(
        "path",
        GATEWAY_PATHS + HARNESS_PATHS + VANELLUS_PATHS + LANIUS_PATHS,
    )
    def test_each_keyed_path_selects_github_md(self, path: str) -> None:
        """Every path conformance.md keys to github.md selects it."""
        rl = load_reading_list(_REPO_ROOT)
        assert rl is not None
        selected = [r.doc for r in select_readings(rl, [path], _REPO_ROOT)]
        assert "docs/foundation/github.md" in selected, path

    def test_github_md_is_not_selected_for_unrelated_paths(self) -> None:
        """The key is scoped: a change nowhere near the code host does not carry it.

        Without this, a row matching everything would satisfy the positive tests above while
        making the keying meaningless.
        """
        rl = load_reading_list(_REPO_ROOT)
        assert rl is not None
        for path in (
            "lib/daemon_runtime/task_claim.py",
            "lib/daemon_runtime/gating.py",
            "execution/daemons/apis/task_watchdog.py",
        ):
            selected = [r.doc for r in select_readings(rl, [path], _REPO_ROOT)]
            assert "docs/foundation/github.md" not in selected, path

    def test_editing_github_md_selects_itself(self) -> None:
        """Changing the document carries the document, so its own edits are reviewed against it."""
        rl = load_reading_list(_REPO_ROOT)
        assert rl is not None
        selected = [
            r.doc for r in select_readings(rl, ["docs/foundation/github.md"], _REPO_ROOT)
        ]
        assert "docs/foundation/github.md" in selected


class TestAnchorCheck:
    """check_foundation_anchors.py is registered in conformance.md#mechanical-checks-on-this-directory.

    A check registered in a document but run by no test is exactly the "reports without binding"
    defect the foundation names: it would rot silently. This runs it against the real documents and
    proves it fails on a planted break and on a missing corpus.
    """

    def _anchors(self):
        import importlib.util

        script = _REPO_ROOT / "execution" / "scripts" / "check_foundation_anchors.py"
        spec = importlib.util.spec_from_file_location("check_foundation_anchors", script)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_real_documents_have_no_broken_anchors(self) -> None:
        mod = self._anchors()
        broken = mod.check(_REPO_ROOT)
        assert broken == [], broken
        assert mod.main(["--root", str(_REPO_ROOT)]) == 0

    def test_a_planted_broken_anchor_is_reported(self, tmp_path: Path) -> None:
        """Revert-the-fix check: a link to a heading that does not exist fails the check."""
        mod = self._anchors()
        fdir = tmp_path / "docs" / "foundation"
        fdir.mkdir(parents=True)
        (fdir / "a.md").write_text(
            "# A\n\n## Real Section\n\nSee [ok](b.md#present) and [bad](b.md#absent).\n"
        )
        (fdir / "b.md").write_text("# B\n\n## Present\n\nText.\n")
        broken = mod.check(tmp_path)
        assert len(broken) == 1, broken
        assert "missing anchor #absent" in broken[0]
        assert mod.main(["--root", str(tmp_path)]) == 1

    def test_a_planted_missing_file_is_reported(self, tmp_path: Path) -> None:
        mod = self._anchors()
        fdir = tmp_path / "docs" / "foundation"
        fdir.mkdir(parents=True)
        (fdir / "a.md").write_text("# A\n\nSee [gone](nosuch.md#x).\n")
        broken = mod.check(tmp_path)
        assert len(broken) == 1, broken
        assert "missing file nosuch.md" in broken[0]

    def test_missing_corpus_fails_closed(self, tmp_path: Path) -> None:
        """An absent docs/foundation/ must not report a pass for a check that never ran."""
        mod = self._anchors()
        with pytest.raises(mod.MissingCorpus) as exc:
            mod.check(tmp_path)
        assert str(tmp_path) in str(exc.value)  # the hint names the root it inspected
        assert mod.main(["--root", str(tmp_path)]) == 1

    def test_empty_corpus_fails_closed(self, tmp_path: Path) -> None:
        """A directory with no .md files is a missing corpus too, not a clean pass."""
        mod = self._anchors()
        (tmp_path / "docs" / "foundation").mkdir(parents=True)
        with pytest.raises(mod.MissingCorpus):
            mod.check(tmp_path)
        assert mod.main(["--root", str(tmp_path)]) == 1


class TestVocabularyCheckEmptyState:
    """The vocabulary check fails closed on a missing corpus, same defect class as the anchors check."""

    def _lint(self):
        import importlib.util

        script = _REPO_ROOT / "execution" / "scripts" / "check_foundation_vocabulary.py"
        spec = importlib.util.spec_from_file_location("check_foundation_vocabulary", script)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_missing_foundation_dir_fails_closed(self, tmp_path: Path, capsys) -> None:
        mod = self._lint()
        assert mod.main(["--root", str(tmp_path), "--quiet-advisory"]) == 1
        assert str(tmp_path) in capsys.readouterr().out  # names the root it inspected

    def test_missing_vocabulary_file_fails_closed(self, tmp_path: Path, capsys) -> None:
        mod = self._lint()
        (tmp_path / "docs" / "foundation").mkdir(parents=True)
        assert mod.main(["--root", str(tmp_path), "--quiet-advisory"]) == 1
        assert "vocabulary.md" in capsys.readouterr().out


class TestVocabularyCheckStaleKeyState:
    """A stale ``PATTERNS`` key fails the vocabulary command; it is not a warning.

    ``missing_pattern_entries`` finds a key whose ``###`` entry is gone. Before revision 30 ``main``
    printed each one and then exited 0, so a renamed term silently dropped its regex bans while the
    command reported a pass — the reports-without-binding defect the foundation names, and a claim the
    helper's docstring made that the exit path contradicted. The direct assertion in
    ``TestVocabularyLint`` covers the healthy document; this covers the command's failure state.
    """

    def _lint(self):
        import importlib.util

        script = _REPO_ROOT / "execution" / "scripts" / "check_foundation_vocabulary.py"
        spec = importlib.util.spec_from_file_location("check_foundation_vocabulary", script)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    @staticmethod
    def _clean_corpus(tmp_path: Path) -> None:
        fdir = tmp_path / "docs" / "foundation"
        fdir.mkdir(parents=True)
        (fdir / "vocabulary.md").write_text(
            '# V\n\n### task\n**Definition:** x.\n**Never:** "work item".\n'
        )
        (fdir / "work_model.md").write_text("# W\n\nClean prose about a task.\n")

    def test_a_clean_corpus_with_current_keys_passes(self, tmp_path: Path, monkeypatch) -> None:
        """Control for the planted case: the same corpus passes when every key names an entry."""
        mod = self._lint()
        self._clean_corpus(tmp_path)
        monkeypatch.setattr(mod, "PATTERNS", {"task": {"never": [(r"\bchip\b", "")]}})
        assert mod.main(["--root", str(tmp_path), "--quiet-advisory"]) == 0

    def test_a_planted_stale_key_fails_the_command(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Revert-the-fix check: one key naming no ``###`` entry makes the command exit 1."""
        mod = self._lint()
        self._clean_corpus(tmp_path)
        monkeypatch.setattr(
            mod,
            "PATTERNS",
            {
                "task": {"never": [(r"\bchip\b", "")]},
                "passage": {"never": [(r"\bpassages?\b", "")]},  # the entry was renamed away
            },
        )
        vocab = (tmp_path / "docs" / "foundation" / "vocabulary.md").read_text()
        assert mod.missing_pattern_entries(vocab) == ["passage"]
        assert mod.main(["--root", str(tmp_path), "--quiet-advisory"]) == 1
        out = capsys.readouterr().out
        assert "PATTERNS key 'passage'" in out
        assert "0 Never hit(s)" in out  # the failure is the stale key, not a prose hit


class TestTermLinkCheckEmptyState:
    """``link_vocabulary_terms.py`` fails closed on a missing vocabulary, in both modes.

    Same defect class ``TestAnchorCheck`` and ``TestVocabularyCheckEmptyState`` cover for the other two
    registered checks: before revision 30 a missing ``vocabulary.md`` printed "nothing to link" and
    exited 0, so a wrong ``--root`` or a partial checkout passed the Term-links control without running it.
    """

    def _linker(self):
        import importlib.util

        script = _REPO_ROOT / "execution" / "scripts" / "link_vocabulary_terms.py"
        spec = importlib.util.spec_from_file_location("link_vocabulary_terms", script)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_the_real_vocabulary_passes_check(self) -> None:
        """Control: the committed document satisfies ``--check`` through the command path."""
        assert self._linker().main(["--root", str(_REPO_ROOT), "--check"]) == 0

    def test_missing_foundation_dir_fails_closed(self, tmp_path: Path, capsys) -> None:
        mod = self._linker()
        assert mod.main(["--root", str(tmp_path), "--check"]) == 1
        assert str(tmp_path) in capsys.readouterr().out  # names the root it inspected

    def test_missing_vocabulary_file_fails_closed_in_write_mode_too(self, tmp_path: Path, capsys) -> None:
        """Without ``--check`` the script writes; on a missing file it must write nothing and still fail."""
        mod = self._linker()
        fdir = tmp_path / "docs" / "foundation"
        fdir.mkdir(parents=True)
        assert mod.main(["--root", str(tmp_path)]) == 1
        assert "vocabulary.md" in capsys.readouterr().out
        assert list(fdir.iterdir()) == []


class TestAdapterKeyedReadings:
    """Each per-system adapter document, and ``adapters.md``, is keyed to the paths conformance.md names.

    The contract ``TestGitHubKeyedReadings`` states, applied to the other five adapter rows: a keyed row
    binds only if ``select_readings()`` returns the document for a change on its paths, withholds it for
    a change elsewhere, and returns it for an edit to the document itself.
    """

    # doc -> (paths conformance.md keys to it, paths that must not select it)
    CASES: dict[str, tuple[list[str], list[str]]] = {
        "docs/foundation/adapters.md": (
            [
                "execution/daemons/apus/apus.py",
                "execution/daemons/formica/formica.py",
                "execution/daemons/monedula/monedula.py",
                "lib/notify/telegram.py",
                "execution/lib/telegram.py",
            ],
            ["lib/daemon_runtime/task_claim.py", "lib/daemon_runtime/gating.py"],
        ),
        "docs/foundation/gmail.md": (
            [
                "execution/daemons/turdus/turdus.py",
                "execution/daemons/riparia/riparia.py",
                "lib/daemon_runtime/run_email.py",
                "lib/approval/email_channel.py",
                ".claude/hooks/gmail_send_gate.py",
            ],
            ["execution/daemons/sylvia/sylvia.py", "lib/daemon_runtime/task_claim.py"],
        ),
        "docs/foundation/calendar.md": (
            [
                "execution/daemons/sylvia/sylvia.py",
                "execution/daemons/cotinga/cotinga.py",
                "execution/daemons/monedula/monedula.py",
            ],
            ["execution/daemons/turdus/turdus.py", "lib/daemon_runtime/task_claim.py"],
        ),
        "docs/foundation/telegram.md": (
            [
                "execution/lib/telegram.py",
                "lib/notify/telegram.py",
                "execution/daemons/cyphorhinus/cyphorhinus.py",
                "lib/activity/feed.py",
            ],
            ["execution/daemons/turdus/turdus.py", "lib/daemon_runtime/task_claim.py"],
        ),
        "docs/foundation/payments.md": (
            ["execution/daemons/monedula/monedula.py"],
            ["execution/daemons/turdus/turdus.py", "lib/daemon_runtime/task_claim.py"],
        ),
    }

    @staticmethod
    def _selected(paths: list[str]) -> list[str]:
        rl = load_reading_list(_REPO_ROOT)
        assert rl is not None
        return [r.doc for r in select_readings(rl, paths, _REPO_ROOT)]

    @pytest.mark.parametrize("doc", sorted(CASES))
    def test_each_adapter_doc_is_a_reading_list_member(self, doc: str) -> None:
        rl = load_reading_list(_REPO_ROOT)
        assert rl is not None
        assert doc in {d for e in rl.keyed for d in e.docs}

    @pytest.mark.parametrize(
        "doc,path",
        [(doc, path) for doc, (positives, _negatives) in sorted(CASES.items()) for path in positives],
    )
    def test_each_keyed_path_selects_its_document(self, doc: str, path: str) -> None:
        assert doc in self._selected([path]), (doc, path)

    @pytest.mark.parametrize(
        "doc,path",
        [(doc, path) for doc, (_positives, negatives) in sorted(CASES.items()) for path in negatives],
    )
    def test_the_key_is_scoped(self, doc: str, path: str) -> None:
        """A change nowhere near the system does not carry its document."""
        assert doc not in self._selected([path]), (doc, path)

    @pytest.mark.parametrize("doc", sorted(CASES))
    def test_editing_the_document_selects_itself(self, doc: str) -> None:
        assert doc in self._selected([doc])


class TestDocsIndexMatchesInventory:
    """``docs/README.md`` and the root README name every foundation document, and nothing that is gone.

    The index is where a contributor learns a keyed document exists; one the index omits is never opened
    (ux finding on PR #745, revisions 24–29: four keyed adapter documents absent, a merged-away file still
    linked, a count of thirteen against eighteen files). Pinning both indexes to the directory listing
    makes the next drift fail a test instead of a review.
    """

    _NUMBER_WORDS = {
        "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
        "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
        "twenty-one": 21, "twenty-two": 22, "twenty-three": 23, "twenty-four": 24, "twenty-five": 25,
    }

    @staticmethod
    def _inventory() -> set[str]:
        return {p.name for p in (_REPO_ROOT / "docs" / "foundation").glob("*.md")}

    def test_docs_index_links_every_document_and_no_ghost(self) -> None:
        import re

        text = (_REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        linked = set(re.findall(r"\]\(foundation/([\w.-]+\.md)", text))
        inventory = self._inventory()
        assert inventory - linked == set(), sorted(inventory - linked)  # unlisted document
        assert linked - inventory == set(), sorted(linked - inventory)  # link to a file that is gone

    def test_root_readme_count_matches_the_directory(self) -> None:
        import re

        text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        m = re.search(r"\]\(docs/foundation/\): ([a-z-]+) documents", text)
        assert m, "the root README no longer states the foundation's document count beside its link"
        assert self._NUMBER_WORDS[m.group(1)] == len(self._inventory()), m.group(1)

    def test_root_readme_links_every_document_and_no_ghost(self) -> None:
        import re

        text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        linked = set(re.findall(r"\]\(docs/foundation/([\w.-]+\.md)", text))
        inventory = self._inventory()
        assert inventory - linked == set(), sorted(inventory - linked)
        assert linked - inventory == set(), sorted(linked - inventory)
