"""Offline tests for the GitHub GFM parity gate's comparison and failures."""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_register_render_parity as parity


REGISTER = (
    "## The register of open design decisions\n"
    "| # | The question | Argued in | Blocks | Status |\n"
    "|---|---|---|---|---|\n"
    "| 1 | q | a | b | **open** |\n"
)
REVISION = "## Doc\n| Revision | Pass | Change |\n|---|---|---|\n| 1 | p | c |\n"
REGISTER_HTML = (
    "<h2>The register of open design decisions</h2><table><thead><tr>"
    "<th>#</th><th>Question</th><th>Argued in</th><th>Blocks</th><th>Status</th>"
    "</tr></thead><tbody><tr><td>1</td><td>q</td><td>a</td><td>b</td>"
    "<td><strong>open</strong></td></tr></tbody></table>"
)
REVISION_HTML = (
    "<h2>Doc</h2><table><thead><tr><th>Revision</th><th>Pass</th><th>Change</th>"
    "</tr></thead><tbody><tr><td>1</td><td>p</td><td>c</td></tr></tbody></table>"
)


class TestRegisterRenderParity(unittest.TestCase):
    def test_workflow_binds_the_real_file_check(self):
        workflow = (parity.ROOT / ".github/workflows/foundation-checks.yml").read_text()
        self.assertIn("Register tables — GitHub GFM render parity", workflow)
        self.assertIn(
            "run: python3 execution/scripts/check_register_render_parity.py", workflow
        )
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", workflow)
        self.assertNotIn("continue-on-error: true", workflow)

    def test_matching_tables_and_excluded_outside_section_table(self):
        outside = "<h2>Other</h2><table><tbody><tr><td>x</td></tr></tbody></table>"
        parity.check_parity("register", REGISTER, outside + REGISTER_HTML)
        parity.check_parity("revision", REVISION, REVISION_HTML)

    def test_extra_and_missing_body_rows_fail(self):
        extra = REGISTER_HTML.replace(
            "</tbody>",
            "<tr><td>2</td><td>x</td><td>x</td><td>x</td><td>x</td></tr></tbody>",
        )
        missing = REGISTER_HTML.replace(
            "<tr><td>1</td><td>q</td><td>a</td><td>b</td><td><strong>open</strong></td></tr>",
            "",
        )
        for html in (extra, missing):
            with self.assertRaisesRegex(ValueError, "register row count"):
                parity.check_parity("register", REGISTER, html)

    def test_changed_number_and_dropped_or_extra_cell_fail(self):
        for html, message in (
            (REGISTER_HTML.replace("<td>1</td>", "<td>2</td>"), "number cell"),
            (REGISTER_HTML.replace("<td>b</td>", ""), "cell count"),
            (
                REGISTER_HTML.replace("<td>b</td>", "<td>b</td><td>extra</td>"),
                "cell count",
            ),
        ):
            with self.assertRaisesRegex(ValueError, f"register row 1: {message}"):
                parity.check_parity("register", REGISTER, html)

    def test_extra_table_in_register_section_fails(self):
        extra = REGISTER_HTML + "<table><tbody><tr><td>2</td></tr></tbody></table>"
        with self.assertRaisesRegex(ValueError, "register table count"):
            parity.check_parity("register", REGISTER, extra)

    def test_api_failure_is_blocking_not_a_skip(self):
        with (
            patch.object(parity, "FILES", (Path("docs/foundation/conformance.md"),)),
            patch.dict("os.environ", {"GITHUB_TOKEN": ""}),
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(parity.main(), 1)
        self.assertIn("GITHUB_TOKEN is required", error.getvalue())


if __name__ == "__main__":
    unittest.main()
