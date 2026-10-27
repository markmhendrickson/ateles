#!/usr/bin/env python3
"""Structural and behavioural checks for templates/app-ci/*.yml.

Structural: every checkout drops credentials unless it is the one documented
exception; non-PR concurrency groups are unique per push; every placeholder
slot fails; the audit workflow runs the script and nothing swallows its exit.

Behavioural: the summary jobs' and the wait-for-CI step's `run:` blocks are
extracted and executed with bash (jq required; skipped when absent). The wait
step runs against a fake `gh` on PATH that serves canned check-run JSON.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

# APP_CI_TEMPLATES_DIR lets a reviewer point the suite at an older copy of the
# templates to confirm the tests go red against them.
TEMPLATES = Path(
    os.environ.get("APP_CI_TEMPLATES_DIR") or Path(__file__).resolve().parents[2] / "templates" / "app-ci"
)
BASH = shutil.which("bash")
JQ = shutil.which("jq")


def load(name):
    return yaml.safe_load((TEMPLATES / name).read_text())


def steps_of(workflow):
    for job_id, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            yield job_id, step


def step_named(workflow, job_id, fragment):
    for step in workflow["jobs"][job_id]["steps"]:
        if fragment in step.get("name", ""):
            return step
    raise AssertionError(f"no step containing {fragment!r} in job {job_id}")


class StructureTests(unittest.TestCase):
    FILES = ["ci.yml", "nightly.yml", "deploy-guard.yml", "trunk-audit.yml"]

    def test_checkouts_drop_credentials_except_the_documented_one(self):
        for name in self.FILES:
            for job_id, step in steps_of(load(name)):
                if str(step.get("uses", "")).startswith("actions/checkout"):
                    persists = step.get("with", {}).get("persist-credentials")
                    if (name, job_id) == ("deploy-guard.yml", "guard"):
                        # The guard action fetches trunk from origin, which a
                        # private repo needs credentials for; documented inline.
                        self.assertIsNone(persists)
                    else:
                        self.assertIs(persists, False, f"{name}:{job_id} keeps checkout credentials")

    def test_non_pr_concurrency_is_unique_per_push(self):
        ci = load("ci.yml")["concurrency"]
        self.assertIn("github.event.pull_request.number || github.sha", ci["group"])
        self.assertIn("github.sha", load("deploy-guard.yml")["concurrency"]["group"])
        self.assertIn("github.sha", load("nightly.yml")["concurrency"]["group"])
        self.assertIs(load("deploy-guard.yml")["concurrency"]["cancel-in-progress"], False)

    def test_unfilled_placeholder_slots_fail(self):
        slots = [
            ("ci.yml", "fast", "Unfilled slot"),
            ("ci.yml", "test", "Unfilled slot"),
            ("nightly.yml", "heavy", "Unfilled slot"),
            ("deploy-guard.yml", "deploy", "fails until replaced"),
        ]
        for name, job_id, fragment in slots:
            run = step_named(load(name), job_id, fragment)["run"]
            self.assertRegex(run, r"(?m)^\s*exit 1\s*$", f"{name}:{job_id}")

    def test_summary_jobs_check_success_positively(self):
        for name, job_id in (("ci.yml", "ci-summary"), ("nightly.yml", "nightly-summary")):
            run = step_named(load(name), job_id, "Fail unless")["run"]
            self.assertIn('.result == "success"', run)
            self.assertNotIn("grep", run)

    def test_audit_runs_the_script_and_fails_on_any_nonzero(self):
        wf = load("trunk-audit.yml")
        run = step_named(wf, "audit", "on trunk")["run"]
        self.assertIn("check_deployed_on_trunk.py", run)
        self.assertNotIn("|| true", run)
        self.assertNotIn("continue-on-error", json.dumps(wf))
        self.assertIn("schedule", wf[True])  # PyYAML parses the key `on` as True

    def test_wait_gate_is_optional_and_least_privilege(self):
        guard = load("deploy-guard.yml")["jobs"]["guard"]
        self.assertEqual(guard["permissions"], {"contents": "read", "checks": "read"})
        step = step_named(load("deploy-guard.yml"), "guard", "Wait for the CI")
        self.assertIn("WAIT_FOR_CI", step["if"])
        self.assertGreater(guard["timeout-minutes"] * 60, int(step["env"]["WAIT_TIMEOUT_SECONDS"].split("'")[-2]))


@unittest.skipUnless(BASH and JQ, "bash and jq are required")
class SummaryJobTests(unittest.TestCase):
    def run_summary(self, workflow, job_id, needs):
        run = step_named(load(workflow), job_id, "Fail unless")["run"]
        env = {**os.environ, "NEEDS_JSON": json.dumps(needs)}
        return subprocess.run([BASH, "-c", run], env=env, capture_output=True, text=True).returncode

    def check(self, workflow, job_id):
        ok = {"a": {"result": "success"}, "b": {"result": "success"}}
        self.assertEqual(self.run_summary(workflow, job_id, ok), 0)
        for bad in ("failure", "cancelled", "skipped", "something-new", None):
            with self.subTest(result=bad):
                needs = {"a": {"result": "success"}, "b": {"result": bad}}
                self.assertEqual(self.run_summary(workflow, job_id, needs), 1)
        self.assertEqual(self.run_summary(workflow, job_id, {}), 1)  # no needs: never green

    def test_ci_summary(self):
        self.check("ci.yml", "ci-summary")

    def test_nightly_summary(self):
        self.check("nightly.yml", "nightly-summary")


FAKE_GH = """#!/usr/bin/env bash
# Serves $FAKE_GH_FIXTURE (JSON) through the --jq filter the caller passes.
jq_expr=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--jq" ]; then jq_expr="$2"; shift; fi
  shift
