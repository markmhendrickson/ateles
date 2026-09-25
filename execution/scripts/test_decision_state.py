#!/usr/bin/env python3
"""Fail-then-pass coverage for the decision-state generator and plan checker.

Every case here is a *planted positive*: it asserts the check goes red on an
input it must catch, not merely green on one it must pass. A check that has
never gone red on the thing it watches proves nothing
(``principles.md``), and the two failures these scripts exist to prevent —
a ruling invisible on ``origin/main``, and a plan field asserting a status the
register contradicts — are both silent when they occur.
"""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_plan_decision_citations as plan_check
import render_decision_state as ds


def register(
    questions: dict[str, str] | None = None,
    argued_in: dict[str, str] | None = None,
    **rows: str,
) -> str:
    """A minimal register table with the given {number: status cell}.

    ``questions`` and ``argued_in`` override the default placeholder text for
    specific row numbers -- needed by any test distinguishing rows on their
    actual subject rather than merely on their number.
    """
    questions = questions or {}
    argued_in = argued_in or {}
    head = "| # | The question | Argued in | Blocks | Status |\n|---|---|---|---|---|\n"
    body = "".join(
        f"| {n} | {questions.get(n, f'question {n}')} | "
        f"`{argued_in.get(n, f'doc.md#a{n}')}` | — | {cell} |\n"
        for n, cell in rows.items()
    )
    return head + body


class TestRegisterParsing(unittest.TestCase):
    def test_status_is_the_bold_lead_not_a_word_later_in_the_cell(self):
        """"never reopened" in a ruling's prose must not read as a status.

        Matching by containment is the defect this guards: it mislabelled nine
        ruled rows as open in an earlier revision of the sibling checker.
        """
        text = register(**{"40": "**ruled** (2026-09-06): and it is never reopened"})
        self.assertEqual(ds.parse_rows(text)["40"]["status"], "ruled")

    def test_reopened_is_not_ruled(self):
        """A question ruled then unsettled is not answered."""
        text = register(**{"95": "**reopened** (2026-09-08) after being ruled"})
        rows = ds.parse_rows(text)
        self.assertEqual(rows["95"]["status"], "reopened")
        row = ds.Row(
            number="95",
            question="q",
            argued_in="",
            blocks="",
            main_status="reopened",
        )
        self.assertEqual(row.ruled, "no")
        self.assertEqual(row.merged, "no")

    def test_ruled_in_part_beats_ruled_in_the_status_order(self):
        text = register(**{"36": "**ruled in part** (2026-09-05): half of it"})
        self.assertEqual(ds.parse_rows(text)["36"]["status"], "ruled in part")

    def test_withdrawn_rows_carry_no_question_so_no_axis_applies(self):
        row = ds.Row(
            number="20", question="—", argued_in="", blocks="", main_status="withdrawn"
        )
        self.assertEqual(row.ruled, "n/a")
        self.assertEqual(row.merged, "n/a")


