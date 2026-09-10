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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_plan_decision_citations as plan_check
import render_decision_state as ds


def register(**rows: str) -> str:
    """A minimal register table with the given {number: status cell}."""
    head = "| # | The question | Argued in | Blocks | Status |\n|---|---|---|---|---|\n"
    body = "".join(
        f"| {n} | question {n} | `doc.md#a{n}` | — | {cell} |\n"
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

    def test_output_does_not_change_when_only_the_branch_count_changes(self):
        """The document changes when a decision's state changes, not otherwise.

        Writing a branch count into the output made `--check` fail whenever
        anyone pushed anything — red for a reason unrelated to any decision. A
        check the reader learns to dismiss has stopped being a control.
        """
        rows = [
            ds.Row(
                number="101",
                question="what the credential binding carries",
                argued_in="`authority_model.md#x`",
                blocks="stage 1",
                main_status="open",
                ruled_on_branches=["origin/claude/decision-101"],
            )
        ]
        few = ds.render(rows, ["origin/a"], ["origin/x"])
        many = ds.render(
            rows, ["origin/a", "origin/b", "origin/c"], ["origin/x", "origin/y"]
        )
        self.assertEqual(few, many)

    def test_a_changed_ruling_does_change_the_output(self):
        """The other half: the check must still go red on what it watches."""
        base = ds.Row(
            number="101", question="q", argued_in="", blocks="", main_status="open"
        )
        ruled = ds.Row(
            number="101",
            question="q",
            argued_in="",
            blocks="",
            main_status="open",
            ruled_on_branches=["origin/claude/decision-101"],
        )
        self.assertNotEqual(ds.render([base], [], []), ds.render([ruled], [], []))

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


if __name__ == "__main__":
    unittest.main()
