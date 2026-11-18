"""Accounting and reader-parity tests for foundation register candidates."""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_decision_state as ds
import reserve_register_number as reserve
import check_foundation_register_narrative as narrative
from foundation_register_rows import candidate_cells, decision_rows, has_digit


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