class TestThreeAxes(unittest.TestCase):
    def test_ruled_on_a_branch_and_open_on_main_is_ruled_but_not_merged(self):
        """The decision-101 case: an executor reading the corpus sees open."""
        row = ds.Row(
            number="101",
            question="what the credential binding carries",
            argued_in="",
            blocks="stage 1 of the migration",
            main_status="open",
            ruled_on_branches=["origin/claude/foundation-decision-101"],
        )
        self.assertEqual(row.ruled, "yes")
        self.assertEqual(row.merged, "no")
        self.assertIn("decision-101", row.ruling_lives)

    def test_implemented_defaults_to_unknown_and_is_never_inferred(self):
        """A ruling's existence is not evidence anything implements it."""
        row = ds.Row(
            number="101",
            question="q",
            argued_in="",
            blocks="",
            main_status="ruled",
            ruled_on_branches=[],
        )
        self.assertEqual(row.ruled, "yes")
        self.assertEqual(row.merged, "yes")
        self.assertEqual(row.implemented, "unknown")

    def test_merged_row_gains_nothing_from_a_branch_copy(self):
        row = ds.Row(
            number="78", question="q", argued_in="", blocks="", main_status="ruled"
        )
        self.assertEqual(row.ruling_lives, "—")

    def test_committed_render_ignores_ruled_on_branches(self):
        """The committed document is a pure function of main's own status.

        `ruled_on_branches` only ever gets populated by `collect_branches`, and
        `render()` -- the committed document -- must produce the same bytes
        whether or not a Row happens to carry it. That is what makes the
        committed file deterministic for a given commit: nothing about another
        branch may leak into it, even via a field the dataclass still has for
        `collect_branches`'s benefit.
        """
        bare = [
            ds.Row(
                number="101",
                question="what the credential binding carries",
                argued_in="`authority_model.md#x`",
                blocks="stage 1",
                main_status="open",
            )
        ]
        with_branches = [
            ds.Row(
                number="101",
                question="what the credential binding carries",
                argued_in="`authority_model.md#x`",
                blocks="stage 1",
                main_status="open",
                ruled_on_branches=["origin/claude/decision-101"],
            )
        ]
        self.assertEqual(ds.render(bare), ds.render(with_branches))

    def test_a_changed_ruling_does_change_the_output(self):
        """The other half: the check must still go red on what it watches."""
        base = ds.Row(
            number="101", question="q", argued_in="", blocks="", main_status="open"
        )
        ruled = ds.Row(
            number="101", question="q", argued_in="", blocks="", main_status="ruled"
        )
        self.assertNotEqual(ds.render([base]), ds.render([ruled]))

    def test_bare_anchor_citations_are_requalified_to_the_register(self):
        """In a register cell `#x` means conformance.md; copied here it would not."""
        self.assertEqual(
            ds.requalify("see `#scope` and `other.md#y`"),
            "see `conformance.md#scope` and `other.md#y`",
        )

    def test_subject_is_truncated_and_says_so(self):
        long = " ".join(f"w{i}" for i in range(40))
        self.assertTrue(ds.subject(long).endswith("…"))
        self.assertEqual(ds.subject("—"), "—")


