"""Tests for the session-start hook-wiring mismatch banner (ateles#973).

The regression case is the literal live consequence the issue reports: a
worktree `.claude/settings.json` missing `git_stash_guard.py` (PreToolUse
Bash), the `compact` matcher on `session_start.py`, and
`reinject_working_method.py` on compact. That session had no mechanical
never-stash enforcement and lost its interaction rules at every compaction
boundary, and nothing surfaced either fact.

Two properties matter as much as the detection:

* **Silence on a healthy checkout.** A banner that fires when nothing is wrong
  trains the operator to ignore it, which is the same outcome as not having it.
* **Fail-open.** The hook must never crash or hang a session start — an
  unreadable reference, or no reference at all, degrades to silence.
"""

import json
import runpy
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))

import hook_wiring_reference as hw  # noqa: E402

FULL = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Edit|Write|NotebookEdit|Bash",
                "hooks": [
                    {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/git_stash_guard.py"'},
                    {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/gmail_send_gate.py"'},
                ],
            }
        ],
        "SessionStart": [
            {
                "matcher": "startup|resume|clear|compact",
                "hooks": [
                    {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/session_start.py"'}
                ],
            },
            {
                "matcher": "compact",
                "hooks": [
                    {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/reinject_working_method.py"'}
                ],
            },
        ],
    }
}

# The three wirings the stale worktree lacked. `session_start.py` is present
# but on a matcher that excludes `compact` — the subtle half of the real bug,
# since the file resolves and the hook appears wired.
STALE = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Edit|Write|NotebookEdit|Bash",
                "hooks": [
                    {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/gmail_send_gate.py"'}
                ],
            }
        ],
        "SessionStart": [
            {
                "matcher": "startup|resume|clear",
                "hooks": [
                    {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/session_start.py"'}
                ],
            }
        ],
    }
}


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.reference = self.dir / "reference.json"
        self.reference.write_text(
            json.dumps({"wirings": sorted(list(w) for w in hw.wirings(FULL))}),
            encoding="utf-8",
        )

    def settings(self, obj: dict) -> Path:
        path = self.dir / "settings.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return path

    def gaps_for(self, obj: dict) -> list[str]:
        return hw.missing(self.settings(obj), self.reference)


class TestRegression(Fixture):
    """The issue's literal case: the three wirings the stale worktree lacked."""

    def test_all_three_missing_hooks_named_by_filename(self) -> None:
        gaps = self.gaps_for(STALE)
        self.assertEqual(len(gaps), 3, gaps)
        joined = " ".join(gaps)
        for name in (
            "git_stash_guard.py",
            "session_start.py",
            "reinject_working_method.py",
        ):
            self.assertIn(name, joined)

    def test_banner_is_actionable_not_a_bare_count(self) -> None:
        text = hw.banner(self.gaps_for(STALE))
        # Grep-able filenames, the reason each matters, and a runnable remedy.
        self.assertIn("git_stash_guard.py", text)
        self.assertIn("never-stash", text)
        self.assertIn("compaction", text)
        self.assertIn("git show origin/main:.claude/settings.json", text)

    def test_compact_matcher_gap_is_detected_not_just_file_presence(self) -> None:
        """`session_start.py` is wired in STALE — on the wrong matcher.

        The subtle half of the real bug. A check that only asked "is this file
        wired anywhere" would pass here and miss it.
        """
        joined = " ".join(self.gaps_for(STALE))
        self.assertIn("session_start.py", joined)
        self.assertIn("compact", joined)


class TestSilentOnHealthy(Fixture):
    def test_synced_settings_produce_no_banner(self) -> None:
        self.assertEqual(self.gaps_for(FULL), [])

    def test_extra_local_hook_does_not_trigger(self) -> None:
        """One-directional: a purely local addition is not a mismatch.

        A false positive here would train the operator to ignore the banner.
        """
        extra = json.loads(json.dumps(FULL))
        extra["hooks"]["Stop"] = [
            {"hooks": [{"type": "command", "command": "python3 local_only.py"}]}
        ]
        self.assertEqual(self.gaps_for(extra), [])


class TestPartialDesync(Fixture):
    def test_only_the_actually_missing_hook_is_listed(self) -> None:
        partial = json.loads(json.dumps(FULL))
        pre = partial["hooks"]["PreToolUse"][0]["hooks"]
        partial["hooks"]["PreToolUse"][0]["hooks"] = [
            h for h in pre if "git_stash_guard" not in h["command"]
        ]
        gaps = self.gaps_for(partial)
        self.assertEqual(len(gaps), 1, gaps)
        self.assertIn("git_stash_guard.py", gaps[0])


class TestFailOpen(Fixture):
    def test_absent_reference_degrades_to_silence(self) -> None:
        self.assertEqual(
            hw.missing(self.settings(STALE), self.dir / "no-such.json"), []
        )

    def test_malformed_reference_degrades_to_silence(self) -> None:
        bad = self.dir / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(hw.missing(self.settings(STALE), bad), [])

    def test_absent_settings_degrades_to_silence(self) -> None:
        self.assertEqual(
            hw.missing(self.dir / "no-settings.json", self.reference), []
        )

    def test_no_network_or_subprocess_is_used(self) -> None:
        """The comparison must not reach for git or the network.

        A live fetch inside a hook that fires on every session start is the
        failure surface the arch lens ruled out.
        """
        import subprocess
        import urllib.request

        with mock.patch.object(subprocess, "run") as run, mock.patch.object(
            urllib.request, "urlopen"
        ) as urlopen:
            hw.missing(self.settings(STALE), self.reference)
            run.assert_not_called()
            urlopen.assert_not_called()


class TestSessionStartIntegration(unittest.TestCase):
    """The hook itself must stay fail-open with the new check wired in."""

    def _run_hook(self) -> int:
        sys.argv = ["session_start.py"]
        try:
            runpy.run_path(str(HOOKS / "session_start.py"), run_name="__main__")
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 0
        return 0

    def test_exits_zero_even_when_the_wiring_check_raises(self) -> None:
        with mock.patch.object(hw, "missing", side_effect=RuntimeError("boom")):
            with mock.patch("sys.stdin", new=mock.MagicMock(read=lambda: "{}")):
                self.assertEqual(self._run_hook(), 0)

    def test_healthy_repo_prints_no_wiring_banner(self) -> None:
        """This repo's own settings must match its own reference snapshot."""
        self.assertEqual(hw.missing(), [], hw.missing())


class TestReferenceIsGenerated(unittest.TestCase):
    def test_committed_snapshot_matches_committed_settings(self) -> None:
        """`--check` parity, so the snapshot cannot silently go stale."""
        want = {
            tuple(str(x) for x in row)
            for row in json.loads(
                hw.REFERENCE_PATH.read_text(encoding="utf-8")
            )["wirings"]
        }
        have = hw.wirings(json.loads(hw.SETTINGS_PATH.read_text(encoding="utf-8")))
        self.assertEqual(want, have)

    def test_script_name_extraction_ignores_invocation_style(self) -> None:
        for command in (
            'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/x.py"',
            "python3 .claude/hooks/x.py",
            "'$CLAUDE_PROJECT_DIR'/.claude/hooks/x.py",
        ):
            self.assertEqual(hw._script_name(command), "x.py", command)


if __name__ == "__main__":
    unittest.main()
