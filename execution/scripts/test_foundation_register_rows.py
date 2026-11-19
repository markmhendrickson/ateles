"""Accounting and reader-parity tests for foundation register candidates."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_decision_state as ds
import reserve_register_number as reserve
import check_foundation_register_narrative as narrative
import check_plan_decision_citations as citations
import check_register_render_parity as parity
from foundation_register_rows import candidate_cells, decision_rows, has_digit, revision_rows


def register(**rows: str) -> str:
    header = (
        "| # | The question | Argued in | Blocks | Status |\n|---|---|---|---|---|\n"
    )
    return header + "".join(
        f"| {number} | question | `doc.md#q` | — | {status} |\n"
        for number, status in rows.items()
    )


class TestRegisterCandidateAccounting(unittest.TestCase):
    """Every table candidate is parsed once or rejected on every reader surface."""

    SPELLINGS = (
        "999",
        "**999**",
        "_999_",
        "`999`",
        "~~999~~",
        "[999](x)",
        "<b>999</b>",
        "\\999",
        "９９９",
        "٩٩٩",
        "𝟡𝟡𝟡",
        "999-",
        "999.",
        "999x",
        "99 9",
        "999–1000",
        "",
        "label",
    )
    MODES = (("--check",), ("--json",), ("--stdout",), ("--out",))

    def test_candidate_accounting_for_digit_cells(self):
        base = "## The register of open design decisions\n" + register(
            **{"1": "**open**"}
        )
        parsed = errors = candidates = 0
        for cell in self.SPELLINGS:
            if not has_digit(cell):
                continue
            candidate = f"| {cell} | q | `doc.md#q` | — | **open** |\n"
            self.assertIsNotNone(candidate_cells(candidate))
            for place in ("inside", "before", "after"):
                candidates += 1
                if place == "inside":
                    text = base + candidate
                elif place == "before":
                    text = candidate + base
                else:
                    text = base + "## Later\n" + candidate
                try:
                    rows = decision_rows(text)
                    parsed += len(rows) - 1
                except ValueError:
                    errors += 1
        self.assertEqual(parsed + errors, candidates)

    def test_error_message_has_line_but_no_untrusted_control_text(self):
        text = (
            register(**{"1": "**open**"})
            + "| 999\x1b[31m | q | `doc.md#q` | — | **open** |\n"
        )
        with self.assertRaises(ValueError) as error:
            decision_rows(text)
        self.assertIn("line 4", str(error.exception))
        self.assertNotIn("\x1b", str(error.exception))

    def test_text_before_a_digit_outside_the_section_is_still_rejected(self):
        text = (
            "| abc999 | q | `doc.md#q` | — | **open** |\n"
            "## The register of open design decisions\n" + register(**{"1": "**open**"})
        )
        with self.assertRaisesRegex(ValueError, "outside the register section"):
            decision_rows(text)

        disguised = text.replace("abc999", "Decision 78 ruling")
        with self.assertRaisesRegex(ValueError, "outside the register section"):
            decision_rows(disguised)

    def test_the_outside_section_error_points_to_the_exemption_table(self):
        text = (
            "| abc999 | q | `doc.md#q` | — | **open** |\n"
            "## The register of open design decisions\n" + register(**{"1": "**open**"})
        )
        with self.assertRaisesRegex(ValueError, "OUTSIDE_LABEL_WIDTHS in execution/scripts/foundation_register_rows.py"):
            decision_rows(text)

    def test_matrix_has_no_silent_omission(self):
        import tempfile

        base = register(**{"1": "**open**"})
        heading = "## The register of open design decisions\n"
        row = "| {cell} | question | `doc.md#q` | — | **open** |\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "conformance.md"
            with unittest.mock.patch.object(ds, "FOUNDATION_DIR", root):
                for cell in self.SPELLINGS:
                    for indent in ("", " ", "   ", "    ", "     ", "\t"):
                        for place in ("inside", "before", "after"):
                            candidate = indent + row.format(cell=cell)
                            if place == "inside":
                                text = heading + base + candidate
                            elif place == "before":
                                text = candidate + heading + base
                            else:
                                text = heading + base + "## Later\n" + candidate
                            path.write_text(text)
                            valid = (
                                cell in ("999", "999–1000")
                                and place == "inside"
                                and indent in ("", " ", "   ")
                            )
                            for mode in self.MODES:
                                target = root / "out.md"
                                target.unlink(missing_ok=True)
                                flags = list(mode)
                                if mode == ("--out",):
                                    flags.append(str(target))
                                out, err = io.StringIO(), io.StringIO()
                                with (
                                    contextlib.redirect_stdout(out),
                                    contextlib.redirect_stderr(err),
                                ):
                                    code = ds.main(flags)
                                if valid or (
                                    place != "inside"
                                    and not cell.isdecimal()
                                    and not any(char.isdigit() for char in cell)
                                ):
                                    self.assertEqual(
                                        code,
                                        0,
                                        (cell, indent, place, mode, err.getvalue()),
                                    )
                                else:
                                    self.assertEqual(
                                        code,
                                        1,
                                        (cell, indent, place, mode, out.getvalue()),
                                    )
                                    self.assertIn("line ", err.getvalue())

    def test_all_three_decision_readers_agree(self):
        for cell in self.SPELLINGS:
            text = (
                register(**{"1": "**open**"})
                + f"| {cell} | q | `doc.md#q` | — | **open** |\n"
            )
            readers = (
                lambda: ds.parse_rows(text),
                lambda: reserve.numbers("decision", text),
                lambda: narrative.parse_register(text),
            )
            results = []
            for reader in readers:
                try:
                    results.append(("rows", len(reader())))
                except ValueError:
                    results.append(("error", None))
            self.assertEqual([kind for kind, _ in results], [results[0][0]] * 3, cell)
            if results[0][0] == "rows" and cell != "999–1000":
                self.assertEqual(results, [results[0]] * 3, cell)


# Rendered with GitHub's /markdown API (mode=gfm) on 2026-11-18. To refresh,
# send header + delimiter + row 1 + each shape to `gh api markdown` with
# {"text": markdown, "mode": "gfm"}, and count <tbody><tr> in the HTML.
GFM_SHAPES = {
    "pipe_led": "| 999 | q | a | b | **open** |",
    "bare": "999 | q | a | b | **open** |",
    "bold": "**999** | q | a | b | **open** |",
    "dash": "999- | q | a | b | **open** |",
    "dot": "999. | q | a | b | **open** |",
    "x": "999x | q | a | b | **open** |",
    "link": "[999](#x) | q | a | b | **open** |",
    "strike": "~~999~~ | q | a | b | **open** |",
    "html_s": "<s>999</s> | q | a | b | **open** |",
    "escaped": r"999 \| q | a | b | **open** |",
    "arabic_digits": "٩٩٩ | q | a | b | **open** |",
    "fullwidth": "９９９ | q | a | b | **open** |",
    "zwsp_pipe": "\u200b| 998 | q | a | b | **open** |",
    "blockquote": "> | 999 | q | a | b | **open** |",
    "html_tr": "<tr><td>999</td></tr>",
    "no_pipe_text": "plain table body text",
    "label_only_pipe_end": "label |",
    "indent4_pipe": "    | 999 | q | a | b | **open** |",
    "tab_pipe": "\t| 999 | q | a | b | **open** |",
}


class TestGfmTableBlock(unittest.TestCase):
    def test_short_gfm_delimiters_fail_closed_across_readers(self):
        base = "## The register of open design decisions\n" + register(**{"1": "**open**"})
        for delimiter in ("|-|-|", "| :-: | - |"):
            outside = f"## Other\n| a | b |\n{delimiter}\n999 lost row\n"
            decision = base + outside
            revision = f"## Other\n| a | b |\n{delimiter}\n999 lost row\n"
            for reader in (
                lambda: decision_rows(decision),
                lambda: ds.parse_rows(decision),
                lambda: reserve.numbers("decision", decision),
                lambda: narrative.parse_register(decision),
                lambda: parity.classified_tables("register", decision),
                lambda: revision_rows(revision),
                lambda: reserve.numbers("revision", revision),
                lambda: parity.classified_tables("revision", revision),
            ):
                with self.subTest(delimiter=delimiter, reader=reader), self.assertRaises(ValueError):
                    reader()
            with unittest.mock.patch.object(citations, "run", return_value=(0, decision)):
                with self.assertRaises(ValueError):
                    citations.register_rows()

    def test_github_fixture_and_reader_parity(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/gfm_row_fixture.json").read_text()
        )["cases"]
        self.assertEqual({name.split(":")[0] for name in fixture}, set(GFM_SHAPES))
        decision_base = "## The register of open design decisions\n" + register(**{"1": "**open**"})
        revision_base = (
            "## Example\n| Revision | Pass | What changed |\n|---|---|---|\n"
            "| 1 | pass | change |\n"
        )
        for key, result in fixture.items():
            shape, placement = key.split(":")
            line = GFM_SHAPES[shape]
            gap = "\n" if placement == "after_blank" else ""
            decision = decision_base + gap + line + "\n"
            revision_line = line.replace("| q | a | b | **open** |", "| p | c |")
            revision = revision_base + gap + revision_line + "\n"
            self.assertEqual(result["rows"] == 2, placement == "directly_after" and shape not in
                             {"dot", "blockquote", "html_tr", "indent4_pipe", "tab_pipe"})
            expected_error = not (placement == "directly_after" and shape in
                                  {"pipe_led", "bare", "zwsp_pipe"})
            if placement == "after_blank" and "|" not in line:
                expected_error = False
            decision_readers = (
                lambda: ds.parse_rows(decision),
                lambda: reserve.numbers("decision", decision),
                lambda: narrative.parse_register(decision),
            )
            revision_readers = (
                lambda: revision_rows(revision),
                lambda: reserve.numbers("revision", revision),
            )
            for readers, should_error in (
                (decision_readers, expected_error),
                (revision_readers, expected_error and not
                 (placement == "after_blank" and shape == "label_only_pipe_end")),
            ):
                for reader in readers:
                    with self.subTest(key=key, reader=reader):
                        if should_error:
                            with self.assertRaisesRegex(ValueError, "line [56]"):
                                reader()
                        else:
                            reader()

    def test_pipeless_lines_in_each_table_position(self):
        decision_base = "## The register of open design decisions\n" + register(**{"1": "**open**"})
        revision_base = "## Example\n| Revision | Pass | Change |\n|---|---|---|\n| 1 | p | c |\n"
        for base, reader in ((decision_base, decision_rows), (revision_base, revision_rows)):
            for text in (base.replace("|---|", "no pipe\n|---|", 1),
                         base.replace("| 1 |", "no pipe\n| 1 |", 1),
                         base + "no pipe\n"):
                with self.subTest(base=base[:20], text=text[:90]):
                    with self.assertRaisesRegex(ValueError, "line "):
                        reader(text)
            # A blank line closes the GFM table. Plain prose can follow it.
            reader(base + "\nno pipe\n")

    def test_outside_section_optional_pipe_and_unicode_digits(self):
        base = "## The register of open design decisions\n" + register(**{"1": "**open**"})
        for line in ("**999** | q | a | b | **open** |", "999- | q | a | b | **open** |",
                     "[999](#x) | q | a | b | **open** |", "٩٩٩ | q | a | b | **open** |",
                     "\u200b| 998 | q | a | b | **open** |", "> | 999 | q | a | b | **open** |"):
            with self.subTest(line=line):
                with self.assertRaises(ValueError):
                    decision_rows(line + "\n" + base)
                with self.assertRaises(ValueError):
                    revision_rows("## Other\n" + line)

    def test_spacing_and_tabs_are_accounted(self):
        base = "## The register of open design decisions\n" + register(**{"1": "**open**"})
        revision = "| Revision | Pass | Change |\n|---|---|---|\n| 1 | p | c |\n"
        for prefix in (" ", "  ", "   ", "    ", "     ", "\t"):
            line = prefix + "999 | q | a | b | **open** |\n"
            revision_line = prefix + "999 | p | c |\n"
            for reader, text in ((decision_rows, base + line),
                                 (revision_rows, revision + revision_line)):
                with self.subTest(prefix=repr(prefix), reader=reader):
                    if len(prefix) <= 3 and prefix != "\t":
                        reader(text)
                    else:
                        with self.assertRaises(ValueError):
                            reader(text)

    def test_cli_modes_and_branch_copy_reject_malformed_rows_without_writing(self):
        import tempfile
        from test_decision_state import FakeGit

        base = "## The register of open design decisions\n" + register(**{"1": "**open**"})
        shapes = ("bold", "dash", "link", "arabic_digits", "blockquote", "zwsp_pipe")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "conformance.md"
            with unittest.mock.patch.object(ds, "FOUNDATION_DIR", root):
                for shape in shapes:
                    for gap in ("", "\n"):
                        text = base + gap + GFM_SHAPES[shape] + "\n"
                        source.write_text(text)
                        valid = shape == "zwsp_pipe" and not gap
                        for mode in (("--check",), ("--json",), ("--stdout",), ("--out",)):
                            target = root / "projection.md"
                            target.unlink(missing_ok=True)
                            args = list(mode) + ([str(target)] if mode == ("--out",) else [])
                            out, err = io.StringIO(), io.StringIO()
                            with (contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
                                code = ds.main(args)
                            with self.subTest(shape=shape, gap=bool(gap), mode=mode):
                                self.assertEqual(code, 0 if valid else 1, err.getvalue())
                                if not valid:
                                    self.assertIn("line ", err.getvalue())
                                    self.assertFalse(target.exists())
                        fake = FakeGit({ds.MAIN_REF: base, "origin/feature": text}, ["origin/feature"])
                        with unittest.mock.patch.object(ds, "FOUNDATION_DIR", Path("docs/foundation")), \
                             unittest.mock.patch.object(ds, "run", fake), \
                             contextlib.redirect_stdout(io.StringIO()), \
                             contextlib.redirect_stderr(io.StringIO()):
                            branch_code = ds.main(["--branches", "--json"])
                        self.assertEqual(branch_code, 0 if valid else 1, (shape, bool(gap)))

    def test_every_gfm_table_body_candidate_is_row_or_error(self):
        base = "## The register of open design decisions\n" + register(**{"1": "**open**"})
        parsed = errors = 0
        for shape, line in GFM_SHAPES.items():
            text = base + line + "\n"
            try:
                parsed += len(decision_rows(text)) - 1
            except ValueError:
                errors += 1
        self.assertEqual(parsed + errors, len(GFM_SHAPES))

    def test_number_keys_reject_leading_zeros_and_normalize_range_dash(self):
        base = register(**{"7": "**open**"})
        for number in ("07", "5-07"):
            with self.assertRaises(ValueError):
                ds.parse_rows(base + f"| {number} | q | a | b | **open** |\n")
            with self.assertRaises(ValueError):
                reserve.numbers("decision", base + f"| {number} | q | a | b | **open** |\n")
        for number in ("5-7", "5–7"):
            text = register(**{number: "**open**"})
            self.assertEqual(list(ds.parse_rows(text)), ["5–7"])
            self.assertEqual(reserve.numbers("decision", text), [5, 6, 7])
        with self.assertRaisesRegex(ValueError, "duplicate register row"):
            decision_rows(register(**{"5-7": "**open**", "5–7": "**open**"}))

    def test_unescaped_pipe_inside_code_span_is_an_extra_cell(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/gfm_row_fixture.json").read_text())
        row = fixture["unescaped_code_pipe"]
        text = (
            "| # | The question | Argued in | Blocks | Status |\n"
            "|---|---|---|---|---|\n"
            + row["row"] + "\n"
        )
        self.assertEqual(len(candidate_cells(text.splitlines()[2])), row["gfm_cell_count"])
        with self.assertRaisesRegex(ValueError, "malformed register row"):
            decision_rows(text)
        with self.assertRaisesRegex(ValueError, "malformed revision row"):
            revision_rows("| Revision | Pass | Change |\n|---|---|---|\n| 999 | `a | b` | change |\n")
        self.assertEqual(
            candidate_cells(r"| 1 | q | a | `left\|right` and more | **open** |")[-2],
            r"`left\|right` and more",
        )

    def test_real_row_70_has_five_cells_and_full_blocks_text(self):
        source = Path("docs/foundation/conformance.md").read_text()
        line = next(line for line in source.splitlines() if line.startswith("| 70 |"))
        self.assertEqual(len(candidate_cells(line)), 5)
        self.assertIn(r"`*\|intake`", decision_rows(source)["70"]["blocks"])

    def test_pipeless_digit_rows_in_outside_tables(self):
        outside = "## Other\n| Label | Value |\n|---|---|\n| ordinary | value |\n"
        decision = "## The register of open design decisions\n" + register(**{"1": "**open**"})
        revision = "## Revisions\n| Revision | Pass | Change |\n|---|---|---|\n| 1 | p | c |\n"
        for cell in ("999 lost row", "٩٩٩ lost row", "**999** x", "[999](x) y", "  999 lost row"):
            with self.subTest(cell=cell):
                with self.assertRaisesRegex(ValueError, "outside the register section"):
                    decision_rows(outside + cell + "\n" + decision)
                with self.assertRaisesRegex(ValueError, "malformed revision row"):
                    revision_rows(outside + cell + "\n" + revision)
                decision_rows(outside + "\n" + cell + "\n" + decision)
                revision_rows(outside + "\n" + cell + "\n" + revision)
        # The declared exemptions keep their exact labels and widths.
        decision_rows("| Check | A | B | C |\n|---|---|---|---|\n"
                      "| Decision 78 ruling | x | y | z |\n" + decision)

    def test_non_ascii_whitespace_does_not_end_table_or_split_lines(self):
        decision = register(**{"1": "**open**"})
        revision = "| Revision | Pass | Change |\n|---|---|---|\n| 1 | p | c |\n"
        probes = json.loads((Path(__file__).parent / "fixtures/gfm_row_fixture.json").read_text())[
            "unicode_probe_inputs"
        ]
        for probe in probes:
            with self.subTest(name=probe["name"], position=probe["position"]):
                with self.assertRaisesRegex(ValueError, "malformed register row"):
                    decision_rows(decision + probe["text"] + "\n")
                with self.assertRaisesRegex(ValueError, "malformed revision row"):
                    revision_rows(revision + probe["text"] + "\n")
        for blank in ("", " ", "\t", " \t "):
            decision_rows(decision + blank + "\n999 plain pipeless\n")
            revision_rows(revision + blank + "\n999 plain pipeless\n")
        for ending in ("\n", "\r", "\r\n"):
            with self.subTest(ending=repr(ending)):
                self.assertEqual(len(decision_rows(decision.replace("\n", ending))), 1)
                self.assertEqual(len(revision_rows(revision.replace("\n", ending))), 1)

    def test_status_source_vocabulary_rejects_invisible_and_lookalike_text(self):
        for status in ("**o\u200bpen**", "\u200b**open**", "**open**\u200b",
                       "**оpen**", "**open**extra", "*open*"):
            with self.subTest(status=status):
                with self.assertRaisesRegex(ValueError, "(malformed|unknown) .*status"):
                    decision_rows(register(**{"1": status}))
        self.assertEqual(decision_rows(register(**{"1": "**open** (context)"}))["1"]["status"], "open")

    def test_outside_pipeless_row_reaches_all_decision_consumers(self):
        text = (
            "## Other\n| Check | Value |\n|---|---|\n| ordinary | value |\n"
            "999 lost row\n## The register of open design decisions\n"
            + register(**{"1": "**open**"})
        )
        for reader in (
            lambda: ds.parse_rows(text),
            lambda: reserve.numbers("decision", text),
            lambda: narrative.parse_register(text),
        ):
            with self.subTest(reader=reader), self.assertRaises(ValueError):
                reader()
        with unittest.mock.patch.object(citations, "run", return_value=(0, text)):
            with self.assertRaises(ValueError):
                citations.register_rows()

    def test_all_four_renderer_modes_reject_status_pollution(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "conformance.md"
            with unittest.mock.patch.object(ds, "FOUNDATION_DIR", root):
                for status in ("**o\u200bpen**", "\u200b**open**", "**open**\u200b",
                               "**оpen**", "**open**extra", "*open*"):
                    path.write_text(register(**{"1": status}))
                    for mode in TestRegisterCandidateAccounting.MODES:
                        target = root / "out.md"
                        target.unlink(missing_ok=True)
                        flags = list(mode) + ([str(target)] if mode == ("--out",) else [])
                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            code = ds.main(flags)
                        with self.subTest(status=repr(status), mode=mode):
                            self.assertEqual(code, 1)
                            self.assertFalse(target.exists())
