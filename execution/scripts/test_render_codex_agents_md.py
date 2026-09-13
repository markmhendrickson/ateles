"""Tests for execution/scripts/render_codex_agents_md.py.

The script exists to make the operator's standing rules bind in Codex, and its
`--check` mode is the control that keeps the rendered copy from going stale. A
control that cannot fail is decoration (`CLAUDE.md`: "A mechanism that does not
bind is not a control"), so the tests here are the failure modes themselves:

* the literal state found on 2026-09-13 — an `AGENTS.md` that exists and is
  empty, which is how a Codex session came to run with no rules at all;
* a rendered copy that has LOST a safety rule (``NEVER `git stash` ``), which is
  what a hand-maintained copy does over time;
* `CLAUDE.md` GAINING a rule the rendered copy lacks, which is the realistic
  drift direction — the source moves and the copy does not;
* a stale omission record, so a suppression cannot quietly widen the gate.

Every test writes to a temporary `--out`, never the operator's real Codex home.

Run: python3 execution/scripts/test_render_codex_agents_md.py
"""

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = Path(__file__).resolve().parent / "render_codex_agents_md.py"
REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

EXIT_OK = 0
EXIT_LOST = 1
EXIT_USAGE = 2


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


class TestRender(unittest.TestCase):
    def test_render_then_check_passes(self) -> None:
        """The rendered file carries every standing rule in the real CLAUDE.md.

        This is the gate lint.sh runs, against the repo's actual rule set, so a
        rule added to CLAUDE.md without being carried here fails in CI.
        """
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            rendered = run("--out", str(out))
            self.assertEqual(rendered.returncode, EXIT_OK, rendered.stderr)
            self.assertTrue(out.exists())

            checked = run("--check", "--out", str(out))
            self.assertEqual(
                checked.returncode, EXIT_OK,
                f"parity check failed against the real CLAUDE.md:\n"
                f"{checked.stdout}",
            )
            self.assertIn("OK:", checked.stdout)

    def test_rendered_file_carries_the_hard_constraints(self) -> None:
        """The rules that have cost real work are present by name.

        Named explicitly rather than counted: a count passes while the one rule
        that matters is the one missing.
        """
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))
            text = out.read_text(encoding="utf-8")
            for rule in (
                "NEVER `git stash`",
                "One worktree, one agent",
                "Verify before asserting",
                "Merge stays gated",
                "Proceed with your recommendation",
                "End every turn with the decisions",
            ):
                self.assertIn(rule, text, f"missing standing rule: {rule}")

    def test_states_that_claude_code_hooks_do_not_run_here(self) -> None:
        """A Codex session must not rely on a guard that is not running.

        CLAUDE.md documents `git stash` and Gmail sends as mechanically
        enforced. In Codex they are not, and telling the reader a mechanism has
        their back when it does not is worse than silence.
        """
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))
            text = out.read_text(encoding="utf-8")
            self.assertIn("do not run in Codex", text)
            self.assertIn("is not blocked here", text)


class TestCheckFails(unittest.TestCase):
    def test_empty_agents_md_fails(self) -> None:
        """The literal 2026-09-13 state: the file exists and carries nothing."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            out.write_text("", encoding="utf-8")
            result = run("--check", "--out", str(out))
            self.assertEqual(result.returncode, EXIT_LOST)
            self.assertIn("empty", result.stdout)

    def test_absent_agents_md_fails(self) -> None:
        """No load point at all is a failure, not a skip."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            result = run("--check", "--out", str(out))
            self.assertEqual(result.returncode, EXIT_LOST)
            self.assertIn("does not exist", result.stdout)

    def test_lost_safety_rule_is_named(self) -> None:
        """A rendered copy that dropped the never-stash rule fails, by name."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))
            kept = [
                line
                for line in out.read_text(encoding="utf-8").splitlines(True)
                if "NEVER `git stash`" not in line
            ]
            out.write_text("".join(kept), encoding="utf-8")

            result = run("--check", "--out", str(out))
            self.assertEqual(result.returncode, EXIT_LOST)
            self.assertIn("git stash", result.stdout)

    def test_new_rule_upstream_is_detected(self) -> None:
        """The realistic drift: CLAUDE.md gains a rule, the copy does not.

        Uses a temporary --source so the real CLAUDE.md is never touched.
        """
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))

            source = Path(tmp) / "CLAUDE.md"
            source.write_text(
                CLAUDE_MD.read_text(encoding="utf-8")
                + "\n- **Never run a migration against prod without a dry "
                "run.** Planted.\n",
                encoding="utf-8",
            )

            result = run(
                "--check", "--out", str(out), "--source", str(source)
            )
            self.assertEqual(result.returncode, EXIT_LOST)
            self.assertIn("migration against prod", result.stdout)

    def test_unreadable_source_exits_2_not_1(self) -> None:
        """"Could not run" must never read as "passed" — or as a finding."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))
            result = run(
                "--check",
                "--out",
                str(out),
                "--source",
                str(Path(tmp) / "absent.md"),
            )
            self.assertEqual(result.returncode, EXIT_USAGE)


class TestOmissions(unittest.TestCase):
    """A suppression nobody checks is how a real rule goes missing."""

    def test_omissions_are_reported_not_silent(self) -> None:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))
            result = run("--check", "--out", str(out))
            self.assertIn("Deliberate omissions honoured", result.stdout)

    def test_every_omission_carries_a_reason(self) -> None:
        sys.path.insert(0, str(SCRIPT.parent))
        import render_codex_agents_md as mod  # noqa: E402

        self.assertTrue(mod.DELIBERATE_OMISSIONS)
        for key, reason in mod.DELIBERATE_OMISSIONS.items():
            self.assertTrue(reason.strip(), f"{key} has no stated reason")

    def test_omitted_rules_still_exist_in_claude_md(self) -> None:
        """A stale omission fails the gate rather than widening it.

        An entry naming a rule CLAUDE.md no longer has is a suppression with
        nothing behind it, and would mask a future rule of the same name.
        """
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))
            result = run("--check", "--out", str(out))
            self.assertNotIn("stale omission record", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
