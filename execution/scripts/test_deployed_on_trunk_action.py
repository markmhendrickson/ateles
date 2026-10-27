#!/usr/bin/env python3
"""Runs the deployed-on-trunk composite action's `run:` block for real.

The block is extracted from `.github/actions/deployed-on-trunk/action.yml` and
executed with bash against throwaway git repositories (a bare origin plus a
clone), with INPUT_SHA / INPUT_BRANCH set the way the action sets them. No
network and no GitHub involved. Skipped when git or bash is unavailable.

Exit contract under test: 0 on trunk, 1 not on trunk (or commit unavailable),
2 the input was unusable (bad/short/multi-line sha, empty or invalid branch).
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTION = REPO_ROOT / ".github" / "actions" / "deployed-on-trunk" / "action.yml"

GIT = shutil.which("git")
BASH = shutil.which("bash")


def action_script() -> str:
    action = yaml.safe_load(ACTION.read_text())
    steps = action["runs"]["steps"]
    assert len(steps) == 1, "test assumes a single run step"
    return steps[0]["run"]


def git(cwd, *args):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    out = subprocess.run([GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@unittest.skipUnless(GIT and BASH, "git and bash are required")
class ActionRunBlockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="deployed-on-trunk-")
        tmp = Path(cls.tmp)
        cls.script = tmp / "run.sh"
        cls.script.write_text(action_script())
        origin = tmp / "origin.git"
        work = tmp / "work"
        git(tmp, "init", "--bare", "--initial-branch=main", str(origin))
        git(tmp, "clone", str(origin), str(work))
        git(work, "checkout", "-b", "main")
        (work / "a.txt").write_text("1")
        git(work, "add", "a.txt")
        git(work, "commit", "-m", "first")
        cls.first = git(work, "rev-parse", "HEAD")
        (work / "a.txt").write_text("2")
        git(work, "commit", "-am", "second")
        cls.tip = git(work, "rev-parse", "HEAD")
        git(work, "push", "origin", "main")
        # A commit that exists locally and on origin, but only on a side branch.
        git(work, "checkout", "-b", "side", cls.first)
        (work / "b.txt").write_text("side")
        git(work, "add", "b.txt")
        git(work, "commit", "-m", "off trunk")
        cls.off_trunk = git(work, "rev-parse", "HEAD")
        git(work, "push", "origin", "side")
        git(work, "checkout", "main")
        cls.work = work

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_action(self, sha, branch="main"):
        env = {**os.environ, "INPUT_SHA": sha, "INPUT_BRANCH": branch}
        return subprocess.run([BASH, str(self.script)], cwd=self.work, env=env, capture_output=True, text=True)

    def test_ancestor_passes(self):
        result = self.run_action(self.first)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_trunk_tip_passes(self):
        self.assertEqual(self.run_action(self.tip).returncode, 0)

    def test_off_trunk_commit_exits_1(self):
        result = self.run_action(self.off_trunk)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not an ancestor", result.stdout)

    def test_unknown_full_sha_exits_1(self):
        self.assertEqual(self.run_action("f" * 40).returncode, 1)

    def test_short_sha_exits_2_even_when_it_names_an_ancestor(self):
        self.assertEqual(self.run_action(self.first[:7]).returncode, 2)

    def test_garbage_sha_exits_2(self):
        for bad in ("not-a-sha", "", "g" * 40, self.first + "0"):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_action(bad).returncode, 2)

    def test_two_line_sha_exits_2(self):
        # Line-by-line matching (grep) would accept this: each line is a sha.
        result = self.run_action(f"{self.first}\n{self.off_trunk}")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_empty_branch_exits_2(self):
        self.assertEqual(self.run_action(self.first, branch="").returncode, 2)

    def test_invalid_branch_name_exits_2(self):
        for bad in ("a:b", "has space", "x..y", "-delete"):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_action(self.first, branch=bad).returncode, 2)


if __name__ == "__main__":
    unittest.main()
