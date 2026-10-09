#!/usr/bin/env python3
"""Tests for the ateles#1333 split of the canonical rule inventory.

Three things are pinned here, each red before the change and green after:

1. **The old coupling stays gone.** No workflow triggered by a pull request may
   run a job on a self-hosted runner. On the commit before #1333 this failed on
   `canonical-rule-inventory.yml`, whose measurement job needed the operator's
   macOS runner; the planted copy of that shape below keeps the detector honest.
2. **The replacement is hosted and unprivileged.** `rule-sources.yml` runs on
   `pull_request` (never `pull_request_target`), on GitHub-hosted runners, with
   a read-only token and no secrets, and runs each public check by name.
3. **The public-source check binds.** `check_public_rule_sources.py` passes on
   this checkout and fails on a planted source that parses to no rules, on a
   missing store, and on a private store being read.

The private audit's two flags the runbook relies on (`--output`,
`--require-complete-measurement`) are also pinned to the renderer, so the
runbook cannot name a flag the instrument does not have.
"""

from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path

import yaml

import check_public_rule_sources as checker
import render_rule_inventory as renderer

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RULE_SOURCES_WORKFLOW = WORKFLOWS / "rule-sources.yml"
ATELES_TESTS_WORKFLOW = WORKFLOWS / "ateles-tests.yml"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "rule_inventory_audit.md"

PR_EVENTS = frozenset({"pull_request", "pull_request_target"})

# The shape of the retired job, kept as a planted negative.
RETIRED_SELF_HOSTED_WORKFLOW = """
name: canonical rule inventory
on:
  pull_request_target:
    types: [opened, reopened, synchronize]
permissions:
  contents: read
jobs:
  rule-inventory:
    name: canonical rule inventory
    runs-on: [self-hosted, macOS, canonical-rule-inventory]
    steps:
      - run: echo measure
"""

# Public rule sources a PR can change; each must trigger the hosted workflow.
REQUIRED_TRIGGER_PATHS = (
    "CLAUDE.md",
    ".claude/settings.json",
    ".claude/hooks/**",
    ".claude/skills/**",
    ".codex/**",
    "lib/daemon_runtime/agent_loader.py",
    "lib/daemon_runtime/policy_skill_renderer.py",
    "execution/evals/rule_delivery/**",
    "execution/scripts/check_public_rule_sources.py",
    ".github/workflows/rule-sources.yml",
)

# What the hosted workflow must actually run.
REQUIRED_COMMANDS = (
    "execution/scripts/check_public_rule_sources.py",
    ".claude/hooks/hook_wiring_reference.py --check",
    ".claude/hooks/test_session_rule_index.py",
    ".claude/hooks/test_session_rule_delivery.py",
    ".claude/hooks/test_rule_injection_gate.py",
    "lib/daemon_runtime/test_agent_loader.py",
    "lib/daemon_runtime/test_policy_skill_renderer.py",
    "execution/scripts/test_codex_rule_hooks.py",
    "execution/evals/rule_delivery/",
    "execution/scripts/test_public_rule_sources.py",
)


def _load(text: str) -> dict:
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise AssertionError("workflow is not a mapping")
    return document


def _triggers(document: dict) -> set[str]:
    # PyYAML reads a bare `on:` key as boolean True (YAML 1.1).
    on = document.get("on", document.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(item) for item in on}
    if isinstance(on, dict):
        return {str(key) for key in on}
    return set()


def _runs_on_labels(job: dict) -> list[str]:
    runs_on = job.get("runs-on", [])
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, list):
        return [str(label) for label in runs_on]
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels", [])
        group = runs_on.get("group")
        out = [labels] if isinstance(labels, str) else [str(x) for x in labels]
        # A runner group is how GitHub routes to self-hosted/larger runners.
        return out + ([f"group:{group}"] if group else [])
    return []


