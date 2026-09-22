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
INST = HERE / "instance_check.py"
RECIP = HERE / "reciprocity_check.py"


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

    RUN = "run-01"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "log.json")
        self.addCleanup(self.tmp.cleanup)
        self._open_run()

    def _open_run(self, run_id: str | None = None, *, population: str | None = None,
                  anchors: tuple[tuple[str, str], ...] = (("ent_abc seen manually", "3"),),
                  ) -> subprocess.CompletedProcess:
        """Open an instrument run with a same-run anchor block (SKILL.md 3.1)."""
        args = ["run", "--path", self.path, "--run-id", run_id or self.RUN]
        if population is not None:
            args += ["--population", population]
        for case, count in anchors:
            args += ["--anchor", case, "--anchor-count", count]
        return run(INSTR, *args)

    def test_zero_without_anchor_is_rejected(self) -> None:
        run(INSTR, "add", "--path", self.path, "--metric", "deliverables in Neotoma",
            "--query", "retrieve_entities deliverable", "--count", "0",
            "--run-id", self.RUN,
            "--anchor", "known deliverable ent_abc", "--anchor-result", "fail")
        r = run(INSTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("measurement of the instrument", r.stderr)

    def test_zero_with_anchor_but_no_hazard_is_rejected(self) -> None:
        run(INSTR, "add", "--path", self.path, "--metric", "deliverables",
            "--query", "retrieve_entities deliverable", "--count", "0",
            "--run-id", self.RUN,
            "--anchor", "ent_abc returns", "--anchor-result", "pass")
        r = run(INSTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no hazard ruled out", r.stderr)

    def test_anchored_zero_with_hazard_passes(self) -> None:
        run(INSTR, "add", "--path", self.path, "--metric", "deliverables",
            "--query", "retrieve_entities deliverable", "--count", "0",
            "--run-id", self.RUN,
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
             sourcing: str = "first-hand-documented",
             corroborated_by: str | None = None,
             authored_by: str | None = None,
             relay_evidence: str | None = None,
             sourcing_disclosed: bool = False,
             convention: str | None = "other items name the person owed",
             distinguisher: str | None = "her status note restates them as hers") -> None:
        args = [
            "add", "--path", self.path, "--citation", citation,
            "--author-confirmed-by", "file owner metadata",
            "--genre", genre, "--obligation-direction", direction,
            "--sourcing", sourcing,
            "--opposite-reading", "a complaint that the operator is late",
            "--initial-reading", initial, "--final-reading", final,
        ]
        if convention:
            args += ["--convention", convention]
        if distinguisher:
            args += ["--distinguisher", distinguisher]
        if corroborated_by:
            args += ["--corroborated-by", corroborated_by]
        if authored_by:
            args += ["--authored-by", authored_by]
        if relay_evidence:
            args += ["--relay-evidence", relay_evidence]
        if sourcing_disclosed:
            args += ["--sourcing-disclosed"]
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
            "--sourcing", "first-hand-documented",
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


# ===================================================================
# The six HIGH-severity defects found by the 2026-09-22 dogfood run.
# Each test below was verified RED by mutating the new guard; what the
# mutation was, and what it let through, is recorded on each test.
# ===================================================================


class InstanceCheckTests(unittest.TestCase):
    """D13 -- the write-target check must be behavioural, not nominal.

    The old 0.1 test was "name the MCP tool prefix". In the dogfood run
    `mcp__mcpsrv_neotoma__*` WAS the operator's prefix and still resolved to a
    local SQLite db holding 6 rendered pages while hosted prod held 285.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "instance.json")
        self.addCleanup(self.tmp.cleanup)

    def _assert(self, probe: dict, origin: str = "https://neotoma.markmhendrickson.com"):
        return run(INST, "assert", "--path", self.path,
                   "--prefix", "mcp__mcpsrv_neotoma__",
                   "--intended-origin", origin, "--probe-json", "-",
                   stdin=json.dumps(probe))

    def test_local_sqlite_behind_the_right_prefix_is_a_hard_stop(self) -> None:
        """THE dogfood failure verbatim: correct prefix, wrong store.

        RED when the check trusts the prefix, the server name or NEOTOMA_ENV
        instead of the probe: this exact payload passes and the evaluation is
        written to a local database the operator will never look at.
        """
        r = self._assert({
            "user_id": "00000000-0000-0000-0000-000000000000",
            "storage": {"storage_backend": "local",
                        "sqlite_db": "/Users/markmhendrickson/data/neotoma.db"},
        })
        self.assertEqual(r.returncode, 1, "a local SQLite store must abort the run")
        self.assertIn("MISMATCH", r.stderr)
        self.assertIn("HARD STOP", r.stderr)
        self.assertIn("Do not write", r.stderr)

    def test_localhost_fallback_origin_is_a_hard_stop(self) -> None:
        """The proxy fell back to DEFAULT_BASE_URL = http://localhost:3080."""
        r = self._assert({"storage": {"storage_backend": "hosted"},
                          "base_url": "http://localhost:3080"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("MISMATCH", r.stderr)

    def test_a_different_hosted_instance_is_a_hard_stop(self) -> None:
        """Writing to a CLIENT instance is the 0.1 boundary violation."""
        r = self._assert({"storage": {"storage_backend": "hosted"},
                          "base_url": "https://client.example.com"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("client.example.com", r.stderr)

    def test_unreadable_probe_is_a_mismatch_not_a_pass(self) -> None:
        """An unreadable answer is not a passing answer.

        RED when the check fails OPEN on a probe it cannot parse -- that would
        restore the defect exactly: a check that cannot tell hosted from local.
        """
        r = self._assert({"user_id": "abc", "unrelated": True})
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot establish which store", r.stderr)

        r = run(INST, "assert", "--path", self.path, "--prefix", "p",
                "--intended-origin", "https://neotoma.example.com",
                "--probe-json", "-", stdin="not json at all")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not JSON", r.stderr)

    def test_intended_hosted_instance_confirms(self) -> None:
        r = self._assert({"storage": {"storage_backend": "hosted"},
                          "base_url": "https://neotoma.markmhendrickson.com"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WRITE INSTANCE CONFIRMED", r.stdout)

        summary = run(INST, "summary", "--path", self.path)
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assertIn("neotoma.markmhendrickson.com", summary.stdout)

    def test_summary_without_an_assertion_fails(self) -> None:
        """A run that never probed cannot claim it wrote to the right place."""
        r = run(INST, "summary", "--path", str(Path(self.tmp.name) / "absent.json"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("NO INSTANCE ASSERTION RECORDED", r.stderr)


class SameRunAnchorTests(unittest.TestCase):
    """D16 -- the anchor must come back non-zero IN THE SAME EXECUTION.

    Four background scans ran in the dogfood; two were broken and failed in
    OPPOSITE directions. The rule is a guard on every number, not just zeros:
    a confident wrong non-zero is more likely to be believed than a zero.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "log.json")
        self.addCleanup(self.tmp.cleanup)

    def _run(self, run_id: str, *anchors: tuple[str, str], population: str | None = None):
        args = ["run", "--path", self.path, "--run-id", run_id]
        if population is not None:
            args += ["--population", population]
        for case, count in anchors:
            args += ["--anchor", case, "--anchor-count", count]
        return run(INSTR, *args)

    def _count(self, metric: str, count: str, run_id: str | None, *extra: str):
        args = ["add", "--path", self.path, "--metric", metric,
                "--query", "grep -c name corpus/", "--count", count,
                "--anchor", "DeGannes in transcript 2026-08-14",
                "--anchor-result", "pass", *extra]
        if run_id:
            args += ["--run-id", run_id]
        return run(INSTR, *args)

    def test_all_anchors_zero_in_the_same_run_voids_the_run(self) -> None:
        """Broken run #1: input file list deleted mid-flight.

        It reported `degannes -> 0`, which was the RIGHT ANSWER on ZERO
        EVIDENCE. The only signal separating it from a true negative was that
        the anchor names collapsed to 0 in the same execution.

        RED when the log accepts a run whose anchors all came back zero: the
        negative finding reads as confirmed while resting on nothing.
        """
        self._run("scan-broken", ("DeGannes", "0"), ("Manju", "0"))
        self._count("degannes mentions", "0", "scan-broken",
                    "--hazard", "stale-tracker")
        r = run(INSTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1, "a run with no live anchor must not validate")
        self.assertIn("every known-positive anchor came back zero", r.stderr)
        self.assertIn("measurement of the instrument", r.stderr)

    def test_anchor_exceeding_population_is_structurally_impossible(self) -> None:
        """Broken run #2: `grep -lic | wc -l` counts files, not matches.

        It reported a name present in all 414 files searched, and another 513
        times against 414 files. The tell is not that a row looked wrong -- it
        is that a match count CANNOT exceed the population searched.

        RED when the log has no population field or does not compare against
        it: the run's plausible-looking non-zero rows ship as fact. Note this
        run's anchors are non-zero, so the zero-collapse check above does not
        catch it -- the two guards are independent.
        """
        self._run("scan-inflated", ("Manju", "513"), ("DeGannes", "414"),
                  population="414")
        self._count("manju mentions", "513", "scan-inflated")
        r = run(INSTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot exceed the population", r.stderr)
        self.assertIn("including the plausible ones", r.stderr)

    def test_nonzero_count_also_requires_a_same_run_anchor(self) -> None:
        """The rule is symmetric -- D16's addendum.

        RED when same-run anchoring is enforced only on zeros: a fabricated
        non-zero ships unchallenged, and a confident wrong non-zero is MORE
        likely to be believed than a zero.
        """
        r = self._count("slack messages from subject", "312", None)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run(INSTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1, "a non-zero needs a same-run anchor too")
        self.assertIn("no run_id", r.stderr)

    def test_count_citing_an_unopened_run_is_rejected(self) -> None:
        """An anchor proven once and cited later proves nothing about this run."""
        self._count("mentions", "12", "never-opened")
        r = run(INSTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no anchor block", r.stderr)

    def test_a_run_needs_at_least_one_anchor(self) -> None:
        r = run(INSTR, "run", "--path", self.path, "--run-id", "bare")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("at least one known-positive anchor", r.stderr)

    def test_valid_run_with_live_anchors_passes(self) -> None:
        """The good run: six anchors non-zero, counts within population."""
        self._run("scan-valid", ("DeGannes", "3"), ("Manju", "27"), population="256")
        self._count("degannes mentions", "0", "scan-valid",
                    "--hazard", "field-name-variant")
        r = run(INSTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("INSTRUMENTS OK", r.stdout)
        self.assertIn("anchored run", r.stdout)


class RelayedMachineOutputTests(unittest.TestCase):
    """D5 -- the subject SENT it; an agent AUTHORED it.

    4.2 q1 says confirm authorship from metadata "not inferred from content".
    Followed literally that gives the WRONG answer for a Slack message whose
    author field says the subject and whose text is verbatim agent output she
    flags one message later.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "attr.json")
        self.addCleanup(self.tmp.cleanup)

    def _add(self, **kw):
        args = [
            "add", "--path", self.path,
            "--citation", kw.get("citation", "slack D0BE 2026-09-11 11:55"),
            "--author-confirmed-by", "slack message author field",
            "--genre", kw.get("genre", "relayed-machine-output"),
            "--obligation-direction", kw.get("direction", "unresolved"),
            "--sourcing", kw.get("sourcing", "unsourced"),
            "--opposite-reading", "her own technical diagnosis",
            "--initial-reading", kw.get("initial", "subject-at-fault"),
            "--final-reading", kw.get("final", "ambiguous"),
            "--distinguisher", "the agent transcript she pasted from",
        ]
        if kw.get("relay_evidence", "she flags it one message later") is not None:
            args += ["--relay-evidence", kw.get("relay_evidence",
                                                "she flags it one message later")]
        if kw.get("authored_by"):
            args += ["--authored-by", kw["authored_by"]]
        return run(ATTR, *args)

    def test_the_genre_exists_at_all(self) -> None:
        """RED before the genre is added: argparse rejects it outright, so the
        only available bucket is `record-of-others`, whose own docstring frames
        it as a HUMAN third party."""
        r = self._add()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("relayed-machine-output", r.stdout)

    def test_relayed_output_cannot_be_read_as_the_subjects_fault(self) -> None:
        """Crediting her with analysis she did not perform is a misattribution
        in exactly the way reading her to-do list as a complaint was.

        RED when the genre is cosmetic -- present in the vocabulary but
        carrying no rule: the agent's words score against her.
        """
        r = self._add(final="subject-at-fault", direction="unresolved")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("she sent it, an agent wrote it", r.stderr)

    def test_relayed_output_is_not_a_commitment_she_made(self) -> None:
        """An agent's output in her channel is not her promise."""
        self._add(direction="subject-owes-operator", final="no-fault")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not her promise", r.stderr)

    def test_relay_claim_must_name_its_evidence(self) -> None:
        """Metadata says who SENT it and cannot say who AUTHORED it, so the
        machine-authorship claim needs its own evidence -- otherwise the genre
        becomes an escape hatch for any inconvenient citation."""
        self._add(relay_evidence=None)
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no relay evidence", r.stderr)

    def test_properly_recorded_relay_passes(self) -> None:
        self._add(authored_by="her coding agent")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)


class SourcingReliabilityTests(unittest.TestCase):
    """D8 -- genre reliability is not sourcing reliability.

    "PROPOSAL AND NDA SIGNED" sits in a WRITTEN sheet, so the ASR
    mistranscription hazard does not apply and all four 4.2 questions pass. It
    is still one person's unrecorded say-so, and the source record grades it so.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "attr.json")
        self.addCleanup(self.tmp.cleanup)

    def _add(self, *, sourcing: str, final: str = "subject-at-fault",
             disclosed: bool = False, corroborated_by: str | None = None,
             citation: str = "Pipeline_Tracker_Sep18: 'PROPOSAL AND NDA SIGNED'"):
        args = [
            "add", "--path", self.path, "--citation", citation,
            "--author-confirmed-by", "google sheet revision history",
            "--genre", "self-accounting",
            "--obligation-direction", "subject-owes-operator",
            "--sourcing", sourcing,
            "--convention", "other rows name the person owed",
            "--opposite-reading", "the NDA is unsigned and the row is aspirational",
            "--distinguisher", "a countersigned document on the instance",
            "--initial-reading", "no-fault", "--final-reading", final,
        ]
        if disclosed:
            args += ["--sourcing-disclosed"]
        if corroborated_by:
            args += ["--corroborated-by", corroborated_by]
        return run(ATTR, *args)

    def test_undisclosed_single_verbal_report_is_rejected(self) -> None:
        """THE case: passes every authorship check, high-trust format, and is
        still uncorroborated say-so.

        RED when sourcing is not tracked separately from genre: the citation
        passes all of 4.2 and "the client signed an NDA" enters a performance
        review on one person's word.
        """
        self._add(sourcing="single-verbal-report")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("single verbal report", r.stderr)
        self.assertIn("reliable record of WHAT WAS SAID", r.stderr)

    def test_disclosed_single_verbal_report_is_allowed(self) -> None:
        """The limitation stated in the finding is the whole remedy -- the
        citation is usable, it just may not pass as corroborated."""
        self._add(sourcing="single-verbal-report", disclosed=True)
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_corroborated_must_name_what_corroborates_it(self) -> None:
        """Otherwise 'corroborated' is a word you can type to clear the gate.

        RED when the grade is accepted bare: every weak citation gets upgraded
        by assertion.
        """
        self._add(sourcing="corroborated")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing named as corroborating it", r.stderr)

        self.setUp()
        self._add(sourcing="corroborated",
                  corroborated_by="countersigned PDF ent_4f2f in the instance")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_sourcing_is_mandatory(self) -> None:
        r = run(ATTR, "add", "--path", self.path, "--citation", "x",
                "--author-confirmed-by", "meta", "--genre", "to-operator",
                "--obligation-direction", "subject-owes-operator",
                "--opposite-reading", "y", "--initial-reading", "no-fault",
                "--final-reading", "no-fault")
        self.assertNotEqual(r.returncode, 0, "a citation without a sourcing grade must not record")

    def test_a_row_with_no_sourcing_grade_is_rejected_at_check(self) -> None:
        """argparse `choices` guards the WRITE path only.

        A log written before this field existed -- an in-flight upgrade, a
        hand-edited file, a row produced by an older copy of the script -- has
        no `sourcing` key at all, and would otherwise sail through `check` with
        the D8 guard silently absent. Enumerate every entrance to a guarded
        action: testing the CLI path proves nothing about the others.

        RED when `check` only validates rows the CLI wrote: this file passes
        and an ungraded citation carries a finding.
        """
        Path(self.path).write_text(json.dumps({
            "created_at": "2026-09-22T00:00:00+00:00",
            "citations": [{
                "citation": "row written before the sourcing field existed",
                "author_confirmed_by": "google sheet revision history",
                "genre": "to-operator",
                "obligation_direction": "subject-owes-operator",
                "opposite_reading": "x",
                "initial_reading": "no-fault",
                "final_reading": "no-fault",
            }],
        }))
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("sourcing missing or unknown", r.stderr)

    def test_first_hand_documented_needs_no_disclosure(self) -> None:
        self._add(sourcing="first-hand-documented")
        r = run(ATTR, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)


class ReciprocityTests(unittest.TestCase):
    """D14 -- nothing swept for what the SUBJECT was owed.

    The one gap no existing guard catches: 4.3's tripwire counts corrections to
    SUBJECT-AUTHORED citations only, so a one-sided expectation table never
    trips it. A literal run would have been materially harsher AND passed every
    check in the skill.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "recip.json")
        self.addCleanup(self.tmp.cleanup)

    def _owed(self, item: str, *, expectation: str = "E8", delivered: str = "no",
              owed_by: str = "CEO", blocking: str = "four named leads"):
        return run(RECIP, "owed", "--path", self.path, "--expectation", expectation,
                   "--item", item, "--owed-by", owed_by,
                   "--requested-on", "2026-08-05", "--in-force-from", "2026-08-05",
                   "--delivered", delivered, "--blocking", blocking)

    def _finding(self, *, expectation: str = "E8", verdict: str = "not met",
                 items: str | None = None, accounted: str | None = None,
                 nothing_owed: str | None = None):
        args = [
            "finding", "--path", self.path, "--expectation", expectation,
            "--verdict", verdict,
        ]
        if items:
            args += ["--counterpart-items", items]
        if accounted:
            args += ["--accounted", accounted]
        if nothing_owed:
            args += ["--nothing-owed", nothing_owed]
        return run(RECIP, *args)

    def test_shortfall_verdict_with_no_counterpart_account_is_rejected(self) -> None:
        """THE defect: a `not met` that never asked what she was owed.

        RED when the gate does not exist: the harsher evaluation renders and
        passes every other check in the skill, including the direction
        tripwire, which structurally cannot see a one-sided table.
        """
        self._finding()
        r = run(RECIP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no counterpart account", r.stderr)
        self.assertIn("case for the prosecution", r.stderr)

    def test_undelivered_obligation_in_window_must_be_named_by_the_finding(self) -> None:
        """The reference price range: owed by the CEO, open ~6 weeks, gating
        four named leads. Recording it and then not mentioning it in the
        verdict is the failure with extra steps.

        RED when the gate only checks that SOME account exists: a run can
        record the counterpart row, cite an unrelated one, and still score her
        as if the gating input had arrived.
        """
        self._owed("reference price range")
        self._owed("website sign-off", owed_by="operator and CEO",
                   blocking="her outreach")
        self._finding(items="website sign-off",
                      accounted="verdict held: outreach resumed 20 Aug")
        r = run(RECIP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("reference price range", r.stderr)
        self.assertIn("does not name it", r.stderr)

    def test_counterpart_item_must_have_an_owed_row(self) -> None:
        """Naming a dependency you never recorded is an assertion, not a sweep."""
        self._finding(items="a thing nobody logged", accounted="handled")
        r = run(RECIP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no `owed` row", r.stderr)

    def test_named_items_need_the_verdict_to_account_for_them(self) -> None:
        self._owed("reference price range")
        self._finding(items="reference price range")
        r = run(RECIP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not say how the verdict accounts", r.stderr)

    def test_nothing_owed_must_name_the_surfaces_swept(self) -> None:
        """"Nothing was owed" is a claim about your sweep before it is a claim
        about the operator (SKILL.md 2.3)."""
        self._finding(nothing_owed="nothing was outstanding")
        r = run(RECIP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("name the surfaces swept", r.stderr)

    def test_nothing_owed_with_surfaces_passes(self) -> None:
        """A legitimate one-sided window -- allowed, but only when stated."""
        self._finding(nothing_owed=("swept Slack, Gmail and the tracker for open "
                                    "asks to the operator in this window; none found"))
        r = run(RECIP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_met_verdict_needs_no_counterpart_account(self) -> None:
        """`met` and `insufficient evidence` assert no shortfall, so the gate
        must not fire on them -- a gate that blocks legitimate runs gets
        switched off, which is worse than no gate."""
        self._finding(verdict="met")
        self._finding(expectation="E3", verdict="insufficient evidence")
        r = run(RECIP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_fully_accounted_shortfall_passes(self) -> None:
        self._owed("reference price range")
        self._finding(items="reference price range",
                      accounted=("verdict downgraded to partially met: the gating "
                                 "input never arrived in the window"))
        r = run(RECIP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RECIPROCITY OK", r.stdout)

    def test_contradictory_account_is_rejected(self) -> None:
        self._owed("reference price range")
        self._finding(items="reference price range", accounted="x",
                      nothing_owed="swept everything; none found")
        r = run(RECIP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("contradict", r.stderr)

    def test_empty_findings_are_rejected(self) -> None:
        r = run(RECIP, "check", "--path", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no findings recorded", r.stderr)


class EvaluationSchemaTests(unittest.TestCase):
    """D11 -- 5.2 mandated storing an `evaluation` entity with no schema.

    `describe_entity_type('evaluation')` errors; /store accepts undeclared
    fields, routes them to raw_fragments, and they read back empty -- the exact
    bug 3.2 names first. `strict: true` governs MATCHING, not declaration.
    """

    SCHEMA = HERE.parent / "references" / "evaluation_schema.json"

    def test_schema_file_ships_and_is_valid(self) -> None:
        """RED before the schema exists: the skill mandates a write against an
        unregistered type, so the first run on any instance silently discards
        its own payload."""
        self.assertTrue(self.SCHEMA.exists(), "5.2 mandates a store; ship the schema")
        payload = json.loads(self.SCHEMA.read_text())
        self.assertEqual(payload["entity_type"], "evaluation")
        self.assertIn("fields", payload["schema_definition"])

    def test_schema_declares_every_field_the_skill_writes(self) -> None:
        """RED when a field the skill instructs you to store is undeclared --
        that field reads back empty and nothing reports it."""
        fields = json.loads(self.SCHEMA.read_text())["schema_definition"]["fields"]
        for required in ("title", "subject_entity_id", "audience", "expectations",
                         "findings", "owed_to_subject", "operator_impressions",
                         "sweep_manifest", "instrument_log", "attribution_check",
                         "write_instance", "revision_log"):
            self.assertIn(required, fields, f"schema missing field: {required}")

    def test_schema_carries_the_counterpart_field(self) -> None:
        """D14's table must survive the store, or the gate is cosmetic."""
        fields = json.loads(self.SCHEMA.read_text())["schema_definition"]["fields"]
        self.assertIn("owed_to_subject", fields)

    def test_skill_requires_register_and_readback(self) -> None:
        """RED when 5.2 still says only "store" -- the instruction that
        triggers the bug the skill itself warns about two paragraphs down."""
        body = " ".join((HERE.parent / "SKILL.md").read_text().split())
        self.assertIn("describe_entity_type('evaluation')", body)
        self.assertIn("references/evaluation_schema.json", body)
        self.assertIn("read back", body.lower())


class SkillProseTests(unittest.TestCase):
    """The six fixes must be stated in SKILL.md, not only in the scripts.

    A guard the prose does not describe is one an agent routes around: the
    scripts are invoked BY the skill text, so a script nothing tells you to run
    is not a control (CLAUDE.md: "a mechanism that does not bind is not a
    control").
    """

    BODY = " ".join((HERE.parent / "SKILL.md").read_text().split())

    def test_instance_check_is_behavioural_and_aborts(self) -> None:
        self.assertIn("instance_check.py", self.BODY)
        self.assertIn("the run aborts", self.BODY)
        self.assertNotIn("the tool prefix is the only thing distinguishing them",
                         self.BODY)

    def test_relayed_machine_output_genre_is_documented(self) -> None:
        self.assertIn("relayed machine output", self.BODY.lower())
        self.assertIn("metadata cannot answer it", self.BODY.lower())

    def test_sourcing_axis_is_documented(self) -> None:
        self.assertIn("single-verbal-report", self.BODY)
        self.assertIn("reliable record of WHAT WAS SAID", self.BODY)

    def test_counterpart_table_is_documented_and_gated(self) -> None:
        self.assertIn("What the subject was owed", self.BODY)
        self.assertIn("reciprocity_check.py", self.BODY)
        self.assertIn("counterpart_account", self.BODY)

    def test_same_run_anchor_rule_is_documented(self) -> None:
        self.assertIn("same invocation", self.BODY.lower())
        self.assertIn("population", self.BODY.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
