#!/usr/bin/env python3
"""Tests for the `evaluate-person` skill's guard scripts.

Each test replays a REAL failure from the 2026-09-22 ad-hoc review session that
motivated this skill. The point of every test is that it goes RED against the
behaviour that produced the wrong evaluation -- not that it agrees with the code
as written.

What each test looked like red (verified by mutating the guard and re-running):

  test_partial_slack_sweep_is_rejected
      RED when _failures() does not compare total_read against total_matched:
      the 3-of-24-Slack-conversations manifest passes `check` and the sweep is
      declared complete. That is the original bug exactly.

  test_swept_without_denominator_is_rejected
      RED when a null total_matched is allowed through: a surface can claim
      `swept` while nobody ever counted what was there, which is how "enough
      material" passes for "all the material".

  test_unmeasured_filter_is_rejected
      RED when a recorded filter with a null excluded count passes: the
      arbitrary date floor was exactly an unmeasured filter.

  test_zero_without_anchor_is_rejected
      RED when _failures() accepts an entry whose anchor_result is "fail": the
      pre-migration Neotoma count is reported as a fact about the person.

  test_zero_with_anchor_but_no_hazard_is_rejected
      RED when a zero needs only an anchor: an undeclared schema field can
      return a true zero for a query that is itself well-formed.

  test_five_same_direction_corrections_trip_the_wire
      RED when _failures() checks citations individually but never counts them:
      all five of the original misattributions pass every per-citation check and
      the systematic bias ships. This is the test that matters most.

  test_opposite_direction_correction_clears_the_wire
      RED when the tripwire fires on volume rather than on asymmetry -- it would
      block legitimate runs and get disabled, which is worse than no tripwire.

  test_self_accounting_read_as_others_obligation_needs_convention
      RED when the genre/direction combination that failed five times carries no
      extra burden of proof.

  test_unresolved_direction_cannot_support_a_finding
      RED when a citation with an unresolved obligation direction may still be
      marked subject-at-fault.

  test_sensitive_scan_flags_art9_and_passes_effect_only_phrasing
      RED when the scanner has no health vocabulary (the "diagnosed with" line
      publishes), and RED in the other direction when it is so broad that the
      prescribed effect-only replacement also trips -- which would make the
      blocker unclearable and train the operator to ignore it.

Run:  python3 test_evaluate_person.py        (stdlib unittest, no deps)
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SWEEP = HERE / "sweep_manifest.py"
INSTR = HERE / "instrument_log.py"
ATTR = HERE / "attribution_check.py"
SENS = HERE / "sensitive_scan.py"


def run(script: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, input=stdin,
    )


class SweepManifestTests(unittest.TestCase):
    """The 3-of-24 Slack failure and its neighbours."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "manifest.json")
        run(SWEEP, "init", "--path", self.path, "--subject", "Test Subject",
            "--surfaces", "slack,gmail")
        self.addCleanup(self.tmp.cleanup)

    def _all_other_surfaces_ok(self) -> None:
        run(SWEEP, "record", "--path", self.path, "--surface", "gmail",
            "--status", "swept", "--total-matched", "10", "--total-read", "10")

    def test_partial_slack_sweep_is_rejected(self) -> None:
        """THE original bug: 3 of 24 conversations declared a complete sweep."""
        self._all_other_surfaces_ok()
        run(SWEEP, "record", "--path", self.path, "--surface", "slack",
            "--status", "swept", "--total-matched", "24", "--total-read", "3")
        r = run(SWEEP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1, "a 3-of-24 sweep must not pass as complete")
        self.assertIn("read 3 of 24", r.stderr)
        self.assertIn("'partial', not 'swept'", r.stderr)

    def test_swept_without_denominator_is_rejected(self) -> None:
        self._all_other_surfaces_ok()
        run(SWEEP, "record", "--path", self.path, "--surface", "slack",
            "--status", "swept", "--total-read", "3")
        r = run(SWEEP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no denominator", r.stderr)

    def test_unmeasured_filter_is_rejected(self) -> None:
        """The arbitrary date floor: a filter whose excluded count nobody knows."""
        self._all_other_surfaces_ok()
        run(SWEEP, "record", "--path", self.path, "--surface", "slack",
            "--status", "swept", "--total-matched", "24", "--total-read", "24",
            "--filter", "since 2026-08-01")
        r = run(SWEEP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("excluded count is null", r.stderr)

    def test_complete_sweep_passes(self) -> None:
        self._all_other_surfaces_ok()
        run(SWEEP, "record", "--path", self.path, "--surface", "slack",
            "--status", "swept", "--total-matched", "24", "--total-read", "24",
            "--excluded-by-filter", "0")
        r = run(SWEEP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SWEEP OK", r.stdout)

    def test_unreachable_surface_needs_a_reason(self) -> None:
        self._all_other_surfaces_ok()
        r = run(SWEEP, "record", "--path", self.path, "--surface", "slack",
                "--status", "unreachable")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("requires --note", r.stderr)

    def test_cite_names_searched_and_unsearched_surfaces(self) -> None:
        """A negative finding must name surfaces, never stand bare."""
        run(SWEEP, "record", "--path", self.path, "--surface", "gmail",
            "--status", "swept", "--total-matched", "312", "--total-read", "312")
        run(SWEEP, "record", "--path", self.path, "--surface", "slack",
            "--status", "unreachable", "--note", "no export access")
        r = run(SWEEP, "cite", "--path", self.path, "--surfaces", "gmail,slack")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Searched: gmail (312 items, full range)", r.stdout)
        self.assertIn("Not searched: slack (no export access)", r.stdout)


class InstrumentLogTests(unittest.TestCase):
    """The pre-migration count and the undeclared-field zero."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "log.json")
        self.addCleanup(self.tmp.cleanup)

    def test_zero_without_anchor_is_rejected(self) -> None:
        run(INSTR, "add", "--path", self.path, "--metric", "deliverables in Neotoma",
            "--query", "retrieve_entities deliverable", "--count", "0",
            "--anchor", "known deliverable ent_abc", "--anchor-result", "fail")
        r = run(INSTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("measurement of the instrument", r.stderr)

    def test_zero_with_anchor_but_no_hazard_is_rejected(self) -> None:
        run(INSTR, "add", "--path", self.path, "--metric", "deliverables",
            "--query", "retrieve_entities deliverable", "--count", "0",
            "--anchor", "ent_abc returns", "--anchor-result", "pass")
        r = run(INSTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no hazard ruled out", r.stderr)

    def test_anchored_zero_with_hazard_passes(self) -> None:
        run(INSTR, "add", "--path", self.path, "--metric", "deliverables",
            "--query", "retrieve_entities deliverable", "--count", "0",
            "--anchor", "ent_abc returns", "--anchor-result", "pass",
            "--hazard", "undeclared-schema-field",
            "--hazard", "pre-migration-snapshot")
        r = run(INSTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("INSTRUMENTS OK", r.stdout)

    def test_empty_log_is_rejected(self) -> None:
        r = run(INSTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no count in the evaluation has been validated", r.stderr)

    def test_unknown_hazard_is_rejected(self) -> None:
        r = run(INSTR, "add", "--path", self.path, "--metric", "m", "--query", "q",
                "--count", "0", "--anchor", "a", "--anchor-result", "pass",
                "--hazard", "vibes")
        self.assertNotEqual(r.returncode, 0)


class AttributionCheckTests(unittest.TestCase):
    """The five same-direction misattributions -- the core of this skill."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "attr.json")
        self.addCleanup(self.tmp.cleanup)

    def _add(self, citation: str, *, initial: str, final: str,
             genre: str = "self-accounting",
             direction: str = "subject-owes-operator",
             convention: str | None = "other items name the person owed",
             distinguisher: str | None = "her status note restates them as hers") -> None:
        args = [
            "add", "--path", self.path, "--citation", citation,
            "--author-confirmed-by", "file owner metadata",
            "--genre", genre, "--obligation-direction", direction,
            "--opposite-reading", "a complaint that the operator is late",
            "--initial-reading", initial, "--final-reading", final,
        ]
        if convention:
            args += ["--convention", convention]
        if distinguisher:
            args += ["--distinguisher", distinguisher]
        run(ATTR, *args)

    def test_five_same_direction_corrections_trip_the_wire(self) -> None:
        """Each citation passes individually; the PATTERN is the finding.

        This replays the motivating failure at full size: five corrections, all
        moving the reading away from the subject's fault, none the other way.
        """
        for i in range(5):
            self._add(f"todo.md item {i}", initial="operator-at-fault",
                      final="subject-at-fault")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1, "five one-directional corrections must trip")
        self.assertIn("DIRECTION TRIPWIRE", r.stderr)
        self.assertIn("signature of systematic misreading", r.stderr)

    def test_three_same_direction_corrections_trip_the_wire(self) -> None:
        """Threshold is 3, so the bias is caught before it reaches five."""
        for i in range(3):
            self._add(f"todo.md item {i}", initial="subject-at-fault",
                      final="operator-at-fault")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("DIRECTION TRIPWIRE", r.stderr)

    def test_two_same_direction_corrections_do_not_trip(self) -> None:
        for i in range(2):
            self._add(f"todo.md item {i}", initial="subject-at-fault",
                      final="operator-at-fault")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_opposite_direction_correction_clears_the_wire(self) -> None:
        """Asymmetry is the signal, not volume.

        A tripwire that fires on ordinary correction volume gets switched off,
        which leaves the real bias undetected. Five corrections with one running
        the other way are evidence of careful reading, not of bias.
        """
        for i in range(5):
            self._add(f"todo.md item {i}", initial="operator-at-fault",
                      final="subject-at-fault")
        self._add("slack msg 9", initial="subject-at-fault",
                  final="operator-at-fault")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ATTRIBUTION OK", r.stdout)

    def test_self_accounting_read_as_others_obligation_needs_convention(self) -> None:
        """The exact genre/direction pair that failed five times."""
        self._add("todo.md item 1", initial="operator-at-fault",
                  final="operator-at-fault", genre="self-accounting",
                  direction="operator-owes-subject", convention=None)
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("precise failure this check exists for", r.stderr)

    def test_self_accounting_as_others_obligation_passes_with_convention(self) -> None:
        self._add("todo.md item 1", initial="operator-at-fault",
                  final="operator-at-fault", genre="self-accounting",
                  direction="operator-owes-subject",
                  convention="this list's 'blocked by X' column names the blocker")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unresolved_direction_cannot_support_a_finding(self) -> None:
        self._add("ambiguous note", initial="subject-at-fault",
                  final="subject-at-fault", direction="unresolved")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("must be marked 'ambiguous'", r.stderr)

    def test_ambiguous_needs_a_distinguisher(self) -> None:
        self._add("ambiguous note", initial="subject-at-fault", final="ambiguous",
                  direction="unresolved", distinguisher=None)
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no distinguishing artifact", r.stderr)

    def test_empty_table_is_rejected(self) -> None:
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no citations recorded", r.stderr)

    def test_author_must_be_confirmed_from_metadata(self) -> None:
        run(ATTR, "add", "--path", self.path, "--citation", "note.md",
            "--author-confirmed-by", "", "--genre", "self-accounting",
            "--obligation-direction", "subject-owes-operator",
            "--opposite-reading", "x", "--initial-reading", "no-fault",
            "--final-reading", "no-fault")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("author not confirmed", r.stderr)


class SensitiveScanTests(unittest.TestCase):
    """RGPD Art. 9 screen, and the false-positive bound that keeps it usable."""

    def test_sensitive_scan_flags_art9_and_passes_effect_only_phrasing(self) -> None:
        bad = "She missed the March deadline while being treated for depression."
        r = run(SENS, "-", stdin=bad)
        self.assertEqual(r.returncode, 1, "an Art. 9 health disclosure must block")
        self.assertIn("health", r.stderr)

        # The replacement the skill prescribes must be publishable, or the
        # blocker is unclearable and gets ignored.
        good = ("Unavailable 12-26 March; reason known to the operator and not "
                "recorded here. The March deadline moved as a result.")
        r = run(SENS, "-", stdin=good)
        self.assertEqual(r.returncode, 0, f"prescribed phrasing must pass: {r.stderr}")
        self.assertIn("CLEAN", r.stdout)

    def test_ordinary_evaluation_prose_is_clean(self) -> None:
        ok = ("She delivered 4 of 7 pipeline updates in the window. Response "
              "latency averaged 2.3 days against an expectation of same-week. "
              "Counter-evidence: two updates were sent to a channel not searched.")
        r = run(SENS, "-", stdin=ok)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_finance_and_family_categories_flag(self) -> None:
        for text, cat in (
            ("He mentioned his divorce during the January call.", "family-situation"),
            ("She said she was in debt and needed the invoice paid.", "finances"),
        ):
            r = run(SENS, "-", stdin=text)
            self.assertEqual(r.returncode, 1, f"{cat} must block: {text}")
            self.assertIn(cat, r.stderr)

    def test_json_output_is_machine_readable(self) -> None:
        r = run(SENS, "-", "--json", stdin="diagnosed with a chronic illness")
        payload = json.loads(r.stdout)
        self.assertFalse(payload["clean"])
        self.assertTrue(any(h["category"] == "health" for h in payload["hits"]))


class TemplateTests(unittest.TestCase):
    """Structural guarantees of the standardized page template."""

    TPL = HERE.parent / "references" / "evaluation_page_template.html"

    def test_template_exists_and_has_all_eight_sections(self) -> None:
        body = self.TPL.read_text()
        for marker in ("1. HEADER", "2. KEY TAKEAWAYS", "3. OVERVIEW",
                       "4. EXPECTATIONS", "5. FINDINGS",
                       "6. OPERATOR IMPRESSIONS TESTED",
                       "7. METHOD AND COVERAGE", "8. FOOTER"):
            self.assertIn(marker, body, f"template missing section: {marker}")

    def test_template_has_no_cross_links(self) -> None:
        """SKILL.md 0.3: a named-person evaluation is standalone.

        RED if the template ships with an /entities/ link or a /markdown link --
        both leak under a guest token that is not scope-enforced.
        """
        body = self.TPL.read_text()
        self.assertNotIn("/entities/", body)
        self.assertNotIn("/markdown", body)

    def test_template_carries_mandatory_audience_and_rgpd_lines(self) -> None:
        # Collapse whitespace: the template hard-wraps prose, so a two-word
        # phrase can straddle a newline.
        body = " ".join(self.TPL.read_text().split())
        self.assertIn("AUDIENCE_LINE", body)
        self.assertIn("bearer credential", body)
        self.assertIn("Art. 6(1)(f)", body)

    def test_template_has_no_script_tags(self) -> None:
        """Scripts never run in the sandboxed iframe; shipping one is a lie."""
        self.assertNotIn("<script", self.TPL.read_text().lower())

    def test_template_counter_evidence_is_present_in_every_finding(self) -> None:
        """Counter-evidence is a mandatory field on every finding block.

        Count only RENDERED occurrences -- guidance lives in HTML comments and
        must not be able to satisfy the assertion. RED if a finding block ships
        without the field, which is the cheapest guard the skill has against
        motivated reading.
        """
        import re
        rendered = re.sub(r"<!--.*?-->", "", self.TPL.read_text(), flags=re.S)
        self.assertGreater(rendered.count('class="finding"'), 0)
        self.assertEqual(
            rendered.count('class="finding"'),
            rendered.count("Counter-evidence"),
            "every finding block must carry a Counter-evidence field",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
