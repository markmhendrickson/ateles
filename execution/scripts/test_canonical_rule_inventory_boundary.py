#!/usr/bin/env python3
"""Binding tests for the trusted-code/candidate-data measurement boundary."""

from __future__ import annotations

import contextlib
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import package_rule_inventory_inputs as inputs
import run_canonical_rule_inventory_gate as gate

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "canonical-rule-inventory.yml"


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


class CandidateDataPackerTest(unittest.TestCase):
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
            with self.assertRaises(inputs.InputBoundaryError):
                inputs.validate_inputs(destination)

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
            with self.assertRaises(inputs.InputBoundaryError):
                inputs.package_inputs(source, root / "data")


class StableGateOutputTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
