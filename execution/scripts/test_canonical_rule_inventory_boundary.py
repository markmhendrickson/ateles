#!/usr/bin/env python3
"""Binding tests for the trusted-code/candidate-data measurement boundary."""

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

import yaml

import package_rule_inventory_inputs as inputs
import render_rule_inventory as renderer
import run_canonical_rule_inventory_gate as gate

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "canonical-rule-inventory.yml"
ATELES_TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ateles-tests.yml"

TRUST_BOUNDARY_GUIDANCE = "# Trusted default-branch code executes; candidate files cross only as validated data."
EXIT_CLASS_GUIDANCE = (
    "# Exit classes (class -> exit -> stable message):",
    "# untrusted_source -> job fail -> canonical measurement refuses code from fork repositories",
    "# boundary_rejected -> 2 -> canonical rule inventory input boundary rejected reason=",
    "# boundary_failed -> 2 -> rule inventory measurement failed before a safe verdict",
    "# instrument_incomplete -> 3 -> rule inventory equality unavailable: canonical read credential is missing",
    "# instrument_incomplete -> 3 -> rule inventory equality unavailable: canonical repository roots are missing",
    "# instrument_incomplete -> 3 -> rule inventory equality unavailable: full measurement is incomplete",
    "# inventory_mismatch -> 1 -> rule inventory differs from the complete canonical measurement",
    "# safety_failed -> 2 -> rule inventory public-output safety check failed",
    "# match -> 0 -> rule inventory matches the complete canonical measurement",
)
STAGE_ZERO_GUIDANCE = (
    "# Local reproduce requires Stage-0 docs/foundation/rule_inventory.md to already "
    "exist; the packager does not generate it (#1123; not #1114/#923)."
)
LOCAL_REPRODUCTION_GUIDANCE = (
    '# python3 execution/scripts/package_rule_inventory_inputs.py --source "$PWD" '
    '--destination "$INPUTS_DIR"',
    '# python3 execution/scripts/package_rule_inventory_inputs.py --validate "$INPUTS_DIR"',
    '# python3 execution/scripts/run_canonical_rule_inventory_gate.py "$INPUTS_DIR"',
)
CANONICAL_GUIDANCE_HEADER = "\n".join(
    (
        "name: canonical rule inventory",
        "",
        TRUST_BOUNDARY_GUIDANCE,
        *EXIT_CLASS_GUIDANCE,
        STAGE_ZERO_GUIDANCE,
        *LOCAL_REPRODUCTION_GUIDANCE,
    )
)
CANONICAL_GUIDANCE_TRANSITION = (
    CANONICAL_GUIDANCE_HEADER + "\non:\n  pull_request_target:\n"
)
ALLOWED_OPERATIONAL_COMMENTS = (
    "# RULE_INVENTORY_CANONICAL_REPOSITORY_ROOTS is supplied by the runner",
    "# service environment. It is never copied into repository configuration",
    "# or printed. The wrapper captures all renderer output and emits only a",
    "# stable exit-class message.",
)


class UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses last-key-wins ambiguity."""


def _construct_unique_mapping(
    loader: UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise AssertionError("workflow contains an invalid mapping key") from exc
        if duplicate:
            raise AssertionError(f"workflow contains duplicate mapping key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _yaml_comment(line: str) -> str:
    """Return a possible YAML comment suffix, conservatively fail-closed.

    YAML quote context depends on scalar position, not merely on seeing a quote
    character: a quote embedded in a plain scalar is literal and does not hide
    the following comment.  The canonical workflow needs no post-guidance hash
    literals, so treating every separated ``#`` as a comment avoids maintaining
    a second, incomplete YAML lexer here.
    """
    for index, character in enumerate(line):
        if character == "#" and (index == 0 or line[index - 1].isspace()):
            return line[index:]
    return ""


def parse_workflow(workflow: str) -> tuple[dict[str, object], dict[str, dict]]:
    """Parse the workflow as data and reject ambiguous execution shapes."""

    try:
        document = yaml.load(workflow, Loader=UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise AssertionError("workflow is not valid YAML") from exc
    if not isinstance(document, dict):
        raise AssertionError("workflow root is not a mapping")
    allowed_root_keys = {"name", True, "permissions", "jobs"}
    if set(document) - allowed_root_keys:
        raise AssertionError("workflow root has an unsafe execution control")
    triggers = document.get(True)
    if not isinstance(triggers, dict) or set(triggers) != {"pull_request_target"}:
        raise AssertionError("privileged workflow trigger is not default-branch-only")
    pull_request_target = triggers["pull_request_target"]
    if (
        not isinstance(pull_request_target, dict)
        or pull_request_target.get("types") != ["opened", "reopened", "synchronize"]
        or not isinstance(pull_request_target.get("paths"), list)
    ):
        raise AssertionError("privileged workflow trigger is not structurally pinned")
    if document.get("permissions") != {"contents": "read"}:
        raise AssertionError("workflow permissions are not read-only")
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        raise AssertionError("workflow has no structural jobs mapping")
    allowed_job_keys = {
        "name",
        "needs",
        "runs-on",
        "timeout-minutes",
        "steps",
        "env",
        "if",
        "continue-on-error",
    }
    allowed_step_keys = {
        "name",
        "uses",
        "with",
        "run",
        "env",
        "if",
        "continue-on-error",
    }
    for job_name, job in jobs.items():
        if not isinstance(job_name, str) or not isinstance(job, dict):
            raise AssertionError("workflow job is not a named mapping")
        if set(job) - allowed_job_keys:
            raise AssertionError(f"job {job_name} has an unsafe execution control")
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            raise AssertionError(f"job {job_name} has no structural steps list")
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise AssertionError(f"job {job_name} step {index} is not a mapping")
            if set(step) - allowed_step_keys:
                raise AssertionError(
                    f"job {job_name} step {index} has an unsafe execution control"
                )
            forms = [key for key in ("uses", "run") if key in step]
            if len(forms) != 1:
                raise AssertionError(
                    f"job {job_name} step {index} has an ambiguous execution form"
                )
            form = forms[0]
            if not isinstance(step[form], str) or not step[form].strip():
                raise AssertionError(
                    f"job {job_name} step {index} has an invalid {form} form"
                )
            if "with" in step and (
                form != "uses" or not isinstance(step["with"], dict)
            ):
                raise AssertionError(
                    f"job {job_name} step {index} has an invalid with mapping"
                )
            if "env" in step and not isinstance(step["env"], dict):
                raise AssertionError(
                    f"job {job_name} step {index} has an invalid env mapping"
                )
        if "env" in job and not isinstance(job["env"], dict):
            raise AssertionError(f"job {job_name} has an invalid env mapping")
    return document, jobs


def _needs(job: dict, dependency: str) -> bool:
    needs = job.get("needs")
    if isinstance(needs, str):
        return needs == dependency
    return isinstance(needs, list) and needs == [dependency]


def _command(step: dict) -> str:
    return str(step["run"]).strip()


def _checkout_steps(jobs: dict[str, dict]):
    for job_name, job in jobs.items():
        for index, step in enumerate(job["steps"]):
            if step.get("uses") == "actions/checkout@v4":
                yield job_name, index, step


def verify_privileged_boundary(workflow: str) -> None:
    document, jobs = parse_workflow(workflow)
    if set(jobs) != {"trusted-source", "candidate-inputs", "rule-inventory"}:
        raise AssertionError("unexpected workflow job set")
    trusted_source = jobs["trusted-source"]
    candidate = jobs["candidate-inputs"]
    privileged = jobs["rule-inventory"]
    refusal_steps = [
        step
        for step in trusted_source["steps"]
        if step.get("name") == "Refuse fork repositories before privileged execution"
    ]
    expected_refusal = (
        'if [ "$HEAD_REPOSITORY" != "$GITHUB_REPOSITORY" ]; then\n'
        '  echo "::error::canonical measurement refuses code from fork repositories"\n'
        "  exit 1\n"
        "fi"
    )
    if (
        len(trusted_source["steps"]) != 1
        or len(refusal_steps) != 1
        or "if" in trusted_source
        or "continue-on-error" in trusted_source
        or "if" in refusal_steps[0]
        or "continue-on-error" in refusal_steps[0]
        or _command(refusal_steps[0]) != expected_refusal
        or refusal_steps[0].get("env")
        != {"HEAD_REPOSITORY": "${{ github.event.pull_request.head.repo.full_name }}"}
        or not _needs(candidate, "trusted-source")
    ):
        raise AssertionError("fork refusal is not binding before candidate admission")
    if privileged.get("runs-on") != [
        "self-hosted",
        "macOS",
        "canonical-rule-inventory",
    ]:
        raise AssertionError("canonical job is not pinned to its runner class")
    if not _needs(privileged, "candidate-inputs"):
        raise AssertionError("privileged job can bypass candidate-data admission")

    candidate_checkouts = [
        (index, step)
        for job_name, index, step in _checkout_steps(jobs)
        if job_name == "candidate-inputs"
    ]
    trusted_packer_checkouts = [
        (index, step)
        for index, step in candidate_checkouts
        if step.get("with", {}).get("path") == "trusted"
    ]
    expected_trusted_checkout = {
        "ref": "${{ github.event.repository.default_branch }}",
        "path": "trusted",
        "persist-credentials": False,
    }
    if (
        len(trusted_packer_checkouts) != 1
        or trusted_packer_checkouts[0][1].get("with") != expected_trusted_checkout
    ):
        raise AssertionError(
            "candidate packer checkout is not exactly one trusted checkout"
        )
    packer_command = (
        "python3 trusted/execution/scripts/package_rule_inventory_inputs.py \\\n"
        '  --source "$GITHUB_WORKSPACE/candidate" \\\n'
        '  --destination "$GITHUB_WORKSPACE/candidate-inputs"'
    )
    packer_steps = [
        (index, step)
        for index, step in enumerate(candidate["steps"])
        if "run" in step and _command(step) == packer_command
    ]
    if len(packer_steps) != 1 or packer_steps[0][0] <= trusted_packer_checkouts[0][0]:
        raise AssertionError("candidate packer checkout does not bind the packer run")
    if not any(
        step.get("with", {}).get("ref") == "${{ github.event.pull_request.head.sha }}"
        and step.get("with", {}).get("path") == "candidate"
        for _, step in candidate_checkouts
    ):
        raise AssertionError("candidate inputs are not bound to the declared PR head")

    for _, _, checkout in _checkout_steps(jobs):
        checkout_with = checkout.get("with")
        if (
            not isinstance(checkout_with, dict)
            or checkout_with.get("persist-credentials") is not False
        ):
            raise AssertionError("a checkout can retain persisted credentials")

    allowed_actions = {
        "candidate-inputs": {
            "actions/checkout@v4",
            "actions/setup-python@v5",
            "actions/upload-artifact@v4",
        },
        "rule-inventory": {
            "actions/checkout@v4",
            "actions/setup-python@v5",
            "actions/download-artifact@v4",
        },
    }
    allowed_runs = {
        "candidate-inputs": {packer_command},
        "rule-inventory": {
            "python3 trusted/execution/scripts/package_rule_inventory_inputs.py "
            '\\\n  --validate "$GITHUB_WORKSPACE/candidate-inputs"',
            "python3 trusted/execution/scripts/run_canonical_rule_inventory_gate.py "
            '\\\n  "$GITHUB_WORKSPACE/candidate-inputs"',
        },
    }
    for job_name in ("candidate-inputs", "rule-inventory"):
        if "if" in jobs[job_name] or "continue-on-error" in jobs[job_name]:
            raise AssertionError(f"job {job_name} has an unsafe execution control")
        for step in jobs[job_name]["steps"]:
            if "if" in step or "continue-on-error" in step:
                raise AssertionError(
                    f"job {job_name} step has an unsafe execution control"
                )
            if "uses" in step and step["uses"] not in allowed_actions[job_name]:
                message = (
                    "privileged job invokes an unapproved action"
                    if job_name == "rule-inventory"
                    else "candidate packer invokes an unapproved action"
                )
                raise AssertionError(message)
            if "run" in step and _command(step) not in allowed_runs[job_name]:
                message = (
                    "privileged job can execute a non-trusted command"
                    if job_name == "rule-inventory"
                    else "candidate packer can execute a non-trusted command"
                )
                raise AssertionError(message)

    if len(candidate_checkouts) != 2:
        raise AssertionError("candidate packer checkout set is not exact")
    candidate_setup = [
        step
        for step in candidate["steps"]
        if step.get("uses") == "actions/setup-python@v5"
    ]
    candidate_upload = [
        step
        for step in candidate["steps"]
        if step.get("uses") == "actions/upload-artifact@v4"
    ]
    if len(candidate_setup) != 1 or candidate_setup[0].get("with") != {
        "python-version": "3.13"
    }:
        raise AssertionError("candidate packer Python action is not exact")
    if len(candidate_upload) != 1 or candidate_upload[0].get("with") != {
        "name": "canonical-rule-inventory-inputs",
        "path": "candidate-inputs",
        "include-hidden-files": True,
        "if-no-files-found": "error",
        "retention-days": 1,
    }:
        raise AssertionError("candidate artifact action is not exact")
    privileged_checkouts = [
        step
        for job_name, _, step in _checkout_steps(jobs)
        if job_name == "rule-inventory"
    ]
    if (
        len(privileged_checkouts) != 1
        or privileged_checkouts[0].get("with") != expected_trusted_checkout
    ):
        raise AssertionError("privileged checkout is not the trusted default branch")
    privileged_setup = [
        step
        for step in privileged["steps"]
        if step.get("uses") == "actions/setup-python@v5"
    ]
    privileged_download = [
        step
        for step in privileged["steps"]
        if step.get("uses") == "actions/download-artifact@v4"
    ]
    if len(privileged_setup) != 1 or privileged_setup[0].get("with") != {
        "python-version": "3.13"
    }:
        raise AssertionError("privileged Python action is not exact")
    if len(privileged_download) != 1 or privileged_download[0].get("with") != {
        "name": "canonical-rule-inventory-inputs",
        "path": "candidate-inputs",
    }:
        raise AssertionError("privileged artifact action is not exact")

    token_locations: list[tuple[str, object]] = []
    for scope_name, scope in [("workflow", document), *jobs.items()]:
        env = scope.get("env") if isinstance(scope, dict) else None
        if env is not None:
            if not isinstance(env, dict):
                raise AssertionError(f"{scope_name} has an invalid env mapping")
            if "NEOTOMA_BEARER_TOKEN" in env:
                token_locations.append(
                    (f"{scope_name}.env", env["NEOTOMA_BEARER_TOKEN"])
                )
    gate_steps = []
    for job_name, job in jobs.items():
        for index, step in enumerate(job["steps"]):
            env = step.get("env", {})
            if "NEOTOMA_BEARER_TOKEN" in env:
                token_locations.append(
                    (f"{job_name}.steps[{index}].env", env["NEOTOMA_BEARER_TOKEN"])
                )
                gate_steps.append(step)
    if (
        len(token_locations) != 1
        or len(gate_steps) != 1
        or gate_steps[0].get("name") != "Rule inventory — full measured output matches"
        or gate_steps[0].get("env")
        != {"NEOTOMA_BEARER_TOKEN": "${{ secrets.NEOTOMA_BEARER_TOKEN }}"}
        or token_locations[0][1] != "${{ secrets.NEOTOMA_BEARER_TOKEN }}"
        or workflow.count("${{ secrets.NEOTOMA_BEARER_TOKEN }}") != 1
    ):
        raise AssertionError(
            "canonical credential is exposed outside one measurement step"
        )
    allowed_env_steps = {id(refusal_steps[0]), id(gate_steps[0])}
    for job in jobs.values():
        for step in job["steps"]:
            if "env" in step and id(step) not in allowed_env_steps:
                raise AssertionError(
                    "workflow step can alter the trusted execution env"
                )
    if "env" in document or any("env" in job for job in jobs.values()):
        raise AssertionError("workflow scope can alter the trusted execution env")
    if "vars.RULE_INVENTORY_CANONICAL_REPOSITORY_ROOTS" in workflow:
        raise AssertionError(
            "private canonical roots moved into repository configuration"
        )


def verify_candidate_artifact_transfer(workflow: str) -> None:
    """The curated data tree must cross the artifact boundary intact.

    ``actions/upload-artifact`` excludes hidden files unless callers opt in.
    Both the manifest that binds the candidate tree and the ``.claude`` rule
    stores are hidden, so omitting this setting makes every real measurement
    incomplete even though the packer and validator are individually sound.
    """

    _, jobs = parse_workflow(workflow)
    uploads = [
        step
        for step in jobs["candidate-inputs"]["steps"]
        if step.get("uses") == "actions/upload-artifact@v4"
    ]
    if len(uploads) != 1:
        raise AssertionError("candidate input artifact upload step is missing")
    upload_with = uploads[0].get("with", {})
    if upload_with.get("path") != "candidate-inputs":
        raise AssertionError("artifact upload is not limited to the curated data tree")
    if upload_with.get("include-hidden-files") is not True:
        raise AssertionError("candidate artifact upload excludes hidden inputs")


def verify_workflow_guidance(workflow: str) -> None:
    if not workflow.startswith(CANONICAL_GUIDANCE_TRANSITION):
        raise AssertionError(
            "workflow guidance is incomplete, contradictory, or reordered"
        )
    trigger_start = len(CANONICAL_GUIDANCE_HEADER) + 1
    permissions_boundary = workflow.find("\npermissions:\n", trigger_start)
    if permissions_boundary < 0:
        raise AssertionError("workflow guidance trigger region is incomplete")
    trigger_region = workflow[trigger_start:permissions_boundary]
    if any(_yaml_comment(line) for line in trigger_region.splitlines()):
        raise AssertionError("workflow guidance appears inside the trigger region")

    comments_outside_guidance = [
        comment
        for line in workflow[trigger_start:].splitlines()
        if (comment := _yaml_comment(line))
    ]
    if comments_outside_guidance != list(ALLOWED_OPERATIONAL_COMMENTS):
        raise AssertionError(
            "workflow guidance or an unapproved comment appears outside its "
            "designated region"
        )


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

    def test_inline_privileged_run_is_also_rejected(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        prefix, privileged = workflow.split("  rule-inventory:\n", 1)
        planted_privileged = privileged.replace(
            "      - uses: actions/setup-python@v5\n",
            "      - name: planted inline candidate execution\n"
            "        run: python3 candidate-inputs/attacker_controlled.py\n"
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

    def test_fork_refusal_cannot_be_skipped_or_continued(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutations = {
            "step if": (
                "      - name: Refuse fork repositories before privileged execution\n",
                "      - name: Refuse fork repositories before privileged execution\n"
                "        if: ${{ false }}\n",
            ),
            "step continue": (
                "      - name: Refuse fork repositories before privileged execution\n",
                "      - name: Refuse fork repositories before privileged execution\n"
                "        continue-on-error: true\n",
            ),
            "job if": (
                "  trusted-source:\n    name: canonical measurement source is trusted\n",
                "  trusted-source:\n    name: canonical measurement source is trusted\n"
                "    if: ${{ false }}\n",
            ),
            "job continue": (
                "  trusted-source:\n    name: canonical measurement source is trusted\n",
                "  trusted-source:\n    name: canonical measurement source is trusted\n"
                "    continue-on-error: true\n",
            ),
        }
        for label, (before, after) in mutations.items():
            with self.subTest(label=label):
                mutant = workflow.replace(before, after, 1)
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

    def test_trusted_packer_checkout_cannot_be_overwritten_later(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            "      - uses: actions/setup-python@v5\n",
            "      - name: Overwrite trusted packer with candidate code\n"
            "        uses: actions/checkout@v4\n"
            "        with:\n"
            "          ref: ${{ github.event.pull_request.head.sha }}\n"
            "          path: trusted\n"
            "          persist-credentials: false\n"
            "      - uses: actions/setup-python@v5\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        with self.assertRaisesRegex(AssertionError, "packer checkout"):
            verify_privileged_boundary(mutant)

    def test_every_checkout_must_disable_persisted_credentials(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        declared_candidate = (
            "      - name: Check out declared candidate inputs\n"
            "        uses: actions/checkout@v4\n"
            "        with:\n"
            "          ref: ${{ github.event.pull_request.head.sha }}\n"
            "          path: candidate\n"
            "          persist-credentials: false\n"
        )
        mutant = workflow.replace(
            declared_candidate,
            declared_candidate.replace(
                "persist-credentials: false", "persist-credentials: true"
            ),
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        with self.assertRaisesRegex(AssertionError, "persisted credentials"):
            verify_privileged_boundary(mutant)

    def test_canonical_token_exists_only_on_the_gate_step(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            "  candidate-inputs:\n    name: package candidate inputs as data\n",
            "  candidate-inputs:\n    name: package candidate inputs as data\n"
            "    env:\n"
            "      NEOTOMA_BEARER_TOKEN: ${{ secrets.NEOTOMA_BEARER_TOKEN }}\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        with self.assertRaisesRegex(AssertionError, "canonical credential"):
            verify_privileged_boundary(mutant)

    def test_workflow_guidance_fails_red_when_exit_class_is_removed(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace("# boundary_rejected", "# boundary-removed", 1)
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        with self.assertRaisesRegex(AssertionError, "guidance is incomplete"):
            verify_workflow_guidance(mutant)

    def test_exit_mapping_cannot_be_masked_by_a_relocated_machine_string(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            "# Trusted default-branch code executes; candidate files cross only as validated data.\n",
            "# Trusted default-branch code executes; candidate files cross only as validated data; "
            "canonical rule inventory input boundary rejected reason=.\n",
            1,
        ).replace(
            "# boundary_rejected -> 2 -> canonical rule inventory input boundary rejected reason=\n",
            "# boundary_rejected -> 0 -> rule inventory matches the complete canonical measurement\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        self.assertNotIn(EXIT_CLASS_GUIDANCE[2], mutant.splitlines())
        with self.assertRaisesRegex(AssertionError, "guidance"):
            verify_workflow_guidance(mutant)

    def test_stage_zero_contradiction_cannot_be_masked_by_relocated_words(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            "# Trusted default-branch code executes; candidate files cross only as validated data.\n",
            "# Trusted default-branch code executes; candidate files cross only as validated data; "
            "does not generate.\n",
            1,
        ).replace(
            STAGE_ZERO_GUIDANCE + "\n",
            "# Local reproduce treats Stage-0 docs/foundation/rule_inventory.md as optional "
            "and generates it (#1123; not #1114/#923).\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        self.assertNotIn(STAGE_ZERO_GUIDANCE, mutant.splitlines())
        with self.assertRaisesRegex(AssertionError, "guidance"):
            verify_workflow_guidance(mutant)

    def test_indented_boundary_mapping_contradiction_is_rejected(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            "on:\n",
            "  # boundary_rejected -> 0 -> rule inventory matches the complete "
            "canonical measurement\n"
            "on:\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        yaml.safe_load(mutant)
        with self.assertRaisesRegex(AssertionError, "guidance"):
            verify_workflow_guidance(mutant)

    def test_indented_stage_zero_contradiction_is_rejected(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            "on:\n",
            "  # Local reproduce may generate the Stage-0 "
            "docs/foundation/rule_inventory.md input\n"
            "on:\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        yaml.safe_load(mutant)
        with self.assertRaisesRegex(AssertionError, "guidance"):
            verify_workflow_guidance(mutant)

    def test_inline_on_boundary_mapping_contradiction_is_rejected(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            "on:\n",
            "on: # boundary_rejected -> 0 -> rule inventory matches the "
            "complete canonical measurement\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        yaml.safe_load(mutant)
        verify_privileged_boundary(mutant)
        with self.assertRaisesRegex(AssertionError, "guidance"):
            verify_workflow_guidance(mutant)

    def test_inline_on_stage_zero_contradiction_is_rejected(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            "on:\n",
            "on: # Local reproduce may generate the Stage-0 "
            "docs/foundation/rule_inventory.md input\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        yaml.safe_load(mutant)
        verify_privileged_boundary(mutant)
        with self.assertRaisesRegex(AssertionError, "guidance"):
            verify_workflow_guidance(mutant)

    def test_indented_comment_after_on_transition_is_rejected(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            "on:\n  pull_request_target:\n",
            "on:\n"
            "  # boundary_rejected -> 0 -> rule inventory matches the complete "
            "canonical measurement\n"
            "  pull_request_target:\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        yaml.safe_load(mutant)
        verify_privileged_boundary(mutant)
        with self.assertRaisesRegex(AssertionError, "guidance"):
            verify_workflow_guidance(mutant)

    def test_duplicate_on_key_is_rejected_before_last_key_can_win(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        on_block = workflow.split("on:\n", 1)[1].split("\npermissions:\n", 1)[0]
        mutant = workflow.replace(
            "\npermissions:\n",
            f"\non:\n{on_block}\npermissions:\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        yaml.safe_load(mutant)
        verify_workflow_guidance(mutant)
        with self.assertRaisesRegex(AssertionError, "duplicate mapping key"):
            parse_workflow(mutant)

    def test_post_trigger_boundary_mapping_contradiction_is_rejected(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            "  pull_request_target:\n",
            "  pull_request_target:\n"
            "    # boundary_rejected -> 0 -> rule inventory matches the "
            "complete canonical measurement\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        yaml.safe_load(mutant)
        verify_privileged_boundary(mutant)
        with self.assertRaisesRegex(AssertionError, "guidance"):
            verify_workflow_guidance(mutant)

    def test_post_trigger_stage_zero_contradiction_is_rejected(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            "  pull_request_target:\n",
            "  pull_request_target:\n"
            "    # Local reproduce may generate the Stage-0 "
            "docs/foundation/rule_inventory.md input\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        yaml.safe_load(mutant)
        verify_privileged_boundary(mutant)
        with self.assertRaisesRegex(AssertionError, "guidance"):
            verify_workflow_guidance(mutant)

    def test_protected_guidance_comment_outside_designated_region_is_rejected(
        self,
    ) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutant = workflow.replace(
            "jobs:\n",
            "jobs:\n"
            "  # Local reproduce may generate the Stage-0 "
            "docs/foundation/rule_inventory.md input\n",
            1,
        )
        self.assertNotEqual(workflow, mutant, "negative mutation was not planted")
        yaml.safe_load(mutant)
        verify_privileged_boundary(mutant)
        with self.assertRaisesRegex(AssertionError, "guidance"):
            verify_workflow_guidance(mutant)

    def test_plain_scalar_quote_cannot_hide_a_guidance_comment(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        mutations = {
            "double quote": (
                '    name: package candidate inputs as data" '
                "# Local reproduce may generate the Stage-0 input\n"
            ),
            "single quote": (
                "    name: package candidate inputs as data' "
                "# boundary_rejected -> 0 -> match\n"
            ),
        }
        for label, replacement in mutations.items():
            with self.subTest(label=label):
                mutant = workflow.replace(
                    "    name: package candidate inputs as data\n",
                    replacement,
                    1,
                )
                self.assertNotEqual(
                    workflow, mutant, "negative mutation was not planted"
                )
                yaml.safe_load(mutant)
                verify_privileged_boundary(mutant)
                with self.assertRaisesRegex(AssertionError, "guidance"):
                    verify_workflow_guidance(mutant)

    def test_workflow_guidance_uses_exact_machine_messages(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        header = workflow.split("\non:", 1)[0]
        exact_messages = {
            "canonical measurement refuses code from fork repositories",
            "canonical rule inventory input boundary rejected reason=",
            "rule inventory measurement failed before a safe verdict",
            "rule inventory equality unavailable: canonical read credential is missing",
            "rule inventory equality unavailable: canonical repository roots are missing",
            "rule inventory equality unavailable: full measurement is incomplete",
            "rule inventory differs from the complete canonical measurement",
            "rule inventory public-output safety check failed",
            "rule inventory matches the complete canonical measurement",
        }
        self.assertEqual(
            [], sorted(message for message in exact_messages if message not in header)
        )

    def test_local_reproduction_discloses_stage_zero_input_dependency(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        header = workflow.split("\non:", 1)[0]
        self.assertIn("docs/foundation/rule_inventory.md", header)
        self.assertIn("does not generate", header)

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
