"""Tests for check_pr_design_basis.py (the CI design-basis line check).

Each case fails if the check is reverted to "always pass": see the PR body for
what each looked like red.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import check_pr_design_basis as cpdb  # noqa: E402

CODE = ["execution/scripts/foo.py"]
GOOD = "Design basis: docs/foundation/principles.md#1-a-mechanism-that-does-not-bind-is-not-a-control"


class DesignBasisLine(unittest.TestCase):
    def ok(self, body, changed=CODE):
        return cpdb.evaluate(body, changed)

    def test_present_citation_passes(self):
        ok, why = self.ok(f"Summary\n\n{GOOD}\n")
        self.assertTrue(ok, why)

    def test_missing_line_fails(self):
        ok, why = self.ok("Summary only, no basis.\n")
        self.assertFalse(ok)
        self.assertIn("MISSING", why)

    def test_empty_body_fails(self):
        self.assertFalse(self.ok(None)[0])
        self.assertFalse(self.ok("")[0])

    def test_empty_value_fails(self):
        ok, why = self.ok("Design basis:\n\nTask: x\n")
        self.assertFalse(ok)
        self.assertIn("empty", why)

    def test_whitespace_only_value_fails(self):
        self.assertFalse(self.ok("Design basis:    \n")[0])

    def test_citation_of_missing_document_is_invalid(self):
        ok, why = self.ok("Design basis: docs/foundation/does_not_exist.md#x\n")
        self.assertFalse(ok)
        self.assertIn("INVALID", why)

    def test_unfilled_template_placeholder_fails(self):
        ok, _ = self.ok("Design basis: docs/foundation/<doc>.md#<section>\n")
        self.assertFalse(ok)

    def test_sanctioned_no_design_form_passes(self):
        for sep in ("—", "--", "-", ":"):
            body = f"Design basis: no design applies {sep} CI wiring for an existing check\n"
            ok, why = self.ok(body)
            self.assertTrue(ok, f"{sep!r}: {why}")

    def test_no_design_without_reason_fails(self):
        for body in (
            "Design basis: no design applies\n",
            "Design basis: no design applies —\n",
            "Design basis: No Design Applies.\n",
        ):
            ok, why = self.ok(body)
            self.assertFalse(ok, body)
            self.assertIn("reason", why)

    def test_unrelated_text_is_invalid(self):
        self.assertFalse(self.ok("Design basis: see the issue\n")[0])

    def test_markdown_decorations_are_accepted(self):
        for line in (f"- {GOOD}", f"**Design basis:** {GOOD[len('Design basis: '):]}",
                     f"> {GOOD}", "design basis: " + GOOD[len("Design basis: "):]):
            self.assertTrue(self.ok(line + "\n")[0], line)

    def test_template_comment_and_code_fence_do_not_count(self):
        body = (
            "<!--\nDesign basis: docs/foundation/principles.md\n-->\n"
            "```\nDesign basis: docs/foundation/principles.md\n```\n"
        )
        ok, why = self.ok(body)
        self.assertFalse(ok)
        self.assertIn("MISSING", why)

    def test_one_valid_line_among_several_passes(self):
        self.assertTrue(self.ok(f"Design basis:\n{GOOD}\n")[0])


class Exemption(unittest.TestCase):
    def test_docs_only_is_exempt_without_a_line(self):
        ok, why = cpdb.evaluate("", ["docs/dev/guide.md", "docs/infrastructure/x.md"])
        self.assertTrue(ok)
        self.assertIn("exempt", why)

    def test_generated_only_is_exempt(self):
        files = [
            "docs/taxonomy.md",
            "docs/phases.md",
            "docs/architecture.md",
            "docs/foundation/decision_state.md",
            "docs/foundation/projection/adapters.md",
            "docs/agents/pavo.md",
            ".claude/skills/pavo/SKILL.md",
        ]
        self.assertTrue(cpdb.evaluate("", files)[0])

    def test_one_code_file_removes_the_exemption(self):
        ok, why = cpdb.evaluate("", ["docs/dev/guide.md", "execution/scripts/a.py"])
        self.assertFalse(ok)
        self.assertIn("MISSING", why)

    def test_hand_authored_foundation_doc_is_not_exempt(self):
        self.assertFalse(cpdb.evaluate("", ["docs/foundation/principles.md"])[0])

    def test_non_markdown_under_docs_is_not_exempt(self):
        self.assertFalse(cpdb.evaluate("", ["docs/dev/tool.py"])[0])

    def test_nested_agent_or_skill_paths_are_not_generated(self):
        self.assertFalse(cpdb.evaluate("", [".claude/skills/pavo/extra/SKILL.md"])[0])
        self.assertFalse(cpdb.evaluate("", [".claude/skills/pavo/helper.py"])[0])

    def test_empty_file_list_fails_closed(self):
        self.assertFalse(cpdb.evaluate("", [])[0])
        self.assertFalse(cpdb.evaluate("", ["", "  "])[0])


class Cli(unittest.TestCase):
    def run_cli(self, body, files):
        with tempfile.TemporaryDirectory() as d:
            ev = Path(d, "event.json")
            ev.write_text(json.dumps({"pull_request": {"body": body}}))
            cf = Path(d, "files.txt")
            cf.write_text("\n".join(files))
            return subprocess.run(
                [sys.executable, str(HERE / "check_pr_design_basis.py"),
                 "--event", str(ev), "--changed-files", str(cf)],
                capture_output=True, text=True,
            )

    def test_exit_codes(self):
        self.assertEqual(self.run_cli(GOOD, CODE).returncode, 0)
        self.assertEqual(self.run_cli("nothing", CODE).returncode, 1)
        self.assertEqual(self.run_cli(None, CODE).returncode, 1)
        self.assertEqual(self.run_cli("", ["docs/dev/a.md"]).returncode, 0)

    def test_unreadable_inputs_are_not_a_pass(self):
        r = subprocess.run(
            [sys.executable, str(HERE / "check_pr_design_basis.py"),
             "--event", "/nonexistent/e.json", "--changed-files", "/nonexistent/f"],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 2)


class Wiring(unittest.TestCase):
    """The workflow binds: right triggers, no soft-fail, calls the script."""

    raw = (REPO / ".github/workflows/pr-design-basis.yml").read_text()
    # Comments explain the rules and may name them; only live YAML binds.
    text = "\n".join(
        ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")
    )

    def test_runs_on_the_four_pr_events(self):
        m = re.search(r"types:\s*\[([^\]]*)\]", self.text)
        events = {t.strip() for t in m.group(1).split(",")}
        self.assertEqual(events, {"opened", "edited", "synchronize", "reopened"})
        self.assertNotIn("pull_request_target", self.text)

    def test_cannot_soft_fail(self):
        self.assertNotIn("continue-on-error", self.text)
        self.assertNotRegex(self.text, r"\|\|\s*true")

    def test_not_path_filtered(self):
        self.assertNotIn("paths:", self.text)

    def test_reads_body_from_event_payload_and_calls_script(self):
        self.assertIn("--event \"$GITHUB_EVENT_PATH\"", self.text)
        self.assertIn("execution/scripts/check_pr_design_basis.py", self.text)

    def test_template_has_the_field(self):
        tpl = (REPO / ".github/pull_request_template.md").read_text()
        self.assertRegex(tpl, r"(?m)^Design basis:")
        # An unfilled template must itself fail the check.
        self.assertFalse(cpdb.evaluate(tpl, CODE)[0])


if __name__ == "__main__":
    unittest.main()
