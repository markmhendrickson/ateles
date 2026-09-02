#!/usr/bin/env python3
"""Tests for render_plan_page.py — the plan → rendered_page projection.

Run: python3 execution/scripts/test_render_plan_page.py

The property that matters is that the renderer FAILS rather than degrades. A
page that quietly drops a strand is worse than one that fails to publish,
because nobody reloads a page they have already read.
"""

import json
import re
import sys
import unittest
import urllib.error
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_plan_page as rpp  # noqa: E402
from render_plan_page import (  # noqa: E402
    DEFAULT_PLAN_ID,
    MARKER_RE,
    PAGE_CSS,
    PlanShapeError,
    _served_hash,
    _summarize,
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


class TestSummarize(unittest.TestCase):
    """UX review guidance (Accipiter, ateles#735): a truncated cell must be
    visibly incomplete, never read as the full step."""

    def test_multi_sentence_input_ends_with_ellipsis_and_is_shorter(self):
        text = "First sentence here. Second sentence follows. Third trails off."
        summary = _summarize(text)
        self.assertTrue(summary.endswith("…"))
        self.assertLess(len(summary), len(text))

    def test_single_short_sentence_has_no_ellipsis(self):
        summary = _summarize("Just one short sentence.")
        self.assertFalse(summary.endswith("…"))
        self.assertEqual(summary, "Just one short sentence.")

    def test_length_cut_within_one_sentence_also_gets_ellipsis(self):
        long_sentence = "word " * 100 + "."
        summary = _summarize(long_sentence.strip(), limit=50)
        self.assertTrue(summary.endswith("…"))

    def test_empty_text_returns_empty(self):
        self.assertEqual(_summarize(""), "")
        self.assertEqual(_summarize("   "), "")

    def test_abbreviation_period_is_not_a_sentence_boundary(self):
        """A naive split on '. ' truncates 'Mr. Smith...' down to 'Mr…' —
        a sharper silent-omission bug than the one this function fixes."""
        self.assertEqual(
            _summarize("Mr. Smith went to Washington."),
            "Mr. Smith went to Washington.",
        )
        self.assertEqual(
            _summarize("Use e.g. the config file for defaults."),
            "Use e.g. the config file for defaults.",
        )

    def test_abbreviation_at_truncation_point_keeps_its_periods(self):
        """rstrip('.') would turn a trailing 'e.g.' into 'e.g' before the
        ellipsis, corrupting the abbreviation."""
        summary = _summarize("Use e.g. the config file for defaults. Then restart.")
        self.assertTrue(summary.startswith("Use e.g. the config file for defaults"))
        self.assertNotIn("e.g…", summary)

    def test_source_ellipsis_is_not_doubled(self):
        """A step that already trails off with '...' or '….' must not gain a
        second, stacked ellipsis character."""
        summary = _summarize("Do the thing etc... Then more happens next.")
        self.assertNotIn("……", summary)
        self.assertNotIn("...…", summary)


class TestPlanLinkAndHeadings(unittest.TestCase):
    """UX review guidance (Accipiter) + arch guidance (Waxwing), ateles#735."""

    LONG_STEP_STRAND = (
        "STRAND A — LONG STEPS\n\n"
        "1. [SWARM] First sentence is short. But there is a second sentence "
        "that pushes this step past a single clause so it gets truncated "
        "in the row. Blocked by: nothing.\n"
    )

    def _html(self, next_steps=None):
        return build_html(
            {
                "_entity_id": "ent_test_plan_123",
                "title": "Test plan",
                "next_steps": next_steps or TWO_STRANDS,
                "decisions": {"a_settled_thing": "It was decided."},
                "goals": ["Ship it"],
            }
        )

    def test_subtitle_links_the_plan_entity(self):
        html = self._html()
        self.assertIn('href="/entities/ent_test_plan_123"', html)

    def test_plan_link_present_even_when_nothing_truncates(self):
        """Structural, not conditioned on truncation happening to occur."""
        short = "STRAND A — A\n\n1. [SWARM] Short. Blocked by: nothing.\n"
        html = self._html(short)
        self.assertIn('href="/entities/ent_test_plan_123"', html)

    def test_truncated_step_cell_links_onward_to_full_step(self):
        html = self._html(self.LONG_STEP_STRAND)
        # Two links total: the subtitle chrome link plus the per-row "full step".
        self.assertGreaterEqual(html.count('href="/entities/ent_test_plan_123"'), 2)
        self.assertIn("full step", html)

    def test_every_heading_has_id_and_hanchor(self):
        html = self._html()
        heading_re = re.compile(r"<(h[1-6]) id=\"([^\"]+)\">(.*?)</\1>", re.S)
        headings = heading_re.findall(html)
        self.assertGreater(len(headings), 0)
        for _, hid, inner in headings:
            self.assertTrue(hid)
            self.assertIn(f'class="hanchor" href="#{hid}"', inner)

    def test_duplicate_heading_text_gets_distinct_ids(self):
        dup = (
            "STRAND A — REVIEW\n\n1. [SWARM] one. Blocked by: nothing.\n\n"
            "STRAND B — REVIEW\n\n2. [SWARM] two. Blocked by: nothing.\n"
        )
        html = self._html(dup)
        ids = re.findall(r'<h2 id="([^"]+)">', html)
        self.assertEqual(len(ids), len(set(ids)), f"duplicate heading ids: {ids}")

    def test_literal_suffix_text_does_not_collide_with_generated_suffix(self):
        """A heading literally titled with what looks like a de-dupe suffix
        (e.g. a strand titled "REVIEW 2") must not collide with the id a
        second "REVIEW" heading would auto-generate."""
        dup = (
            "STRAND A — REVIEW\n\n1. [SWARM] one. Blocked by: nothing.\n\n"
            "STRAND B — REVIEW 2\n\n2. [SWARM] two. Blocked by: nothing.\n\n"
            "STRAND C — REVIEW\n\n3. [SWARM] three. Blocked by: nothing.\n"
        )
        html = self._html(dup)
        ids = re.findall(r'<h2 id="([^"]+)">', html)
        self.assertEqual(len(ids), len(set(ids)), f"duplicate heading ids: {ids}")

    def test_no_bold_tags_anywhere(self):
        html = self._html()
        self.assertNotIn("<strong>", html)
        self.assertNotIn("<b>", html)

    def test_default_plan_id_used_when_entity_id_absent(self):
        html = build_html(
            {"title": "T", "next_steps": TWO_STRANDS, "decisions": {}, "goals": []}
        )
        self.assertIn(f'href="/entities/{DEFAULT_PLAN_ID}"', html)

    def test_page_css_makes_hanchor_usable(self):
        """The anchors render without CSS but are invisible until hover — this
        is the CSS half of the RENDERED_PAGE_NO_HEADING_ANCHORS contract."""
        self.assertIn("a.hanchor", PAGE_CSS)
        self.assertIn("opacity", PAGE_CSS)


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


def _http_error(code):
    return urllib.error.HTTPError("url", code, "err", {}, None)


class _FakeResponse:
    """Minimal stand-in for the object `urllib.request.urlopen` yields."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestRenderNetworkPath(unittest.TestCase):
    """Exercises render()'s actual /correct + read-back sequence end to end,
    with urlopen mocked. Regression coverage for ateles#735 review guidance:
    render() must push AND verify custom_css, and a failure on the custom_css
    call must be distinguishable from a failure on html_body."""

    PLAN_ID = "ent_test_plan"
    PAGE_ID = "ent_test_page"
    PLAN_SNAPSHOT = {
        "title": "T",
        "next_steps": "STRAND A — A\n\n1. [SWARM] Short. Blocked by: nothing.\n",
        "decisions": {},
        "goals": [],
    }

    def _plan_get(self):
        return _FakeResponse(json.dumps({"snapshot": self.PLAN_SNAPSHOT}).encode())

    def _served_html(self, digest):
        return _FakeResponse(f"<h1>x</h1><!-- page-content-hash: {digest} -->".encode())

    def test_happy_path_verifies_both_html_body_and_custom_css(self):
        served_holder = {}

        def fake_urlopen(req, timeout=30):
            url = req.full_url
            if url.endswith(f"/entities/{self.PLAN_ID}"):
                return self._plan_get()
            if url.endswith("/correct"):
                payload = json.loads(req.data.decode())
                if payload["field"] == "html_body":
                    served_holder["digest"] = MARKER_RE.search(payload["value"]).group(1)
                return _FakeResponse(b'{"created": false}')
            if url.endswith(f"/entities/{self.PAGE_ID}/html"):
                return self._served_html(served_holder["digest"])
            if url.endswith(f"/entities/{self.PAGE_ID}"):
                return _FakeResponse(json.dumps({"snapshot": {"custom_css": PAGE_CSS}}).encode())
            raise AssertionError(f"unexpected URL: {url}")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rc = rpp.render("https://x", "tok", self.PLAN_ID, self.PAGE_ID)
        self.assertEqual(rc, 0)

    def test_custom_css_failure_is_reported_distinctly_from_html_body_failure(self):
        calls = []

        def fake_urlopen(req, timeout=30):
            url = req.full_url
            if url.endswith(f"/entities/{self.PLAN_ID}"):
                return self._plan_get()
            if url.endswith("/correct"):
                payload = json.loads(req.data.decode())
                calls.append(payload["field"])
                if payload["field"] == "custom_css":
                    raise _http_error(500)
                return _FakeResponse(b'{"created": false}')
            raise AssertionError(f"unexpected URL: {url}")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("sys.stderr"):
                rc = rpp.render("https://x", "tok", self.PLAN_ID, self.PAGE_ID)
        self.assertEqual(rc, 1)
        # html_body was attempted (and, in this scenario, landed) before the
        # custom_css call failed — the two must not be conflated.
        self.assertEqual(calls, ["html_body", "custom_css"])

    def test_custom_css_snapshot_mismatch_after_success_fails_verify(self):
        """The correct call can report success while the server silently
        drops the field — read-back must catch that, not just the HTTP
        status."""
        served_holder = {}

        def fake_urlopen(req, timeout=30):
            url = req.full_url
            if url.endswith(f"/entities/{self.PLAN_ID}"):
                return self._plan_get()
            if url.endswith("/correct"):
                payload = json.loads(req.data.decode())
                if payload["field"] == "html_body":
                    served_holder["digest"] = MARKER_RE.search(payload["value"]).group(1)
                return _FakeResponse(b'{"created": false}')
            if url.endswith(f"/entities/{self.PAGE_ID}/html"):
                return self._served_html(served_holder["digest"])
            if url.endswith(f"/entities/{self.PAGE_ID}"):
                # custom_css never actually landed server-side.
                return _FakeResponse(json.dumps({"snapshot": {"custom_css": ""}}).encode())
            raise AssertionError(f"unexpected URL: {url}")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("sys.stderr"):
                rc = rpp.render("https://x", "tok", self.PLAN_ID, self.PAGE_ID)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
