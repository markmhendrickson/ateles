"""Tests for scripts/verify_claude_md_merge.py.

The script is the mechanical control ateles#973 requires, so its own
correctness has to be proven before the merge gate is trusted. A check that
cannot fail is decoration — the first test here is the literal bug from the
issue: a merged file that drops ``NEVER `git stash` `` must be reported, by
name, with a non-zero exit.

Highest-value edge case, per the QA lens: `Dispatch, don't work inline` and
`Dispatch, don't drift inline` are two DIFFERENT rules that the issue
explicitly keeps as separate bullets. If near-duplicate detection ever
collapsed them into one satisfied requirement, the script would silently
reintroduce the exact bug it exists to prevent. TestNearDuplicate pins that.
"""

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = Path(__file__).resolve().parent / "verify_claude_md_merge.py"

MAIN = """# CLAUDE.md

## Session conduct

- **NEVER `git stash`** in any form — the stash stack is shared.
- **One worktree, one agent.** Never point two agents at the same worktree.
- **Dispatch, don't work inline.** What to dispatch into: a Neotoma entity.
"""

WORKTREE = """# CLAUDE.md

## Session conduct

- **One worktree, one agent.** Never point two agents at the same worktree.
- **Dispatch, don't drift inline.** The failure mode: drift.
- **Verify before asserting.** Check the live system of record.
"""

MERGED_GOOD = """# CLAUDE.md

## Session conduct

- **NEVER `git stash`** in any form — the stash stack is shared.
- **One worktree, one agent.** Never point two agents at the same worktree.
- **Dispatch, don't work inline.** What to dispatch into: a Neotoma entity.
- **Dispatch, don't drift inline.** The failure mode: drift.
- **Verify before asserting.** Check the live system of record.
"""

# The issue's literal failure: a merge that reads as complete while a safety
# rule's name has vanished.
MERGED_DROPS_STASH = """# CLAUDE.md

## Session conduct

- **One worktree, one agent.** Never point two agents at the same worktree.
- **Dispatch, don't work inline.** What to dispatch into: a Neotoma entity.
- **Dispatch, don't drift inline.** The failure mode: drift.
- **Verify before asserting.** Check the live system of record.
"""


def write(dirpath: Path, name: str, text: str) -> Path:
    path = dirpath / name
    path.write_text(text, encoding="utf-8")
    return path


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


