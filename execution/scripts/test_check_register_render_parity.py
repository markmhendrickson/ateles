"""Offline tests for the GitHub GFM parity gate's comparison and failures."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_register_render_parity as parity
import reserve_register_number as reserve


REGISTER = (
    "## The register of open design decisions\n"
    "| # | The question | Argued in | Blocks | Status |\n"
    "|---|---|---|---|---|\n"
    "| 1 | q | a | b | **open** |\n"
)
REVISION = "## Doc\n| Revision | Pass | Change |\n|---|---|---|\n| 1 | p | c |\n"
REGISTER_HTML = (
    "<h2>The register of open design decisions</h2><table><thead><tr>"
    "<th>#</th><th>The question</th><th>Argued in</th><th>Blocks</th><th>Status</th>"
    "</tr></thead><tbody><tr><td>1</td><td>q</td><td>a</td><td>b</td>"
    "<td><strong>open</strong></td></tr></tbody></table>"
)
REVISION_HTML = (
    "<h2>Doc</h2><table><thead><tr><th>Revision</th><th>Pass</th><th>Change</th>"
    "</tr></thead><tbody><tr><td>1</td><td>p</td><td>c</td></tr></tbody></table>"
)


def revision_table(number: int) -> str:
    return (
        "| Revision | Pass | Change |\n|---|---|---|\n"
        f"| {number} | p | c |\n"
    )


def rendered_revision_table(number: int) -> str:
    return (
        "<table><thead><tr><th>Revision</th><th>Pass</th><th>Change</th></tr></thead>"
        f"<tbody><tr><td>{number}</td><td>p</td><td>c</td></tr></tbody></table>"
    )


class TestRegisterRenderParity(unittest.TestCase):
    def test_hidden_heading_between_revision_tables_fails_closed(self):
        html = "<h2>Doc</h2>" + rendered_revision_table(900) * 2
        for hidden in (
            "<!--\n## Spoof\n-->\n",
            "```md\n## Spoof\n```\n",
            "~~~md\n## Spoof\n~~~\n",
            "<div>\n## Spoof\n</div>\n",
            "<div><!-- closed -->\n## Spoof\n</div>\n",
            "<!--\n## \n-->\n",
        ):
            source = "## Doc\n" + revision_table(900) + "\n" + hidden + revision_table(900)
            with self.subTest(hidden=hidden), self.assertRaisesRegex(ValueError, "line .*hidden heading"):
                parity.revision_rows(source)
            with self.subTest(hidden=hidden), self.assertRaisesRegex(ValueError, "line .*hidden heading"):
                reserve.table_counts(source)
            with self.subTest(hidden=hidden), self.assertRaisesRegex(ValueError, "line .*hidden heading"):
                parity.check_parity("revision", source, html)

    def test_hidden_heading_in_decision_register_fails_closed(self):
        source = REGISTER + "\n<!--\n## Spoof\n-->\n" + (
            "| 2 | q | a | b | **open** |\n"
        )
        with self.assertRaisesRegex(ValueError, "hidden heading"):
            parity.decision_rows(source)
        with self.assertRaisesRegex(ValueError, "hidden heading"):
            parity.check_parity("register", source, REGISTER_HTML)

    def test_rendered_heading_identity_must_match_source_on_both_files(self):
        for kind, source, html in (
            ("revision", REVISION, REVISION_HTML.replace("<h2>Doc</h2>", "<h2>Other</h2>")),
            ("register", REGISTER, REGISTER_HTML.replace(
                "<h2>The register of open design decisions</h2>",
                "<h2>Other</h2>")),
        ):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "table 1.*heading.*GitHub"):
                parity.check_parity(kind, source, html)

    def test_indented_and_tab_headings_are_read_as_github_renders_them(self):
        for heading in (" ## Second", "   ## Second", "##\tSecond"):
            source = "## Doc\n" + revision_table(1) + "\n" + heading + "\n" + revision_table(2)
            html = ("<h2>Doc</h2>" + rendered_revision_table(1)
                    + "<h2>Second</h2>" + rendered_revision_table(2))
            with self.subTest(heading=heading):
                self.assertEqual([row.table for row in parity.revision_rows(source)], ["Doc", "Second"])
                parity.check_parity("revision", source, html)

    def test_workflow_binds_the_real_file_check(self):
        workflow = (parity.ROOT / ".github/workflows/foundation-checks.yml").read_text()
        self.assertIn("Register tables — GitHub GFM render parity", workflow)
        self.assertIn(
            "run: python3 execution/scripts/check_register_render_parity.py", workflow
        )
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", workflow)
        self.assertNotIn("continue-on-error: true", workflow)

    def test_matching_tables_and_outside_section_table(self):
        outside = "<h2>Other</h2><table><tbody><tr><td>x</td></tr></tbody></table>"
        source = "## Other\n| Label |\n|-|\n| x |\n" + REGISTER
        parity.check_parity("register", source, outside + REGISTER_HTML)
        parity.check_parity("revision", REVISION, REVISION_HTML)

    def test_extra_table_outside_register_section_fails_total_count(self):
        outside = "<h2>Other</h2><table><tbody><tr><td>x</td></tr></tbody></table>"
        with self.assertRaisesRegex(ValueError, "total table count"):
            parity.check_parity("register", REGISTER, outside + REGISTER_HTML)

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

    def test_changed_body_text_fails_even_with_matching_number_and_width(self):
        html = REGISTER_HTML.replace("<td>b</td>", "<td>different</td>")
        with self.assertRaisesRegex(ValueError, "register row 1: cell 4 text differs"):
            parity.check_parity("register", REGISTER, html)

    def test_changed_status_text_fails_even_with_matching_number_and_width(self):
        html = REGISTER_HTML.replace("<strong>open</strong>", "<strong>ruled</strong>")
        with self.assertRaisesRegex(ValueError, "register row 1: cell 5 text differs"):
            parity.check_parity("register", REGISTER, html)

    def test_header_text_and_revision_body_text_are_compared(self):
        with self.assertRaisesRegex(ValueError, "register header: cell 2 text differs"):
            parity.check_parity(
                "register", REGISTER, REGISTER_HTML.replace("<th>The question</th>", "<th>Question</th>")
            )
        with self.assertRaisesRegex(ValueError, "revision table 1 row 1: cell 2 text differs"):
            parity.check_parity("revision", REVISION, REVISION_HTML.replace("<td>p</td>", "<td>x</td>"))

    def test_visible_text_collapses_whitespace_inside_cells(self):
        parity.check_parity("register", REGISTER, REGISTER_HTML.replace("<td>q</td>", "<td>  q\n </td>"))

    def test_double_backslash_fixture_fails_closed_on_both_tables(self):
        row = json.loads((Path(__file__).parent / "fixtures/gfm_row_fixture.json").read_text())[
            "double_backslash_pipe"
        ]
        register_source = REGISTER.rsplit("| 1 |", 1)[0] + row["register_row"] + "\n"
        revision_source = REVISION.rsplit("| 1 |", 1)[0] + row["revision_row"] + "\n"
        for kind, source, html in (
            ("register", register_source, row["register_html"]),
            ("revision", revision_source, row["revision_html"]),
        ):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "malformed"):
                parity.check_parity(kind, source, html)

    def test_extra_table_in_register_section_fails(self):
        extra = REGISTER_HTML + "<table><tbody><tr><td>2</td></tr></tbody></table>"
        with self.assertRaisesRegex(ValueError, "table count"):
            parity.check_parity("register", REGISTER, extra)

    def test_cli_fallback_uses_existing_login(self):
        with patch.dict("os.environ", {"GITHUB_TOKEN": ""}), patch.object(
            parity.subprocess, "run", return_value=type("Result", (), {"returncode": 0, "stdout": b"<table></table>", "stderr": b""})()
        ) as runner:
            self.assertEqual(parity.render_github("|a|\n|-|\n"), "<table></table>")
        self.assertEqual(runner.call_args.args[0][:3], ["gh", "api", "markdown"])

    def test_transient_http_error_retries_once(self):
        error = HTTPError("https://api.github.com/markdown", 429, "rate limit", {}, None)
        response = type("Response", (), {"read": lambda self: b"<table></table>", "__enter__": lambda self: self, "__exit__": lambda self, *args: None})()
        with patch.dict("os.environ", {"GITHUB_TOKEN": "synthetic"}), patch.object(
            parity.urllib.request, "urlopen", side_effect=[error, response]
        ) as request, patch.object(parity.time, "sleep") as sleep:
            self.assertEqual(parity.render_github("|a|\n|-|\n"), "<table></table>")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once()

    def test_persistent_rate_limit_fails_after_one_retry(self):
        error = HTTPError("https://api.github.com/markdown", 429, "rate limit", {}, None)
        with patch.dict("os.environ", {"GITHUB_TOKEN": "synthetic"}), patch.object(
            parity.urllib.request, "urlopen", side_effect=[error, error]
        ) as request, patch.object(parity.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                parity.render_github("|a|\n|-|\n")
        self.assertEqual(request.call_count, 2)

    def test_cli_network_failure_retries_once(self):
        failed = type("Result", (), {"returncode": 1, "stdout": b"", "stderr": b"Could not resolve host"})()
        good = type("Result", (), {"returncode": 0, "stdout": b"<table></table>", "stderr": b""})()
        with patch.dict("os.environ", {"GITHUB_TOKEN": ""}), patch.object(
            parity.subprocess, "run", side_effect=[failed, good]
        ) as runner, patch.object(parity.time, "sleep") as sleep:
            self.assertEqual(parity.render_github("|a|\n|-|\n"), "<table></table>")
        self.assertEqual(runner.call_count, 2)
        sleep.assert_called_once()

    def test_api_failure_is_blocking_not_a_skip(self):
        with (
            patch.object(parity, "FILES", (Path("docs/foundation/conformance.md"),)),
            patch.dict("os.environ", {"GITHUB_TOKEN": ""}),
            patch.object(parity.subprocess, "run", side_effect=FileNotFoundError),
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(parity.main(), 1)
        self.assertIn("gh CLI is required", error.getvalue())


if __name__ == "__main__":
    unittest.main()
