#!/usr/bin/env python3
"""Binding tests for the trusted-code/candidate-data measurement boundary."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import package_rule_inventory_inputs as inputs
import render_rule_inventory as renderer
import run_canonical_rule_inventory_gate as gate

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "canonical-rule-inventory.yml"
ATELES_TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ateles-tests.yml"


def job_blocks(workflow: str) -> dict[str, str]:
    if "\njobs:" not in workflow:
        raise AssertionError("workflow has no jobs section")
    section = workflow[workflow.index("\njobs:") :]
    headers = list(re.finditer(r"^  ([a-zA-Z0-9_-]+):\s*$", section, re.M))
    return {
        header.group(1): section[
            header.end() : headers[index + 1].start()
            if index + 1 < len(headers)
            else len(section)
        ]
        for index, header in enumerate(headers)
    }


def verify_privileged_boundary(workflow: str) -> None:
    if "pull_request_target:" not in workflow or "workflow_dispatch:" in workflow:
        raise AssertionError("privileged workflow trigger is not default-branch-only")
    blocks = job_blocks(workflow)
    if set(blocks) != {"trusted-source", "candidate-inputs", "rule-inventory"}:
        raise AssertionError("unexpected workflow job set")
    privileged = blocks["rule-inventory"]
    trusted_source = blocks["trusted-source"]
    fork_refusal = (
        '          if [ "$HEAD_REPOSITORY" != "$GITHUB_REPOSITORY" ]; then\n'
        '            echo "::error::canonical measurement refuses code from fork repositories"\n'
        "            exit 1\n"
        "          fi\n"
    )
    if (
        fork_refusal not in trusted_source
        or "needs: trusted-source" not in blocks["candidate-inputs"]
    ):
        raise AssertionError("fork refusal is not binding before candidate admission")
    if "runs-on: [self-hosted, macOS, canonical-rule-inventory]" not in privileged:
        raise AssertionError("canonical job is not pinned to its runner class")
    if "needs: candidate-inputs" not in privileged:
        raise AssertionError("privileged job can bypass candidate-data admission")
    if "github.event.pull_request.head" in privileged or re.search(
        r"^\s+path:\s*candidate\s*$", privileged, re.M
    ):
        raise AssertionError("privileged job can check out candidate code")
    if "ref: ${{ github.event.repository.default_branch }}" not in privileged:
        raise AssertionError("privileged checkout is not the trusted default branch")
    if (
        "path: trusted" not in privileged
        or "persist-credentials: false" not in privileged
    ):
        raise AssertionError(
            "trusted checkout does not preserve the credential boundary"
        )
    allowed_actions = {
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "actions/download-artifact@v4",
    }
    actions = set(re.findall(r"uses:\s*([^\s]+)", privileged))
    if not actions.issubset(allowed_actions):
        raise AssertionError("privileged job invokes an unapproved action")
    commands = [
        line.strip()
        for block in re.findall(r"run:\s*\|\n((?:[ \t]+.*\n?)*)", privileged)
        for line in block.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    allowed_prefixes = (
        "python3 trusted/execution/scripts/package_rule_inventory_inputs.py",
        "python3 trusted/execution/scripts/run_canonical_rule_inventory_gate.py",
        "--validate ",
        '"$GITHUB_WORKSPACE/candidate-inputs"',
    )
    if any(line != "\\" and not line.startswith(allowed_prefixes) for line in commands):
        raise AssertionError("privileged job can execute a non-trusted command")
    if "--validate" not in privileged:
        raise AssertionError("candidate data is not validated on the privileged host")
    if len(re.findall(r"^\s+NEOTOMA_BEARER_TOKEN:\s*", privileged, re.M)) != 1:
        raise AssertionError(
            "canonical credential is exposed outside one measurement step"
        )
    if "vars.RULE_INVENTORY_CANONICAL_REPOSITORY_ROOTS" in workflow:
        raise AssertionError(
            "private canonical roots moved into repository configuration"
        )
    candidate = blocks["candidate-inputs"]
    trusted_packer_checkout = (
        "      - name: Check out trusted measurement code\n"
        "        uses: actions/checkout@v4\n"
        "        with:\n"
        "          ref: ${{ github.event.repository.default_branch }}\n"
        "          path: trusted\n"
        "          persist-credentials: false\n"
    )
    if trusted_packer_checkout not in candidate:
        raise AssertionError("candidate packer checkout is not trusted")
    if "github.event.pull_request.head.sha" not in candidate:
        raise AssertionError("candidate inputs are not bound to the declared PR head")
    if (
        "python3 trusted/execution/scripts/package_rule_inventory_inputs.py"
        not in candidate
    ):
        raise AssertionError(
            "candidate input packer is not trusted default-branch code"
        )


def verify_candidate_artifact_transfer(workflow: str) -> None:
    """The curated data tree must cross the artifact boundary intact.

    ``actions/upload-artifact`` excludes hidden files unless callers opt in.
    Both the manifest that binds the candidate tree and the ``.claude`` rule
    stores are hidden, so omitting this setting makes every real measurement
    incomplete even though the packer and validator are individually sound.
    """

    candidate = job_blocks(workflow)["candidate-inputs"]
    upload_match = re.search(
        r"uses:\s*actions/upload-artifact@v4\s*\n"
        r"\s+with:\s*\n(?P<with>(?:\s{10,}[^\n]*\n?)*)",
        candidate,
    )
    if upload_match is None:
        raise AssertionError("candidate input artifact upload step is missing")
    upload_with = upload_match.group("with")
    if not re.search(r"^\s+path:\s*candidate-inputs\s*$", upload_with, re.M):
        raise AssertionError("artifact upload is not limited to the curated data tree")
    hidden_setting = re.search(
        r"^\s+include-hidden-files:\s*([^\s#]+)", upload_with, re.M
    )
    if hidden_setting is None or hidden_setting.group(1).lower() != "true":
        raise AssertionError("candidate artifact upload excludes hidden inputs")


def verify_workflow_guidance(workflow: str) -> None:
    header = workflow.split("\non:", 1)[0]
    comments = [line for line in header.splitlines() if line.startswith("#")]
    if len(comments) > 15:
        raise AssertionError("workflow guidance exceeds 15 comment lines")
    required = {
        "untrusted_source",
        "boundary_rejected",
        "instrument_incomplete",
        "inventory_mismatch",
        "safety_failed",
        "match",
        "package_rule_inventory_inputs.py --source",
        "package_rule_inventory_inputs.py --validate",
        "run_canonical_rule_inventory_gate.py",
        "#1114",
        "#923",
        "#1123",
    }
    missing = sorted(item for item in required if item not in header)
    if missing:
        raise AssertionError("workflow guidance is incomplete: " + ", ".join(missing))


def verify_pytest_workflow_path_wiring(workflow: str) -> None:
    if '      - ".github/workflows/canonical-rule-inventory.yml"' not in workflow:
        raise AssertionError("canonical workflow edits do not trigger pytest")


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


class WorkflowBoundaryTest(unittest.TestCase):
    def test_real_workflow_has_trusted_code_candidate_data_boundary(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        verify_privileged_boundary(workflow)
        verify_candidate_artifact_transfer(workflow)
        verify_workflow_guidance(workflow)
        verify_pytest_workflow_path_wiring(
            ATELES_TEST_WORKFLOW.read_text(encoding="utf-8")
        )

    def test_candidate_artifact_upload_fails_closed_without_hidden_inputs(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "excludes hidden inputs"):
            verify_candidate_artifact_transfer(
                workflow.replace("          include-hidden-files: true\n", "", 1)
            )
        with self.assertRaisesRegex(AssertionError, "excludes hidden inputs"):
            verify_candidate_artifact_transfer(
                workflow.replace(
                    "          include-hidden-files: true",
                    "          include-hidden-files: false",
                    1,
                )
            )

    def test_planted_candidate_execution_after_checkout_is_rejected(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        prefix, privileged = workflow.split("  rule-inventory:\n", 1)
        planted_privileged = privileged.replace(
            "      - uses: actions/setup-python@v5\n",
            "      - name: planted candidate execution\n"
            "        run: |\n"
            "          python3 candidate-inputs/attacker_controlled.py\n"
            "      - uses: actions/setup-python@v5\n",
            1,
        )
        planted = prefix + "  rule-inventory:\n" + planted_privileged
        self.assertNotEqual(workflow, planted, "negative mutation was not planted")
        with self.assertRaisesRegex(AssertionError, "non-trusted command"):
            verify_privileged_boundary(planted)

    def test_workflow_dispatch_branch_override_is_rejected(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "trigger"):
            verify_privileged_boundary(
                workflow.replace("permissions:", "  workflow_dispatch:\n\npermissions:")
            )

    def test_fork_refusal_fails_red_when_message_is_removed(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            '            echo "::error::canonical measurement refuses code from fork repositories"\n',
            "",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        with self.assertRaisesRegex(AssertionError, "fork refusal"):
            verify_privileged_boundary(mutant)

    def test_fork_refusal_fails_red_when_exit_is_not_failure(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for replacement in ("            exit 0\n", ""):
            with self.subTest(replacement=replacement or "removed"):
                mutant = workflow.replace("            exit 1\n", replacement, 1)
                self.assertNotEqual(
                    workflow, mutant, "negative mutation was not planted"
                )
                with self.assertRaisesRegex(AssertionError, "fork refusal"):
                    verify_privileged_boundary(mutant)

    def test_candidate_packer_checkout_must_remain_trusted(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutations = {
            "candidate ref": (
                "          ref: ${{ github.event.repository.default_branch }}\n",
                "          ref: ${{ github.event.pull_request.head.sha }}\n",
            ),
            "candidate path": (
                "          path: trusted\n",
                "          path: candidate\n",
            ),
            "checkout credential": (
                "          persist-credentials: false\n",
                "          persist-credentials: true\n",
            ),
        }
        for label, (before, after) in mutations.items():
            with self.subTest(label=label):
                mutant = workflow.replace(before, after, 1)
                self.assertNotEqual(
                    workflow, mutant, "negative mutation was not planted"
                )
                with self.assertRaisesRegex(AssertionError, "packer checkout"):
                    verify_privileged_boundary(mutant)

    def test_workflow_guidance_fails_red_when_exit_class_is_removed(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace("# boundary_rejected", "# boundary-removed", 1)
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        with self.assertRaisesRegex(AssertionError, "guidance is incomplete"):
            verify_workflow_guidance(mutant)

    def test_canonical_workflow_edit_must_trigger_pytest_lane(self) -> None:
        workflow = ATELES_TEST_WORKFLOW.read_text(encoding="utf-8")
        verify_pytest_workflow_path_wiring(workflow)
        mutant = workflow.replace(
            '      - ".github/workflows/canonical-rule-inventory.yml"\n', "", 1
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        with self.assertRaisesRegex(AssertionError, "do not trigger pytest"):
            verify_pytest_workflow_path_wiring(mutant)


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