done
if [ -n "${FAKE_GH_FAIL:-}" ]; then exit 1; fi
jq -r "$jq_expr" < "$FAKE_GH_FIXTURE"
"""


@unittest.skipUnless(BASH and JQ, "bash and jq are required")
class WaitForCiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="wait-for-ci-"))
        gh = cls.tmp / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        step = step_named(load("deploy-guard.yml"), "guard", "Wait for the CI")
        cls.run_block = step["run"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def wait(self, conclusions, timeout="0", gh_fails=False):
        fixture = self.tmp / "fixture.json"
        fixture.write_text(json.dumps({"check_runs": [{"conclusion": c} for c in conclusions]}))
        env = {
            **os.environ,
            "PATH": f"{self.tmp}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_FIXTURE": str(fixture),
            "REPO": "o/r",
            "DEPLOY_SHA": "a" * 40,
            "CI_CHECK_NAME": "ci-summary",
            "WAIT_TIMEOUT_SECONDS": timeout,
            "WAIT_INTERVAL_SECONDS": "0",
        }
        if gh_fails:
            env["FAKE_GH_FAIL"] = "1"
        return subprocess.run([BASH, "-c", self.run_block], env=env, capture_output=True, text=True, timeout=60)

    def test_green_check_lets_the_deploy_continue(self):
        self.assertEqual(self.wait(["success"]).returncode, 0)

    def test_failed_check_blocks_immediately(self):
        for bad in ("failure", "cancelled", "skipped", "timed_out"):
            with self.subTest(conclusion=bad):
                result = self.wait([bad], timeout="600")
                self.assertEqual(result.returncode, 1)
                self.assertIn("did not succeed", result.stdout)

    def test_pending_check_times_out_closed(self):
        result = self.wait([None])
        self.assertEqual(result.returncode, 1)
        self.assertIn("within 0s", result.stdout)

    def test_missing_check_times_out_closed(self):
        result = self.wait([])
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing", result.stdout)

    def test_api_error_never_counts_as_green(self):
        self.assertEqual(self.wait(["success"], gh_fails=True).returncode, 1)


if __name__ == "__main__":
    unittest.main()