def self_hosted_pr_jobs(workflow_text: str, label: str) -> list[str]:
    """Return `<workflow>:<job>` for each PR-triggered job not on a hosted runner."""
    document = _load(workflow_text)
    if not (_triggers(document) & PR_EVENTS):
        return []
    found = []
    for job_id, job in (document.get("jobs") or {}).items():
        job = job or {}
        if "uses" in job:
            continue  # a reusable-workflow call; the called workflow is checked itself
        labels = _runs_on_labels(job)
        hosted_image = bool(labels) and all(
            lbl.lower().startswith(("ubuntu-", "windows-", "macos-")) for lbl in labels
        )
        if any(lbl.lower() == "self-hosted" for lbl in labels) or not hosted_image:
            found.append(f"{label}:{job_id}")
    return found


class NoSelfHostedPullRequestJobTest(unittest.TestCase):
    def test_no_pull_request_workflow_needs_a_self_hosted_runner(self) -> None:
        offenders = []
        for path in sorted(WORKFLOWS.glob("*.y*ml")):
            offenders += self_hosted_pr_jobs(
                path.read_text(encoding="utf-8"), path.name
            )
        self.assertEqual(
            offenders, [], "PR-gating jobs must run on GitHub-hosted runners"
        )

    def test_detector_rejects_the_retired_inventory_job(self) -> None:
        self.assertEqual(
            self_hosted_pr_jobs(RETIRED_SELF_HOSTED_WORKFLOW, "retired.yml"),
            ["retired.yml:rule-inventory"],
        )

    def test_detector_rejects_a_runner_group_and_an_expression(self) -> None:
        for runs_on in ("{group: private-pool}", "${{ matrix.runner }}"):
            text = RETIRED_SELF_HOSTED_WORKFLOW.replace(
                "[self-hosted, macOS, canonical-rule-inventory]", runs_on
            )
            self.assertEqual(
                self_hosted_pr_jobs(text, "planted.yml"), ["planted.yml:rule-inventory"]
            )

    def test_detector_ignores_non_pr_workflows(self) -> None:
        text = RETIRED_SELF_HOSTED_WORKFLOW.replace(
            "pull_request_target:\n    types: [opened, reopened, synchronize]",
            "workflow_dispatch:",
        )
        self.assertEqual(self_hosted_pr_jobs(text, "manual.yml"), [])

    def test_retired_workflow_is_absent(self) -> None:
        self.assertFalse((WORKFLOWS / "canonical-rule-inventory.yml").exists())


class RuleSourcesWorkflowPostureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = RULE_SOURCES_WORKFLOW.read_text(encoding="utf-8")
        self.document = _load(self.text)

    def test_runs_on_pull_request_never_pull_request_target(self) -> None:
        triggers = _triggers(self.document)
        self.assertIn("pull_request", triggers)
        self.assertNotIn("pull_request_target", triggers)
        self.assertNotIn("pull_request_target", self.text.split("\non:", 1)[1])

    def test_token_is_read_only_and_no_secret_is_referenced(self) -> None:
        self.assertEqual(self.document.get("permissions"), {"contents": "read"})
        for job in self.document["jobs"].values():
            self.assertNotIn("permissions", job)
        self.assertNotIn("secrets.", self.text)

    def test_every_job_is_github_hosted(self) -> None:
        for job_id, job in self.document["jobs"].items():
            self.assertEqual(job.get("runs-on"), "ubuntu-latest", job_id)

    def test_checkout_does_not_persist_credentials(self) -> None:
        for job in self.document["jobs"].values():
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("actions/checkout"):
                    self.assertIs(
                        step.get("with", {}).get("persist-credentials"), False
                    )

    def test_runs_each_public_check(self) -> None:
        commands = "\n".join(
            str(step.get("run", ""))
            for job in self.document["jobs"].values()
            for step in job.get("steps", [])
        )
        for command in REQUIRED_COMMANDS:
            self.assertIn(command, commands)

    def test_public_rule_sources_trigger_it(self) -> None:
        paths = self.document.get("on", self.document.get(True))["pull_request"][
            "paths"
        ]
        for required in REQUIRED_TRIGGER_PATHS:
            self.assertIn(required, paths)

    def test_workflow_edits_rerun_the_pytest_lane(self) -> None:
        document = _load(ATELES_TESTS_WORKFLOW.read_text(encoding="utf-8"))
        paths = document.get("on", document.get(True))["pull_request"]["paths"]
        self.assertIn(".github/workflows/**", paths)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_public_sources(root: Path) -> None:
    _write(
        root / "CLAUDE.md",
        "# Rules\n\n- **Never skip the check.** Run it every time.\n",
    )
    _write(
        root / ".claude" / "skills" / "example" / "SKILL.md",
        "# Example\n\n- **Always read back.** NEVER trust a success code alone.\n",
    )
    _write(
        root / ".claude" / "hooks" / "example_guard.py",
        '"""Guard.\n\nNever run the forbidden command in any form, in any session.\n"""\n',
    )


