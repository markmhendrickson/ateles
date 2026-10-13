#!/usr/bin/env python3
"""Binding tests for the rule-inventory candidate-data packager and gate.

The pull_request_target workflow these scripts once served was removed by
ateles#1333: complete cross-store measurement is now a private, milestone-
driven audit run locally (docs/runbooks/rule_inventory_audit.md), and no
PR-gating job needs a self-hosted runner. The packager and gate stay as the
rollback path and for auditing a candidate tree as data, so their own
boundary behaviour stays pinned here.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import package_rule_inventory_inputs as inputs
import render_rule_inventory as renderer
import run_canonical_rule_inventory_gate as gate


def make_source(root: Path) -> None:
    for relative in sorted(inputs.EXACT_INPUTS):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"data for {relative}\n", encoding="utf-8")
    skill = root / ".claude" / "skills" / "example" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill data\n", encoding="utf-8")
    hook = root / ".claude" / "hooks" / "example.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("hook data\n", encoding="utf-8")


class CandidateDataPackerTest(unittest.TestCase):
    def assert_boundary_reason(self, reason: str, operation) -> None:
        with self.assertRaises(inputs.InputBoundaryError) as raised:
            operation()
        self.assertEqual(raised.exception.reason, reason)
        self.assertNotIn("/", str(raised.exception))

    def test_package_and_validate_fixed_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "data"
            source.mkdir()
            make_source(source)
            inputs.package_inputs(source, destination)
            inputs.validate_inputs(destination)
            packaged = {
                path.relative_to(destination).as_posix()
                for path in destination.rglob("*")
                if path.is_file()
            }
            self.assertIn(inputs.MANIFEST, packaged)
            self.assertIn(".claude/skills/example/SKILL.md", packaged)
            self.assertIn(".claude/hooks/example.py", packaged)

    def test_changed_data_after_packaging_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "data"
            source.mkdir()
            make_source(source)
            inputs.package_inputs(source, destination)
            (destination / "CLAUDE.md").write_text("changed\n", encoding="utf-8")
            self.assert_boundary_reason(
                "malformed_manifest", lambda: inputs.validate_inputs(destination)
            )

    def test_symlinked_candidate_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            make_source(source)
            outside = root / "outside"
            outside.write_text("outside\n", encoding="utf-8")
            target = source / "CLAUDE.md"
            target.unlink()
            target.symlink_to(outside)
            self.assert_boundary_reason(
                "symlink", lambda: inputs.package_inputs(source, root / "data")
            )

    def test_symlinked_optional_store_root_is_rejected(self) -> None:
        for store_name in ("skills", "hooks"):
            with (
                self.subTest(store_name=store_name),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                source = root / "source"
                source.mkdir()
                for relative in sorted(inputs.EXACT_INPUTS):
                    path = source / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("candidate data\n", encoding="utf-8")
                outside = root / "outside"
                outside.mkdir()
                claude = source / ".claude"
                claude.mkdir()
                (claude / store_name).symlink_to(outside, target_is_directory=True)
                self.assert_boundary_reason(
                    "symlink", lambda: inputs.package_inputs(source, root / "data")
                )

    def test_undeclared_destination_file_is_rejected_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "data"
            source.mkdir()
            make_source(source)
            inputs.package_inputs(source, destination)
            (destination / "undeclared.txt").write_text("extra\n", encoding="utf-8")
            self.assert_boundary_reason(
                "undeclared_path", lambda: inputs.validate_inputs(destination)
            )

    def test_oversized_input_is_rejected_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            make_source(source)
            with mock.patch.object(inputs, "MAX_FILE_BYTES", 4):
                self.assert_boundary_reason(
                    "oversized", lambda: inputs.package_inputs(source, root / "data")
                )

    def test_oversized_total_is_rejected_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            make_source(source)
            with (
                mock.patch.object(inputs, "MAX_FILE_BYTES", 1024),
                mock.patch.object(inputs, "MAX_TOTAL_BYTES", 8),
            ):
                self.assert_boundary_reason(
                    "oversized", lambda: inputs.package_inputs(source, root / "data")
                )

    def test_malformed_manifest_is_rejected_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "data"
            source.mkdir()
            make_source(source)
            inputs.package_inputs(source, destination)
            manifest_path = destination / inputs.MANIFEST
            original = json.loads(manifest_path.read_text(encoding="utf-8"))
            mutants = [
                "{",
                json.dumps({**original, "schema": 99}),
                json.dumps(
                    {
                        **original,
                        "files": [
                            {**original["files"][0], "sha256": "short"},
                            *original["files"][1:],
                        ],
                    }
                ),
            ]
            for mutant in mutants:
                with self.subTest(mutant=mutant[:24]):
                    manifest_path.write_text(mutant, encoding="utf-8")
                    self.assert_boundary_reason(
                        "malformed_manifest",
                        lambda: inputs.validate_inputs(destination),
                    )

    def test_missing_exact_input_is_rejected_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            make_source(source)
            (source / "CLAUDE.md").unlink()
            self.assert_boundary_reason(
                "missing_exact_inputs",
                lambda: inputs.package_inputs(source, root / "data"),
            )

    def test_existing_destination_is_rejected_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "data"
            source.mkdir()
            destination.mkdir()
            make_source(source)
            self.assert_boundary_reason(
                "destination_exists",
                lambda: inputs.package_inputs(source, destination),
            )

    def test_invalid_root_is_rejected_with_reason(self) -> None:
        self.assert_boundary_reason(
            "invalid_root",
            lambda: inputs.package_inputs(Path("relative-source"), Path("data")),
        )

    def test_path_escape_is_rejected_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            outside = root / "CLAUDE.md"
            outside.write_text("outside\n", encoding="utf-8")
            self.assert_boundary_reason(
                "path_escape", lambda: inputs._read_regular_file(source, outside)
            )

    def test_cli_reports_stable_reason_without_private_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            make_source(source)
            (source / "CLAUDE.md").unlink()
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "package_rule_inventory_inputs.py",
                        "--source",
                        str(source),
                        "--destination",
                        str(root / "data"),
                    ],
                ),
                contextlib.redirect_stderr(stderr),
            ):
                result = inputs.main()
            self.assertEqual(result, 2)
            self.assertEqual(
                stderr.getvalue(),
                "canonical rule inventory input boundary rejected "
                "reason=missing_exact_inputs\n",
            )
            self.assertNotIn(str(root), stderr.getvalue())


class MeasurementInstrumentTest(unittest.TestCase):
    def complete_stores(self) -> list[renderer.Store]:
        return [
            renderer.Store(name=name, location="safe")
            for name in sorted(renderer.REQUIRED_MEASUREMENT_STORES)
        ]

    def run_renderer(self, clusters, statements, expected: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "docs" / "foundation" / "rule_inventory.md"
            output.parent.mkdir(parents=True)
            output.write_text(expected, encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    renderer, "read_entities", return_value=(statements, [])
                ),
                mock.patch.object(
                    renderer,
                    "read_file_stores",
                    return_value=([], self.complete_stores()),
                ),
                mock.patch.object(
                    renderer, "build_clusters", return_value=(clusters, [])
                ),
                mock.patch.object(renderer, "render", return_value=expected),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "render_rule_inventory.py",
                        "--repository-input-root",
                        str(root),
                        "--expected-output",
                        str(output),
                        "--check",
                        "--require-complete-measurement",
                    ],
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = renderer.main()
            return result, stderr.getvalue()

    def test_known_positive_complete_measurement_is_nonzero(self) -> None:
        statement = renderer.Statement(
            store="ateles/CLAUDE.md",
            location="CLAUDE.md",
            locator="line",
            text="Never accept a silent zero as a complete measurement.",
            kind="test_must_fail_red",
        )
        cluster = renderer.Cluster(
            kind="test_must_fail_red",
            label="A test must fail on the violation it watches",
            statements=[statement],
        )
        self.assertEqual(renderer.measurement_totals([cluster]), (1, 1))
        result, stderr = self.run_renderer([cluster], [statement], "expected\n")
        self.assertEqual(result, 0, stderr)

    def test_silent_zero_fails_complete_measurement(self) -> None:
        result, stderr = self.run_renderer([], [], "expected\n")
        self.assertEqual(result, 3)
        self.assertIn("instrument returned zero rules or statements", stderr)


class StableGateOutputTest(unittest.TestCase):
    def test_renderer_integrity_failure_is_stable_and_fail_closed(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(gate.Path, "is_symlink", return_value=True),
            contextlib.redirect_stderr(stderr),
        ):
            result = gate.main(["gate", "/private/candidate-inputs"])
        self.assertEqual(result, 2)
        self.assertEqual(
            stderr.getvalue(),
            "rule inventory measurement failed before a safe verdict\n",
        )
        self.assertNotIn("/private", stderr.getvalue())

    def test_renderer_output_is_captured_and_complete_flag_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "data"
            source.mkdir()
            make_source(source)
            inputs.package_inputs(source, destination)
            private_child_output = b"private path ent_private statement value"
            completed = mock.Mock(
                returncode=1,
                stdout=private_child_output,
                stderr=private_child_output,
            )
            stderr = io.StringIO()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "NEOTOMA_BEARER_TOKEN": "masked",
                        "RULE_INVENTORY_CANONICAL_REPOSITORY_ROOTS": "/private",
                    },
                    clear=False,
                ),
                mock.patch.object(
                    gate.subprocess, "run", return_value=completed
                ) as run,
                contextlib.redirect_stderr(stderr),
            ):
                result = gate.main(["gate", str(destination)])
            self.assertEqual(result, 1)
            self.assertIn("differs from the complete", stderr.getvalue())
            self.assertNotIn("private path", stderr.getvalue())
            command = run.call_args.args[0]
            self.assertIn("--require-complete-measurement", command)
            self.assertIn("--repository-input-root", command)
            self.assertIn("--expected-output", command)
            self.assertEqual(run.call_args.kwargs["stdout"], gate.subprocess.PIPE)
            self.assertEqual(run.call_args.kwargs["stderr"], gate.subprocess.PIPE)

    def test_exit_class_message_map_and_unknown_fail_closed(self) -> None:
        cases = {
            0: (0, "rule inventory matches the complete canonical measurement"),
            1: (1, "rule inventory differs from the complete canonical measurement"),
            2: (2, "rule inventory public-output safety check failed"),
            3: (
                3,
                "rule inventory equality unavailable: full measurement is incomplete",
            ),
            99: (2, "rule inventory measurement failed before a safe verdict"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "data"
            source.mkdir()
            make_source(source)
            inputs.package_inputs(source, destination)
            for child_rc, (expected_rc, expected_message) in cases.items():
                with self.subTest(child_rc=child_rc):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    completed = mock.Mock(
                        returncode=child_rc, stdout=b"private", stderr=b"private"
                    )
                    with (
                        mock.patch.dict(
                            os.environ,
                            {
                                "NEOTOMA_BEARER_TOKEN": "masked",
                                "RULE_INVENTORY_CANONICAL_REPOSITORY_ROOTS": "/private",
                            },
                            clear=False,
                        ),
                        mock.patch.object(
                            gate.subprocess, "run", return_value=completed
                        ),
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        result = gate.main(["gate", str(destination)])
                    self.assertEqual(result, expected_rc)
                    self.assertIn(
                        expected_message, stdout.getvalue() + stderr.getvalue()
                    )
                    self.assertNotIn("private", stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
