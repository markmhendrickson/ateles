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


# ── Real-document reading-list budget (QA acceptance for foundation docs) ─────


class TestRealDocumentBudget:
    """Prove the committed docs/foundation/ files fit #744 caps without clip/omit.

    Fixture-only budget tests (``test_budget_truncates_a_long_doc``) show ``_clip``
    works. These assert the *real* reading-list members stay under the caps so
    review prompts carry the contract they are supposed to enforce.
    """

    # strict=True on purpose. The over-cap state is a known, measured fact, not a flaky one: if this
    # test ever passes, the budget pass has landed and the marker is stale, which should fail loudly
    # rather than sit here reporting an xpass nobody reads. Removing the marker is the operator's
    # budget-pass commit, not a test edit.
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "PR #745: the operator directed that content settles before budget, and the rule is "
            "cut and split, never raise the cap. Measured at head a2f9d71: the three-document "
            "kernel is 59,390 chars against MAX_BLOCK_CHARS=40,000 (principles.md 13,352; "
            "work_model.md 16,106; gates_and_workflows.md 29,932), and every kernel and keyed "
            "document exceeds MAX_DOC_CHARS=12,000 — vocabulary.md (73,842) and github.md (45,714) "
            "by the widest margins. Cutting ~19.4k of prose to close the block gap is a content "
            "decision the operator owns; this marker stays until that budget pass lands. "
            "See docs/foundation/status.md, 'Reading-list budget and keying'."
        ),
    )
    def test_real_documents_fit_reading_block_budget(self) -> None:
        """Every Always-read + keyed path on disk is ≤ MAX_DOC_CHARS;
        representative kernel and kernel+keyed reading_block() outputs have no
        'truncated at' / '[omitted:' markers and len(block) ≤ MAX_BLOCK_CHARS."""
        rl = load_reading_list(_REPO_ROOT)
        assert rl is not None
        docs = list(
            dict.fromkeys([*rl.kernel, *(d for e in rl.keyed for d in e.docs)])
        )
        for doc in docs:
            text = (_REPO_ROOT / doc).read_text(encoding="utf-8")
            assert len(text) <= foundation.MAX_DOC_CHARS, (
                doc,
                len(text),
                foundation.MAX_DOC_CHARS,
            )
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
            block = reading_block(list(paths), root=_REPO_ROOT)
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

    def test_pattern_bans_reach_the_ban_lists(self) -> None:
        """Every PATTERNS item is compiled into the class it is keyed under."""
        mod = self._lint()
        vocab = (_REPO_ROOT / "docs" / "foundation" / "vocabulary.md").read_text(encoding="utf-8")
        never, not_for = mod.parse_bans(vocab)
        by_kind = {"never": {b.pattern.pattern for b in never}, "not_for": {b.pattern.pattern for b in not_for}}
        for entry, kinds in mod.PATTERNS.items():
            for kind, items in kinds.items():
                for source, _sense in items:
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