class PublicRuleSourcesCheckTest(unittest.TestCase):
    def run_checker(self, root: Path) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = checker.main(["--root", str(root)])
        return code, stdout.getvalue()

    def test_this_checkout_parses(self) -> None:
        code, out = self.run_checker(REPO_ROOT)
        self.assertEqual(code, 0, out)
        self.assertIn("public rule sources parse", out)

    def test_planted_sources_parse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            make_public_sources(Path(directory))
            code, out = self.run_checker(Path(directory))
        self.assertEqual(code, 0, out)

    def test_a_source_with_no_rules_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_public_sources(root)
            _write(root / "CLAUDE.md", "# Rules\n\nNothing normative here.\n")
            code, out = self.run_checker(root)
        self.assertEqual(code, 1, out)
        self.assertIn("ateles/CLAUDE.md: parsed to zero rule statements", out)

    def test_a_missing_store_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_public_sources(root)
            (root / ".claude" / "hooks" / "example_guard.py").unlink()
            code, out = self.run_checker(root)
        self.assertEqual(code, 1, out)
        self.assertIn("Claude Code hooks (ateles): not found", out)

    def test_a_private_store_being_read_fails(self) -> None:
        stores = [
            renderer.Store(name=name, location="", populated=1, statements=1)
            for name in checker.PUBLIC_RULE_STORES
        ]
        stores.append(
            renderer.Store(name="Codex", location="", populated=1, statements=3)
        )
        self.assertEqual(
            checker.problems_in(stores),
            ["Codex: a store outside the public set was read"],
        )

    def test_canonical_roots_variable_cannot_widen_the_read(self) -> None:
        saved = os.environ.get(renderer.CANONICAL_REPOSITORY_ROOTS_ENV)
        os.environ[renderer.CANONICAL_REPOSITORY_ROOTS_ENV] = str(REPO_ROOT)
        try:
            stores = checker.measure_public_stores(REPO_ROOT)
        finally:
            if saved is None:
                os.environ.pop(renderer.CANONICAL_REPOSITORY_ROOTS_ENV, None)
            else:
                os.environ[renderer.CANONICAL_REPOSITORY_ROOTS_ENV] = saved
        self.assertEqual(checker.problems_in(stores), [])
        self.assertEqual(os.environ.get(renderer.CANONICAL_REPOSITORY_ROOTS_ENV), saved)

    def test_output_names_no_rule_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            make_public_sources(Path(directory))
            _code, out = self.run_checker(Path(directory))
        self.assertNotIn("forbidden command", out)
        self.assertNotIn("trust a success code", out)


class PrivateAuditInstrumentTest(unittest.TestCase):
    def test_private_output_is_refused_inside_the_repository(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            renderer.write_private_output(REPO_ROOT / "docs" / "audit.md", "x")

    def test_private_output_is_written_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "rule_inventory.md"
            renderer.write_private_output(target, "render\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "render\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_runbook_flags_exist_on_the_renderer(self) -> None:
        runbook = RUNBOOK.read_text(encoding="utf-8")
        source = (
            REPO_ROOT / "execution" / "scripts" / "render_rule_inventory.py"
        ).read_text(encoding="utf-8")
        for flag in (
            "--require-complete-measurement",
            "--output",
            "--private-diagnostics",
        ):
            self.assertIn(flag, runbook)
            self.assertIn(f'"{flag}"', source)


if __name__ == "__main__":
    unittest.main()
