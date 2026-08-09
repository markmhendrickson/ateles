"""
Tests that the checkout-freshness guard actually reaches `prepare.py`'s exit.

## Why this file exists separately from lib/daemon_runtime/test_checkout_drift.py

Those tests prove the library raises under enforcement. They cannot prove the
*daemon* honours it — and on the first pass of ateles#405 it did not:

    try:
        from checkout_drift import warn_on_drift
        warn_on_drift(...)                       # raises under enforcement
    except Exception as exc:                     # ...and catches its own abort
        log.warning("checkout freshness check unavailable")

`CheckoutDriftError` subclasses `RuntimeError`, so the blanket catch downgraded
enforcement to a warning and the daemon ran on stale code anyway. The switch was
advertised in the same diff's comment and could never fire. Loxia caught it.

That is the same silently-defeated-safety-mechanism failure the guard exists to
prevent, one level up — so the regression has to be pinned at the *entrypoint*,
where the swallow happened, not at the library boundary where everything already
looked correct.

Run: pytest execution/daemons/phoenicurus-release/test_prepare_checkout_guard.py -v
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

DAEMON_DIR = Path(__file__).resolve().parent
PREPARE = DAEMON_DIR / "prepare.py"
REPO_ROOT = DAEMON_DIR.parent.parent.parent


def _run_prepare(env_extra: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    """
    Run prepare.py as a subprocess, the way launchd does.

    A subprocess is the point: the defect was an exception handler in the
    entrypoint, and only running the real entrypoint exercises it. Importing
    `main()` and calling it would let a test-local try/except mask exactly the
    behaviour under test.
    """
    import os

    env = dict(os.environ)
    env.update(env_extra)
    # Keep the run from doing anything real: --dry-run never spawns an agent,
    # and pointing at a repo with no release tag makes run_prepare exit early.
    return subprocess.run(
        [sys.executable, str(PREPARE), "--dry-run"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def drifted_checkout(tmp_path):
    """A git checkout carrying a local commit that exists nowhere upstream."""

    def _git(repo: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=False)

    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    _git(seed, "config", "commit.gpgsign", "false")
    (seed / "base.txt").write_text("x")
    _git(seed, "add", "base.txt")
    _git(seed, "commit", "--quiet", "-m", "base")
    origin.mkdir()
    _git(origin, "init", "--bare", "--quiet", "-b", "main")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "--quiet", "-u", "origin", "main")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(clone)],
        capture_output=True,
        check=False,
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    _git(clone, "config", "commit.gpgsign", "false")
    (clone / "local_only.txt").write_text("unpushed")
    _git(clone, "add", "local_only.txt")
    _git(clone, "commit", "--quiet", "-m", "local only")
    return clone


def test_enforcement_aborts_the_daemon(drifted_checkout, tmp_path):
    """
    With enforcement on and a drifted checkout, prepare.py must NOT run.

    Against the pre-fix entrypoint this fails: the blanket `except Exception`
    caught CheckoutDriftError and the daemon carried on to preflight.
    """
    proc = _run_prepare(
        {
            "ATELES_ENFORCE_CHECKOUT_FRESHNESS": "1",
            "ATELES_CHECKOUT_DRIFT_NO_FETCH": "1",
            "NEOTOMA_REPO_ROOT": str(tmp_path / "no-such-repo"),
        },
        cwd=drifted_checkout,
    )
    combined = proc.stdout + proc.stderr

    assert proc.returncode != 0, (
        "enforcement did not abort the daemon — the switch is advertised but "
        f"never fires. Output:\n{combined[-1500:]}"
    )
    assert "CheckoutDriftError" in combined or "checkout drift" in combined.lower(), (
        f"aborted, but not for the drift reason. Output:\n{combined[-1500:]}"
    )
    assert "checkout freshness check unavailable" not in combined, (
        "the abort was swallowed and downgraded to the setup-failure warning"
    )


def test_advisory_mode_does_not_abort(drifted_checkout, tmp_path):
    """Default posture: report the drift, keep running."""
    proc = _run_prepare(
        {
            "ATELES_CHECKOUT_DRIFT_NO_FETCH": "1",
            "NEOTOMA_REPO_ROOT": str(tmp_path / "no-such-repo"),
        },
        cwd=drifted_checkout,
    )
    combined = proc.stdout + proc.stderr

    assert "CHECKOUT DRIFT" in combined, (
        f"advisory mode must still report the drift. Output:\n{combined[-1200:]}"
    )
    # It proceeds past the guard and exits on its own preconditions (the bogus
    # NEOTOMA_REPO_ROOT), not on the drift.
    assert "CheckoutDriftError" not in combined, (
        "advisory mode must not raise — a release daemon that refuses to start "
        "is worse than one running slightly stale code"
    )
