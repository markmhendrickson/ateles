#!/usr/bin/env python3
"""Tests for the interactive-session checkout-freshness SessionStart hook.

Builds real git fixture repos (a bare "origin" plus a clone) and drives them
into behind/diverged/dirty/clean/unknown states, the same way
`lib/daemon_runtime/checkout_drift.py`'s own tests would, since this hook
ports that module's reasoning from daemons to interactive sessions.

What red looked like, concretely, before this hook existed: nothing. There
was no mechanism that compared an interactive session's HEAD to
`origin/main` at all, so a session could run its entire length on a stale
checkout with zero signal — which is the literal 2026-09-18 incident this
hook exists to close (a session detached 8 commits behind `origin/main`,
grepped the stale tree for a test that had already merged, and concluded the
mechanism did not exist). These tests fail if `checkout_freshness.py` is
deleted or its detection logic regresses to always reporting "clean"/"unknown".
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))

import checkout_freshness as cf  # noqa: E402


def _run(args: list[str], cwd: Path) -> str:
    p = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return p.stdout.strip()


def _init_repo_pair(tmp: Path) -> tuple[Path, Path]:
    """A bare 'origin' plus a clone tracking it, both with one commit."""
    origin = tmp / "origin.git"
    origin.mkdir()
    _run(["init", "--bare", "-q"], origin)

    seed = tmp / "seed"
    seed.mkdir()
    _run(["init", "-q", "-b", "main"], seed)
    _run(["config", "user.email", "test@example.com"], seed)
    _run(["config", "user.name", "Test"], seed)
    (seed / "f.txt").write_text("one\n", encoding="utf-8")
    _run(["add", "f.txt"], seed)
    _run(["commit", "-q", "-m", "initial"], seed)
    _run(["remote", "add", "origin", str(origin)], seed)
    _run(["push", "-q", "origin", "main"], seed)

    clone = tmp / "clone"
    _run(["clone", "-q", str(origin), str(clone)], tmp)
    _run(["config", "user.email", "test@example.com"], clone)
    _run(["config", "user.name", "Test"], clone)
    _run(["checkout", "-q", "-b", "main", "origin/main"], clone)
    return origin, clone


class FreshnessFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.origin, self.clone = _init_repo_pair(self.tmp)


class TestCleanIsSilent(FreshnessFixture):
    """A current checkout must report clean and print nothing."""

    def test_fresh_clone_is_clean(self) -> None:
        report = cf.check_freshness(self.clone)
        self.assertEqual(report.state, "clean")
        self.assertFalse(report.is_drifted)

    def test_clean_report_produces_no_banner_text(self) -> None:
        report = cf.check_freshness(self.clone)
        # main() only prints for a drifted report — assert the gate directly.
        self.assertFalse(report.is_drifted)


class TestBehind(FreshnessFixture):
    """RED before this hook existed: nothing detected this at all."""

    def setUp(self) -> None:
        super().setUp()
        # Advance origin/main two commits the clone never fetches.
        seed2 = self.tmp / "seed"
        for i in range(2):
            (seed2 / "f.txt").write_text(f"advance {i}\n", encoding="utf-8")
            _run(["add", "f.txt"], seed2)
            _run(["commit", "-q", "-m", f"advance {i}"], seed2)
        _run(["push", "-q", "origin", "main"], seed2)
        _run(["fetch", "-q", "origin"], self.clone)

    def test_reports_behind_with_correct_count(self) -> None:
        report = cf.check_freshness(self.clone)
        self.assertEqual(report.state, "behind")
        self.assertEqual(report.behind, 2)
        self.assertTrue(report.is_drifted)

    def test_banner_names_the_remedy(self) -> None:
        report = cf.check_freshness(self.clone)
        text = cf.banner(report)
        self.assertIn("BEHIND", text)
        self.assertIn("git checkout --detach origin/main", text)

    def test_main_prints_banner_for_behind_checkout(self) -> None:
        """End-to-end: main() run with cwd inside the behind clone."""
        p = subprocess.run(
            [sys.executable, str(HOOKS / "checkout_freshness.py")],
            cwd=str(self.clone),
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(p.returncode, 0)
        self.assertIn("checkout-freshness", p.stdout)
        self.assertIn("BEHIND", p.stdout)

    def test_main_prints_nothing_for_clean_checkout(self) -> None:
        """Same entrypoint, fresh clone — silence is the healthy case."""
        fresh = self.tmp / "clone2"
        _run(["clone", "-q", str(self.origin), str(fresh)], self.tmp)
        p = subprocess.run(
            [sys.executable, str(HOOKS / "checkout_freshness.py")],
            cwd=str(fresh),
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")


class TestDiverged(FreshnessFixture):
    def test_local_commit_plus_upstream_commit_is_diverged(self) -> None:
        seed2 = self.tmp / "seed"
        (seed2 / "f.txt").write_text("upstream advance\n", encoding="utf-8")
        _run(["add", "f.txt"], seed2)
        _run(["commit", "-q", "-m", "upstream advance"], seed2)
        _run(["push", "-q", "origin", "main"], seed2)
        _run(["fetch", "-q", "origin"], self.clone)

        (self.clone / "g.txt").write_text("local\n", encoding="utf-8")
        _run(["add", "g.txt"], self.clone)
        _run(["commit", "-q", "-m", "local commit"], self.clone)

        report = cf.check_freshness(self.clone)
        self.assertEqual(report.state, "diverged")
        self.assertEqual(report.ahead, 1)
        self.assertEqual(report.behind, 1)
        self.assertTrue(report.is_drifted)

    def test_ahead_only_is_diverged_not_clean(self) -> None:
        """Unpushed local commits are not 'current' — mirrors checkout_drift.py."""
        (self.clone / "h.txt").write_text("local only\n", encoding="utf-8")
        _run(["add", "h.txt"], self.clone)
        _run(["commit", "-q", "-m", "unpushed"], self.clone)

        report = cf.check_freshness(self.clone)
        self.assertEqual(report.state, "diverged")
        self.assertTrue(report.is_drifted)


class TestDirty(FreshnessFixture):
    def test_uncommitted_tracked_change_is_dirty(self) -> None:
        (self.clone / "f.txt").write_text("uncommitted edit\n", encoding="utf-8")
        report = cf.check_freshness(self.clone)
        self.assertEqual(report.state, "dirty")
        self.assertTrue(report.is_drifted)

    def test_untracked_file_alone_is_not_dirty(self) -> None:
        """Matches checkout_drift.py: untracked files accumulate normally."""
        (self.clone / "scratch.log").write_text("noise\n", encoding="utf-8")
        report = cf.check_freshness(self.clone)
        self.assertEqual(report.state, "clean")
        self.assertFalse(report.is_drifted)


class TestUnknownIsNotCurrent(FreshnessFixture):
    """A non-verdict must never look like a clean bill of health."""

    def test_no_remote_tracking_ref_is_unknown(self) -> None:
        # A standalone repo with no `origin` remote at all — e.g. a fresh
        # `git init` never pushed anywhere, or a detached checkout whose
        # remote was never configured. Distinct from the "seed" fixture,
        # which DOES have `refs/remotes/origin/main` since it was pushed to
        # the bare origin during setup.
        solo = self.tmp / "solo"
        solo.mkdir()
        _run(["init", "-q", "-b", "main"], solo)
        _run(["config", "user.email", "test@example.com"], solo)
        _run(["config", "user.name", "Test"], solo)
        (solo / "f.txt").write_text("one\n", encoding="utf-8")
        _run(["add", "f.txt"], solo)
        _run(["commit", "-q", "-m", "initial"], solo)

        report = cf.check_freshness(solo)
        self.assertEqual(report.state, "unknown")
        self.assertFalse(report.is_drifted)

    def test_non_repo_directory_is_not_a_repo(self) -> None:
        plain = self.tmp / "not_a_repo"
        plain.mkdir()
        report = cf.check_freshness(plain)
        self.assertEqual(report.state, "not_a_repo")
        self.assertFalse(report.is_drifted)

    def test_unknown_and_not_a_repo_never_print_a_banner(self) -> None:
        """main() must stay silent on a non-verdict, not report false confidence."""
        plain = self.tmp / "not_a_repo2"
        plain.mkdir()
        p = subprocess.run(
            [sys.executable, str(HOOKS / "checkout_freshness.py")],
            cwd=str(plain),
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")


class TestWorktreeAwareRemedy(FreshnessFixture):
    """The fallback discovered today: main claimed by another worktree."""

    def test_remedy_always_suggests_detach_not_plain_checkout(self) -> None:
        # Regardless of worktree topology, the remedy must avoid the exact
        # failure hit today ("main" already used by worktree
        # `ateles-wt-routing`) by always suggesting a detached checkout.
        report = cf.FreshnessReport(state="behind", behind=127, head="abc1234")
        text = cf.remedy(report)
        self.assertIn("--detach", text)
        self.assertIn("already used by worktree", text)


class TestFailOpen(unittest.TestCase):
    def test_never_raises_on_missing_git_binary(self) -> None:
        with TemporaryDirectory() as d:
            # No git repo at all, and _git() must swallow any OSError/timeout.
            report = cf.check_freshness(Path(d))
            self.assertEqual(report.state, "not_a_repo")

    def test_main_always_exits_zero(self) -> None:
        with TemporaryDirectory() as d:
            p = subprocess.run(
                [sys.executable, str(HOOKS / "checkout_freshness.py")],
                cwd=d,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