class TestPlanCitations(unittest.TestCase):
    ROWS = {"1–12": "ruled", "76": "ruled", "78": "ruled", "84": "open", "95": "reopened"}

    def test_not_yet_merged_on_a_merged_decision_is_caught(self):
        """The exact corrupted-array shape: "NOT YET MERGED" of a merged row."""
        entity = {"entity_id": "e", "decision_blockers": ["decision 78 NOT YET MERGED"]}
        problems = plan_check.check_entity(entity, self.ROWS, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("status-disagreement", problems[0])
        self.assertIn("78", problems[0])

    def test_claiming_open_on_a_ruled_row_is_caught(self):
        entity = {"entity_id": "e", "body": "decision 76 remains open"}
        problems = plan_check.check_entity(entity, self.ROWS, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("status-disagreement", problems[0])

    def test_past_tense_status_is_history_and_is_not_judged(self):
        """"decision 76 was open" is correct as written."""
        entity = {"entity_id": "e", "body": "In June, decision 76 was open."}
        self.assertEqual(plan_check.check_entity(entity, self.ROWS, {}), [])

    def test_a_correct_status_claim_passes(self):
        entity = {"entity_id": "e", "body": "decision 84 is open"}
        self.assertEqual(plan_check.check_entity(entity, self.ROWS, {}), [])

    def test_dangling_decision_number(self):
        entity = {"entity_id": "e", "body": "blocked on decision 999"}
        problems = plan_check.check_entity(entity, self.ROWS, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("dangling", problems[0])

    def test_a_row_that_exists_only_on_a_branch_is_pending_not_dangling(self):
        """Reading ahead of the corpus is a different fact from citing nothing."""
        entity = {"entity_id": "e", "body": "decision 105 opened"}
        problems = plan_check.check_entity(
            entity, self.ROWS, {"105": ["origin-pr/914"]}
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("pending", problems[0])
        self.assertIn("origin-pr/914", problems[0])

    def test_the_combined_row_covers_its_range(self):
        """Decision 7 resolves through the "1–12" row, not as dangling."""
        entity = {"entity_id": "e", "body": "decision 7 applies"}
        self.assertEqual(plan_check.check_entity(entity, self.ROWS, {}), [])

    def test_a_plural_citation_is_checked_entry_by_entry(self):
        entity = {"entity_id": "e", "body": "decisions 76, 78 and 999 block this"}
        problems = plan_check.check_entity(entity, self.ROWS, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("999", problems[0])

    def test_missing_foundation_document(self):
        entity = {"entity_id": "e", "body": "see docs/foundation/nope.md"}
        problems = plan_check.check_entity(entity, self.ROWS, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("missing-document", problems[0])

    def test_an_existing_foundation_document_passes(self):
        entity = {"entity_id": "e", "body": "see docs/foundation/conformance.md"}
        self.assertEqual(plan_check.check_entity(entity, self.ROWS, {}), [])

    def test_citations_nested_anywhere_are_found(self):
        """Fields differ per plan; naming them exhaustively would go stale."""
        entity = {
            "entity_id": "e",
            "snapshot": {"snapshot": {"todos": {"t1": {"notes": "decision 999"}}}},
        }
        problems = plan_check.check_entity(entity, self.ROWS, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("snapshot.snapshot.todos.t1.notes", problems[0])


class TestPathFilterCoversThisFile(unittest.TestCase):
    """This file must be in foundation-checks.yml's `paths:` filter.

    It was not, until #930. The lane invoked these tests and the trigger never
    fired for a change that touched only them, so the suite could go red on a
    branch and the merge gate would report nothing — the check present, the lane
    absent from the change (`principles.md` §1).

    Asserting it here rather than trusting the YAML means a future edit that
    drops the entry fails in the suite the entry exists to run.
    """

    def test_this_file_is_named_in_the_foundation_lane_filter(self):
        workflow = (
            Path(__file__).resolve().parents[2]
            / ".github"
            / "workflows"
            / "foundation-checks.yml"
        )
        if not workflow.is_file():
            self.skipTest(f"{workflow} absent on this branch")
        text = workflow.read_text(encoding="utf-8")
        self.assertIn(
            "execution/scripts/test_decision_state.py",
            text,
            "this test file is not in the foundation lane's paths: filter, so a "
            "change touching only it would skip the lane that runs it",
        )
        self.assertIn(
            "execution/scripts/check_plan_decision_citations.py",
            text,
            "check_plan_decision_citations.py is not in the paths: filter",
        )


class TestBranchSweepIsMachineIndependent(unittest.TestCase):
    """The ref set must be the one every clone has, not the one this clone has.

    The committed document was rendered on a clone carrying a non-default
    refspec for pull-request heads (`+refs/pull/*/head:refs/remotes/origin-pr/*`),
    nine of them. CI, with a default checkout, had none, read a different set of
    registers, and `--check` went red against a file nobody had edited.

    That is the failure the document exists to prevent, arriving in the document's
    own generator: a projection whose output depends on which machine rendered it
    reports a decision's state as a function of local git configuration.
    """

    def test_sweep_is_scoped_to_the_origin_namespace(self):
        args = ds.BRANCH_LIST_ARGS
        self.assertEqual(
            args[-1],
            "refs/remotes/origin/",
            "the sweep must name refs/remotes/origin/ explicitly: a bare "
            "refs/remotes/ picks up any namespace a local refspec happens to "
            "fill, and the render stops being reproducible off this machine",
        )

    def test_a_pull_request_ref_namespace_is_not_swept(self):
        """A ref only some clones have must not reach the render."""
        self.assertNotIn(
            "refs/remotes/",
            [a for a in ds.BRANCH_LIST_ARGS if a == "refs/remotes/"],
            "refs/remotes/ as the whole namespace admits origin-pr/* and any "
            "other locally-configured mirror",
        )


class TestSupersessionIsJudgedOnContent(unittest.TestCase):
    """Staleness is a question about the row, not about commit history.

    The first implementation gated a branch's ruling on whether the branch
    descended from `git rev-list -1 origin/main -- conformance.md`. That command
    returns a different commit depending on how a checkout built its history, so
    CI marked 66 refs stale where a developer's clone marked 2, and the same
    source rendered two different documents — decision 93 ruled on one machine
    and unruled on the other. A projection whose output depends on the machine
    cannot be a `--check` gate.

    Decision 95 is the case the gate exists for: ruled and reopened the same day,
    with branches still carrying the superseded ruling. Reading one of those as a
    ruling main is missing would report a reopened question as answered.
    """

    RULED = {"status": "ruled", "status_cell": "**ruled**"}

    def test_a_reopened_row_supersedes_a_branchs_ruling(self):
        main_row = {"status": "reopened", "status_cell": "**reopened**"}
        self.assertTrue(ds.is_superseded(main_row, self.RULED))

    def test_an_open_row_does_not_supersede(self):
        """The ordinary ruled-but-not-merged case must survive."""
        main_row = {"status": "open", "status_cell": "**open**"}
        self.assertFalse(ds.is_superseded(main_row, self.RULED))

    def test_a_row_main_has_never_seen_is_not_superseded(self):
        self.assertFalse(ds.is_superseded(None, self.RULED))

    def test_supersession_does_not_consult_git(self):
        """The test must be answerable from two dicts and nothing else.

        If it ever needs a repository to answer, the machine-dependence is back.
        """
        import inspect

        src = inspect.getsource(ds.is_superseded)
        for forbidden in ("run(", "git", "merge-base", "rev-list"):
            self.assertNotIn(
                forbidden,
                src.split('"""')[-1],
                f"is_superseded must not shell out ({forbidden!r} in its body)",
            )


class FakeGit:
    """Stubs `ds.run` so `collect_main`/`collect_branches` need no repository.

    Maps ("git", "show", "<ref>:<path>") to register text per ref, and the
    branch-listing call to a fixed set of refs. Anything else is an error --
    a test that silently no-ops on an unmocked call would prove nothing.
    """

    def __init__(self, registers: dict[str, str], branch_refs: list[str] | None = None):
        self.registers = registers
        self.branch_refs = branch_refs if branch_refs is not None else list(registers)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        if args[:2] == ["git", "show"]:
            ref = args[2].split(":", 1)[0]
            text = self.registers.get(ref)
            return (0, text) if text is not None else (1, "")
        if tuple(args) == ds.BRANCH_LIST_ARGS:
            lines = "\n".join(f"refs/remotes/{r}" for r in self.branch_refs if r != ds.MAIN_REF)
            return (0, lines)
        raise AssertionError(f"unmocked git call in FakeGit: {args!r}")


class TestCommittedProjectionIsBranchIndependent(unittest.TestCase):
    """An open PR's --check result must not move when an unrelated branch does.

    This is the acceptance criterion the task states directly: the committed
    `decision_state.md` used to sweep every `origin/*` branch, so any unrelated
    push anywhere changed the committed file's bytes and failed `--check` on a
    PR whose own diff never touched the register. `collect_main` reads only
    `origin/main`, so it must return byte-identical rows regardless of what
    other branches exist or say -- proven here by adding an unrelated branch,
    a branch ruling the SAME row differently, and a branch that doesn't parse.
    """

    MAIN = register(**{"111": "**open**"})

    def _rendered_with(self, registers: dict[str, str], branch_refs: list[str]) -> str:
        fake = FakeGit(registers, branch_refs)
        with unittest.mock.patch.object(ds, "run", fake):
            rows = ds.collect_main()
        return ds.render(rows)

    def test_an_unrelated_branch_moving_does_not_change_the_committed_render(self):
        baseline = self._rendered_with({ds.MAIN_REF: self.MAIN}, [])

        with_unrelated_branch = self._rendered_with(
            {
                ds.MAIN_REF: self.MAIN,
                "origin/some/unrelated-branch": register(**{"200": "**ruled** (2026-09-20)"}),
            },
            ["origin/some/unrelated-branch"],
        )
        self.assertEqual(baseline, with_unrelated_branch)

        with_a_ruling_of_our_own_row = self._rendered_with(
            {
                ds.MAIN_REF: self.MAIN,
                "origin/claude/rules-111": register(**{"111": "**ruled** (2026-09-24)"}),
            },
            ["origin/claude/rules-111"],
        )
        self.assertEqual(
            baseline,
            with_a_ruling_of_our_own_row,
            "collect_main must not read any origin/* branch at all -- a "
            "ruling that exists ONLY on a branch is invisible to the "
            "committed document by design (see --branches instead)",
        )

    def test_collect_main_never_calls_the_branch_listing(self):
        """The strongest form of the guarantee: main-only reading is structural."""
        fake = FakeGit({ds.MAIN_REF: self.MAIN}, ["origin/decoy"])
        with unittest.mock.patch.object(ds, "run", fake):
            ds.collect_main()
        for call in fake.calls:
            self.assertNotEqual(
                tuple(call),
                ds.BRANCH_LIST_ARGS,
                "collect_main must never enumerate origin/* branches -- doing "
                "so is how a branch push reached the committed file before",
            )


class TestReusedRowNumberIsACollisionNeverARuling(unittest.TestCase):
    """ateles#1288 / #1088: two branches claimed row 111 for different questions.

    A number-only match reads the second branch's row as "main's row 111,
    ruled, not yet merged" -- which is false; main's row 111 was never
    unmerged, the branch was answering an unrelated question that happened to
    reuse the number. subject_key (the row's own question text) must tell the
    two apart, and a branch row whose key disagrees with main's row of the
    same number must be reported as a collision and must never appear in
    `ruled_on_branches`.
    """

    def test_same_number_different_subject_is_a_collision_not_a_ruling(self):
        main_text = register(
            questions={"111": "whether a rule's end may be a condition"},
            argued_in={"111": "data_model.md#whether-a-rules-end-is-a-condition"},
            **{"111": "**open**"},
        )
        branch_text = register(
            questions={"111": "whether relationship grants inherit across a hierarchy"},
            argued_in={"111": "work_model.md#relationship-grants-for-a-different-question"},
            **{"111": "**ruled** (2026-09-24): unrelated ruling"},
        )
        fake = FakeGit(
            {ds.MAIN_REF: main_text, "origin/ruling-925-relationship-grants": branch_text},
            ["origin/ruling-925-relationship-grants"],
        )
        with unittest.mock.patch.object(ds, "run", fake):
            rows, read, stale, collisions = ds.collect_branches(
                ["origin/ruling-925-relationship-grants"]
            )

        row_111 = next(r for r in rows if r.number == "111")
        self.assertEqual(
            row_111.ruled_on_branches,
            [],
            "a collision must never be admitted into ruled_on_branches -- "
            "that is exactly the misread that produced the live defect",
        )
        self.assertEqual(row_111.merged, "no")
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0].number, "111")
        self.assertEqual(collisions[0].branch, "origin/ruling-925-relationship-grants")
        self.assertNotEqual(
            collisions[0].main_subject_key, collisions[0].branch_subject_key
        )
        self.assertIn("condition", collisions[0].main_question)
        self.assertIn("relationship grants", collisions[0].branch_question)

    def test_same_number_same_subject_is_still_read_as_a_ruling(self):
        """The ordinary case must survive: identical subject key, same row."""
        question = "whether a rule's end may be a condition"
        main_text = register(
            questions={"111": question}, **{"111": "**open**"}
        )
        branch_text = register(
            questions={"111": question},
            **{"111": "**ruled** (2026-09-24): the real ruling"},
        )
        fake = FakeGit(
            {ds.MAIN_REF: main_text, "origin/claude/decision-111": branch_text},
            ["origin/claude/decision-111"],
        )
        with unittest.mock.patch.object(ds, "run", fake):
            rows, read, stale, collisions = ds.collect_branches(
                ["origin/claude/decision-111"]
            )

        row_111 = next(r for r in rows if r.number == "111")
        self.assertEqual(row_111.ruled_on_branches, ["origin/claude/decision-111"])
        self.assertEqual(row_111.ruled, "yes")
        self.assertEqual(row_111.merged, "no")
        self.assertEqual(collisions, [])

    def test_same_question_survives_an_argued_in_anchor_rewrite(self):
        """Row 99's live shape: ruling retitles the anchor, question is untouched.

        Found while proving this generator against the corpus's actual open
        branches rather than synthetic cases alone: row 99's anchor moved from
        a heading describing the open question to one stating the ruling, with
        the question column identical before and after. An anchor-keyed match
        read that ordinary rename as a collision; the question-keyed match
        must not.
        """
        question = (
            "whether a swarm with several instances of the record has "
            "planning of its own"
        )
        main_text = register(
            questions={"99": question},
            argued_in={"99": "planning_model.md#what-this-does-not-reach"},
            **{"99": "**open**"},
        )
        branch_text = register(
            questions={"99": question},
            argued_in={"99": "planning_model.md#every-planning-record-belongs-to-an-instance"},
            **{"99": "**ruled** (2026-09-20): ruled for the controlling instance"},
        )
        fake = FakeGit(
            {ds.MAIN_REF: main_text, "origin/decisions-99": branch_text},
            ["origin/decisions-99"],
        )
        with unittest.mock.patch.object(ds, "run", fake):
            rows, read, stale, collisions = ds.collect_branches(["origin/decisions-99"])

        self.assertEqual(collisions, [])
        row_99 = next(r for r in rows if r.number == "99")
        self.assertEqual(row_99.ruled_on_branches, ["origin/decisions-99"])

    def test_a_reworded_question_under_the_same_argued_in_anchor_is_not_a_collision(self):
        """Rows 84/102/107's live shape: prose tightened mid-draft, anchor stable.

        The mirror image of row 99: an open PR branch reworded the question
        column while ruling it, with `argued_in` never moving. A question-only
        match would misread this as a collision; agreement on EITHER field
        (same_subject) must not.
        """
        anchor = "work_model.md#what-distinguishes-a-task-being-assembled"
        main_text = register(
            questions={"84": "what distinguishes a task with no intake batch"},
            argued_in={"84": anchor},
            **{"84": "**open**"},
        )
        branch_text = register(
            questions={"84": "what distinguishes a workflow-entering task still being assembled"},
            argued_in={"84": anchor},
            **{"84": "**ruled** (2026-09-24): ruled on the branch"},
        )
        fake = FakeGit(
            {ds.MAIN_REF: main_text, "origin/decision-84": branch_text},
            ["origin/decision-84"],
        )
        with unittest.mock.patch.object(ds, "run", fake):
            rows, read, stale, collisions = ds.collect_branches(["origin/decision-84"])

        self.assertEqual(collisions, [])
        row_84 = next(r for r in rows if r.number == "84")
        self.assertEqual(row_84.ruled_on_branches, ["origin/decision-84"])

    def test_same_subject_true_when_only_question_agrees(self):
        a = ("whether x is true", "doc.md#anchor-one")
        b = ("whether x is true", "doc.md#a-different-anchor")
        self.assertTrue(ds.same_subject(a, b))

    def test_same_subject_true_when_only_argued_in_agrees(self):
        a = ("an old phrasing of the question", "doc.md#anchor-one")
        b = ("a reworded phrasing of the question", "doc.md#anchor-one")
        self.assertTrue(ds.same_subject(a, b))

    def test_same_subject_false_when_both_disagree(self):
        """The #1288/#1088 shape: nothing in common on either field."""
        a = ("whether a rule's end may be a condition", "data_model.md#anchor-one")
        b = ("whether relationship grants inherit", "work_model.md#anchor-two")
        self.assertFalse(ds.same_subject(a, b))

    def test_same_subject_false_when_both_blank(self):
        """A blank field never agrees with another blank field."""
        self.assertFalse(ds.same_subject(("", ""), ("", "")))

    def test_subject_key_is_case_insensitive_and_whitespace_normalized(self):
        row_a = {"question": "Whether   X  is true", "argued_in": "", "status": "ruled"}
        row_b = {"question": "whether x is true", "argued_in": "", "status": "ruled"}
        self.assertEqual(ds.subject_key(row_a), ds.subject_key(row_b))

    def test_subject_key_falls_back_to_argued_in_when_question_is_blank(self):
        row = {"question": "—", "argued_in": "`doc.md#the-anchor`", "status": "open"}
        question, argued_in = ds.subject_key(row)
        self.assertEqual(question, "")
        self.assertIn("the-anchor", argued_in)


class TestCommittedOutputIsByteStableForACommit(unittest.TestCase):
    """The acceptance criterion, stated as bytes: same commit, same output.

    Rendering `collect_main()`'s result twice from the identical main register,
    with different (even absent) branch state each time, must produce identical
    bytes -- not merely equal-looking markdown. `--check` compares bytes, so
    this is the literal thing the gate depends on.
    """

    def test_render_is_byte_identical_across_repeated_renders(self):
        main_text = register(
            **{
                "1–12": "**ruled** (2026-01-01)",
                "76": "**ruled** (2026-02-01)",
                "84": "**open**",
                "95": "**reopened** (2026-03-01)",
            }
        )
        fake1 = FakeGit({ds.MAIN_REF: main_text}, [])
        with unittest.mock.patch.object(ds, "run", fake1):
            rendered_first = ds.render(ds.collect_main())

        # A second render, from a DIFFERENT clone's view of the world (extra
        # branches present, listed in a different order) but the SAME main
        # register text -- the scenario an open PR is actually in each time CI
        # re-runs it.
        fake2 = FakeGit(
            {ds.MAIN_REF: main_text, "origin/z/newer": register(**{"300": "**open**"})},
            ["origin/z/newer"],
        )
        with unittest.mock.patch.object(ds, "run", fake2):
            rendered_second = ds.render(ds.collect_main())

        self.assertEqual(rendered_first, rendered_second)
        self.assertEqual(
            rendered_first.encode("utf-8"), rendered_second.encode("utf-8")
        )


if __name__ == "__main__":
    unittest.main()
