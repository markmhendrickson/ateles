#!/usr/bin/env python3
"""Tests for render_plan_page.py — the plan → rendered_page projection.

Run: python3 execution/scripts/test_render_plan_page.py

The property that matters is that the renderer FAILS rather than degrades. A
page that quietly drops a strand is worse than one that fails to publish,
because nobody reloads a page they have already read.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from render_plan_page import (  # noqa: E402
    MARKER_RE,
    PlanShapeError,
    _served_hash,
    _with_marker,
    build_html,
    parse_steps,
)

TWO_STRANDS = """SEQUENCE AS OF today.

STRAND A — UNBLOCK THE PATH

1. [SUBAGENT] Fix the silent decline. Unblocks: everything. Blocked by: nothing. Do this first.

2. [OPERATOR] Decide the policy. Blocked by: step 1.

STRAND B — DRIVE IT RELIABLY

Why this strand exists: the sequence must survive execution.

3. [SWARM] (= step 1) a cross-reference, not a blockable step.

4. [SUBAGENT] Restates an existing contract.
"""


class TestParse(unittest.TestCase):
    def test_parses_strands_and_steps(self):
        strands = parse_steps(TWO_STRANDS)
        self.assertEqual([s["letter"] for s in strands], ["A", "B"])
        self.assertEqual([s["title"] for s in strands][0], "UNBLOCK THE PATH")
        self.assertEqual(sum(len(s["steps"]) for s in strands), 4)

    def test_captures_who_and_blocker(self):
        a = parse_steps(TWO_STRANDS)[0]
        self.assertEqual(a["steps"][0]["who"], "SUBAGENT")
        self.assertEqual(a["steps"][0]["blocked_by"], "nothing")
        self.assertEqual(a["steps"][1]["who"], "OPERATOR")
        self.assertEqual(a["steps"][1]["blocked_by"], "step 1")

    def test_absent_blocker_is_none_not_empty(self):
        """A step with no 'Blocked by:' is unblocked, not unknown.

        In the live plan, steps 12-15 have no blocker because they are not
        blockable: two are cross-references, one restates a contract, one is an
        operator open item. That is data, not a parse failure.
        """
        b = parse_steps(TWO_STRANDS)[1]
        self.assertTrue(all(s["blocked_by"] is None for s in b["steps"]))

    def test_blocker_clause_removed_from_step_text(self):
        step = parse_steps(TWO_STRANDS)[0]["steps"][0]
        self.assertNotIn("Blocked by", step["text"])
        self.assertIn("Fix the silent decline", step["text"])

    def test_strand_preamble_captured(self):
        self.assertIn("survive execution", parse_steps(TWO_STRANDS)[1]["preamble"])


class TestFailsLoudly(unittest.TestCase):
    def test_no_strands_raises(self):
        with self.assertRaises(PlanShapeError):
            parse_steps("1. [SWARM] a step with no strand heading anywhere.")

    def test_empty_strand_raises(self):
        with self.assertRaises(PlanShapeError):
            parse_steps("STRAND A — ALPHA\n\nprose only, no numbered steps\n")

    def test_step_outside_every_strand_raises(self):
        """The check that catches silent omission."""
        with self.assertRaises(PlanShapeError) as ctx:
            parse_steps("9. [SWARM] orphan.\n\nSTRAND A — A\n\n1. [SWARM] real. Blocked by: nothing.\n")
        self.assertIn("9", str(ctx.exception))


class TestRender(unittest.TestCase):
    def _html(self):
        return build_html(
            {
                "title": "Test plan",
                "next_steps": TWO_STRANDS,
                "decisions": {"a_settled_thing": "It was decided."},
                "goals": ["Ship it"],
            }
        )

    def test_blocked_column_only_where_a_blocker_exists(self):
        """Reproduces from data the judgement a human made by hand."""
        html = self._html()
        self.assertEqual(html.count("<th>Blocked by</th>"), 1)

    def test_every_step_reaches_the_page(self):
        html = self._html()
        for n in (1, 2, 3, 4):
            self.assertIn(f"<td>{n}</td>", html)

    def test_operator_steps_called_out(self):
        self.assertIn("Operator-only steps", self._html())

    def test_decisions_rendered_from_the_entity(self):
        self.assertIn("a settled thing", self._html())

    def test_counts_are_computed_not_hardcoded(self):
        """The divergence that motivated this change: a hand-written '22
        decisions' in a repo file while the plan held 26."""
        html = self._html()
        self.assertIn("4 steps, 2 strands", html)
        self.assertIn("1 decisions", html)

    def test_html_is_escaped(self):
        html = build_html(
            {
                "title": "T",
                "next_steps": "STRAND A — A\n\n1. [SWARM] a <script>alert(1)</script> step. Blocked by: nothing.\n",
                "decisions": {},
                "goals": [],
            }
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class TestVerification(unittest.TestCase):
    def test_marker_roundtrip(self):
        body, digest = _with_marker("<h1>hi</h1>")
        self.assertEqual(_served_hash(body), digest)

    def test_marker_is_stable_and_not_stacked(self):
        once, d1 = _with_marker("<h1>hi</h1>")
        twice, d2 = _with_marker(once)
        self.assertEqual(d1, d2)
        self.assertEqual(len(MARKER_RE.findall(twice)), 1)

    def test_marker_changes_with_content(self):
        _, a = _with_marker("<h1>one</h1>")
        _, b = _with_marker("<h1>two</h1>")
        self.assertNotEqual(a, b)

    def test_equal_length_edit_changes_the_hash(self):
        """Byte length alone would miss this; the hash is why it is used."""
        _, a = _with_marker("<p>abc</p>")
        _, b = _with_marker("<p>xyz</p>")
        self.assertNotEqual(a, b)

    def test_missing_marker_reads_as_none(self):
        self.assertIsNone(_served_hash("<h1>no marker here</h1>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