class ScriptCase(unittest.TestCase):
    """Base giving each test a scratch dir with the standard fixtures."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.main = write(self.dir, "main.md", MAIN)
        self.worktree = write(self.dir, "worktree.md", WORKTREE)


class TestRegressionDroppedRule(ScriptCase):
    """The bug ateles#973 reports: a rule silently dropped by diff-reading."""

    def test_dropped_safety_rule_fails_and_is_named(self) -> None:
        merged = write(self.dir, "merged.md", MERGED_DROPS_STASH)
        proc = run(
            "--base", str(self.main),
            "--other", str(self.worktree),
            "--merged", str(merged),
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("NEVER", proc.stdout)
        self.assertIn("git stash", proc.stdout)
        # Actionable: names the side it came from, so the fix is "add it back",
        # not "interpret this diff".
        self.assertIn("main.md", proc.stdout)
        self.assertIn("FAIL", proc.stdout)


class TestHappyPath(ScriptCase):
    def test_union_merge_passes(self) -> None:
        merged = write(self.dir, "merged.md", MERGED_GOOD)
        proc = run(
            "--base", str(self.main),
            "--other", str(self.worktree),
            "--merged", str(merged),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no rule lost", proc.stdout)
        self.assertNotIn("FAIL", proc.stdout)

    def test_check_mode_suppresses_inventory(self) -> None:
        merged = write(self.dir, "merged.md", MERGED_GOOD)
        proc = run(
            "--check",
            "--base", str(self.main),
            "--other", str(self.worktree),
            "--merged", str(merged),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Rule inventory", proc.stdout)


class TestNearDuplicate(ScriptCase):
    """Two similar leads are two rules. Never one counting as both.

    This is the highest-value case in the file: `Dispatch, don't work inline`
    and `Dispatch, don't drift inline` differ by one word and are explicitly
    kept as separate bullets by the issue.
    """

    def test_similar_leads_are_not_collapsed(self) -> None:
        merged = write(
            self.dir,
            "merged.md",
            MERGED_GOOD.replace(
                "- **Dispatch, don't drift inline.** The failure mode: drift.\n",
                "",
            ),
        )
        proc = run(
            "--base", str(self.main),
            "--other", str(self.worktree),
            "--merged", str(merged),
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("drift inline", proc.stdout)

    def test_near_duplicates_are_advisory_only(self) -> None:
        merged = write(self.dir, "merged.md", MERGED_GOOD)
        proc = run(
            "--base", str(self.main),
            "--other", str(self.worktree),
            "--merged", str(merged),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ADVISORY", proc.stdout)
        self.assertIn("Near-duplicate", proc.stdout)


class TestHeadings(ScriptCase):
    def test_renamed_heading_is_caught(self) -> None:
        merged = write(
            self.dir,
            "merged.md",
            MERGED_GOOD.replace("## Session conduct", "## How we work"),
        )
        proc = run(
            "--base", str(self.main),
            "--other", str(self.worktree),
            "--merged", str(merged),
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Session conduct", proc.stdout)
        self.assertIn("heading", proc.stdout)


class TestNormalization(ScriptCase):
    def test_punctuation_and_emphasis_style_match(self) -> None:
        base = write(
            self.dir, "b.md", "# T\n\n- **Rule of the road**  body\n"
        )
        merged = write(self.dir, "m.md", "# T\n\n- __Rule of the road.__ body\n")
        proc = run("--base", str(base), "--merged", str(merged))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_different_rules_do_not_match(self) -> None:
        base = write(self.dir, "b.md", "# T\n\n- **Rule A** body\n")
        merged = write(self.dir, "m.md", "# T\n\n- **Rule B** body\n")
        proc = run("--base", str(base), "--merged", str(merged))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Rule A", proc.stdout)

    def test_bolded_line_in_a_code_fence_is_not_a_rule(self) -> None:
        base = write(
            self.dir,
            "b.md",
            "# T\n\n```\n- **Illustrative only** not a rule\n```\n\n- **Real rule** body\n",
        )
        merged = write(self.dir, "m.md", "# T\n\n- **Real rule** body\n")
        proc = run("--base", str(base), "--merged", str(merged))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class TestInputErrors(ScriptCase):
    def test_missing_file_exits_2_not_a_false_pass(self) -> None:
        proc = run(
            "--base", str(self.dir / "nope.md"),
            "--merged", str(self.main),
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn("no rule lost", proc.stdout)
        self.assertIn("did NOT run", proc.stdout + proc.stderr)

    def test_unresolvable_git_ref_exits_2(self) -> None:
        proc = run(
            "--base", "no-such-ref-xyz:CLAUDE.md",
            "--merged", str(self.main),
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("did NOT run", proc.stdout + proc.stderr)


class TestDedups(ScriptCase):
    """A recorded dedup excuses a dropped name only while it stays true."""

    def test_recorded_dedup_is_honoured_and_reported(self) -> None:
        base = write(
            self.dir, "b.md", "# T\n\n- **Proceed on your recommendation** old\n"
        )
        merged = write(
            self.dir, "m.md", "# T\n\n- **Proceed with your recommendation** new\n"
        )
        rec = write(
            self.dir,
            "d.txt",
            "# note\nProceed on your recommendation => Proceed with your recommendation\n",
        )
        proc = run(
            "--base", str(base), "--merged", str(merged), "--dedups", str(rec)
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Deliberate dedups honoured", proc.stdout)
        self.assertIn("yielded to", proc.stdout)

    def test_stale_dedup_whose_dropped_rule_is_back_fails(self) -> None:
        base = write(self.dir, "b.md", "# T\n\n- **Old rule** body\n")
        merged = write(
            self.dir, "m.md", "# T\n\n- **Old rule** body\n- **New rule** body\n"
        )
        rec = write(self.dir, "d.txt", "Old rule => New rule\n")
        proc = run(
            "--base", str(base), "--merged", str(merged), "--dedups", str(rec)
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("stale", proc.stdout)

    def test_dedup_whose_survivor_vanished_fails(self) -> None:
        base = write(self.dir, "b.md", "# T\n\n- **Old rule** body\n")
        merged = write(self.dir, "m.md", "# T\n\n- **Unrelated** body\n")
        rec = write(self.dir, "d.txt", "Old rule => New rule\n")
        proc = run(
            "--base", str(base), "--merged", str(merged), "--dedups", str(rec)
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("ABSENT", proc.stdout)

    def test_malformed_dedup_line_exits_2(self) -> None:
        base = write(self.dir, "b.md", "# T\n\n- **Rule** body\n")
        rec = write(self.dir, "d.txt", "this line has no arrow\n")
        proc = run(
            "--base", str(base), "--merged", str(base), "--dedups", str(rec)
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("did NOT run", proc.stdout + proc.stderr)


class TestOneDirectionalBlindSpot(ScriptCase):
    """A side that never had the rule cannot witness its loss.

    Found while proving this script could fail: deleting ``NEVER `git stash` ``
    from the merged file passed a `--base origin/main` check cleanly, because
    that rule is worktree-only. The gate is only as wide as the input sides it
    is given — which is the same class of mistake as the hand-assembled
    summary this whole issue exists to replace. Pinned so nobody narrows the
    documented invocation back down.
    """

    def test_base_alone_cannot_see_a_rule_only_the_other_side_had(self) -> None:
        merged = write(self.dir, "merged.md", MERGED_DROPS_STASH)
        weak = run("--base", str(self.worktree), "--merged", str(merged))
        self.assertEqual(weak.returncode, 0, weak.stdout + weak.stderr)

    def test_adding_the_owning_side_catches_it(self) -> None:
        merged = write(self.dir, "merged.md", MERGED_DROPS_STASH)
        strong = run(
            "--base", str(self.worktree),
            "--other", str(self.main),
            "--merged", str(merged),
        )
        self.assertEqual(strong.returncode, 1, strong.stdout + strong.stderr)
        self.assertIn("git stash", strong.stdout)


class TestLiveRepo(unittest.TestCase):
    """The real merge, checked the way CI will check it."""

    def test_branch_loses_no_rule_against_main_or_its_own_head(self) -> None:
        """The binding invocation: every side whose rules must survive.

        `HEAD:CLAUDE.md` is not redundant with main. The file carries rules
        main never had, so main alone cannot witness their loss — see
        TestOneDirectionalBlindSpot.
        """
        repo = SCRIPT.resolve().parents[1]
        dedups = repo / "scripts" / "claude_md_dedups.txt"
        proc = subprocess.run(
            [
                sys.executable, str(SCRIPT), "--check",
                "--base", "origin/main:CLAUDE.md",
                "--other", "HEAD:CLAUDE.md",
                "--merged", "CLAUDE.md",
                "--dedups", str(dedups),
            ],
            capture_output=True, text=True, cwd=repo, check=False,
        )
        if proc.returncode == 2:
            self.skipTest("origin/main not fetched in this checkout")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_both_conflict_markers_present(self) -> None:
        claude_md = (SCRIPT.resolve().parents[1] / "CLAUDE.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            claude_md.count("CONFLICT: unresolved, see ateles#973"),
            2,
            "both deliberately-unresolved rule pairs must carry a marker",
        )


if __name__ == "__main__":
    unittest.main()
