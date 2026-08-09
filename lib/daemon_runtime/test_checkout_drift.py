"""
Tests for lib/daemon_runtime/checkout_drift.py.

These build real git repositories in tmp_path rather than mocking `git`. The
bug being guarded against is a *git state* — a checkout that has diverged such
that `pull --ff-only` refuses — and a mocked `git` would only assert that the
module calls the commands the author expected, not that it reads the state
correctly.

The scenario that matters most is `test_diverged_local_commit_is_drift`: that is
the exact shape of ~/ateles-rc-src on 2026-08-09, where a local merge commit
meant ateles#401 merged to main and never reached the running daemon.

Run: pytest lib/daemon_runtime/test_checkout_drift.py -v
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from checkout_drift import (
    CheckoutDriftError,
    DriftReport,
    check_checkout_drift,
    warn_on_drift,
)


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=False
    )
    return (p.stdout or p.stderr).strip()


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo: Path, name: str, body: str = "x") -> None:
    (repo / name).write_text(body)
    _git(repo, "add", name)
    _git(repo, "commit", "--quiet", "-m", f"add {name}")


@pytest.fixture
def remote_and_clone(tmp_path):
    """A bare 'origin' plus a clone tracking origin/main, both with one commit."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "seed"
    _init(work)
    _commit(work, "base.txt")
    origin.mkdir()
    _git(origin, "init", "--bare", "--quiet", "-b", "main")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "--quiet", "-u", "origin", "main")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(clone)],
        capture_output=True,
        check=False,
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    _git(clone, "config", "commit.gpgsign", "false")
    return origin, work, clone


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


def test_clean_checkout_is_not_drift(remote_and_clone):
    _origin, _work, clone = remote_and_clone
    r = check_checkout_drift(clone)
    assert r.state == "clean", r
    assert not r.is_drifted


def test_behind_is_drift(remote_and_clone):
    """A daemon on an old commit runs superseded code — the ateles#401 symptom."""
    origin, work, clone = remote_and_clone
    _commit(work, "newer.txt")
    _git(work, "push", "--quiet", "origin", "main")

    r = check_checkout_drift(clone)
    assert r.state == "behind", r
    assert r.behind == 1
    assert r.is_drifted
    assert "BEHIND" in r.summary()


def test_diverged_local_commit_is_drift(remote_and_clone):
    """
    The 2026-08-09 shape: a local commit that exists nowhere upstream.

    `git pull --ff-only` refuses here and leaves HEAD untouched, so a merge to
    main silently never reaches the daemon. Ahead-only must count as drift —
    the work is also one power-cycle from being lost.
    """
    _origin, _work, clone = remote_and_clone
    _commit(clone, "local_only.txt")

    r = check_checkout_drift(clone)
    assert r.state == "diverged", r
    assert r.ahead == 1
    assert r.is_drifted
    assert "DIVERGED" in r.summary()


def test_both_ahead_and_behind_is_diverged(remote_and_clone):
    origin, work, clone = remote_and_clone
    _commit(work, "upstream.txt")
    _git(work, "push", "--quiet", "origin", "main")
    _commit(clone, "local.txt")

    r = check_checkout_drift(clone)
    assert r.state == "diverged", r
    assert r.ahead == 1 and r.behind == 1
    assert r.is_drifted


def test_uncommitted_tracked_changes_are_drift(remote_and_clone):
    _origin, _work, clone = remote_and_clone
    (clone / "base.txt").write_text("modified without committing")

    r = check_checkout_drift(clone)
    assert r.state == "dirty", r
    assert r.is_drifted


def test_untracked_files_are_not_drift(remote_and_clone):
    """
    Daemon checkouts accumulate logs and state files. Flagging those would bury
    the real signal under noise operators learn to ignore.
    """
    _origin, _work, clone = remote_and_clone
    (clone / ".phoenicurus_prepare_last_sha").write_text("deadbeef")
    (clone / "some.log").write_text("noise")

    r = check_checkout_drift(clone)
    assert r.state == "clean", r
    assert not r.is_drifted


# ---------------------------------------------------------------------------
# Non-verdicts — the cases that must NOT be reported as drift
# ---------------------------------------------------------------------------


def test_offline_is_unknown_not_drift(remote_and_clone, monkeypatch):
    """
    A failed fetch must not look like stale code. Reporting an offline host as
    drifted would train operators to ignore the warning.
    """
    _origin, _work, clone = remote_and_clone
    _git(clone, "remote", "set-url", "origin", "file:///nonexistent/repo.git")

    r = check_checkout_drift(clone, fetch=True)
    assert r.state == "unknown", r
    assert not r.is_drifted


def test_not_a_repo_is_not_drift(tmp_path):
    r = check_checkout_drift(tmp_path / "plain_dir")
    assert r.state in ("not_a_repo", "unknown"), r
    assert not r.is_drifted


def test_no_upstream_is_unknown(remote_and_clone):
    _origin, _work, clone = remote_and_clone
    _git(clone, "checkout", "--quiet", "-b", "untracked-branch")

    r = check_checkout_drift(clone)
    assert r.state == "unknown", r
    assert not r.is_drifted


# ---------------------------------------------------------------------------
# Posture
# ---------------------------------------------------------------------------


def test_warn_on_drift_does_not_raise_by_default(remote_and_clone, monkeypatch):
    """
    Advisory by default. These daemons are the release, payment, and dispatch
    path — a guard that hard-stops all of them on a stale checkout would cause a
    bigger outage than the drift it prevents.
    """
    monkeypatch.delenv("ATELES_ENFORCE_CHECKOUT_FRESHNESS", raising=False)
    _origin, _work, clone = remote_and_clone
    _commit(clone, "local_only.txt")

    r = warn_on_drift("test-daemon", clone)
    assert r.is_drifted, "must still report the drift it declined to raise on"


def test_warn_on_drift_raises_when_enforced(remote_and_clone):
    _origin, _work, clone = remote_and_clone
    _commit(clone, "local_only.txt")

    with pytest.raises(CheckoutDriftError):
        warn_on_drift("test-daemon", clone, enforce=True)


def test_enforcement_via_env(remote_and_clone, monkeypatch):
    monkeypatch.setenv("ATELES_ENFORCE_CHECKOUT_FRESHNESS", "1")
    _origin, _work, clone = remote_and_clone
    _commit(clone, "local_only.txt")

    with pytest.raises(CheckoutDriftError):
        warn_on_drift("test-daemon", clone)


def test_clean_checkout_never_raises_even_when_enforced(remote_and_clone):
    _origin, _work, clone = remote_and_clone
    r = warn_on_drift("test-daemon", clone, enforce=True)
    assert r.state == "clean"


def test_unknown_never_raises_even_when_enforced(remote_and_clone):
    """Offline must not take down a daemon that enabled enforcement."""
    _origin, _work, clone = remote_and_clone
    _git(clone, "remote", "set-url", "origin", "file:///nonexistent/repo.git")

    r = warn_on_drift("test-daemon", clone, enforce=True)
    assert r.state == "unknown"


# ---------------------------------------------------------------------------
# Enforcement reaches the caller
# ---------------------------------------------------------------------------


def test_enforcement_is_not_swallowed_by_a_caller_guard(remote_and_clone, monkeypatch):
    """
    A caller that wraps the check in `except Exception` silently defeats
    enforcement: CheckoutDriftError is a RuntimeError, so a blanket catch
    downgrades the abort to a warning and the daemon runs on stale code anyway.

    That is precisely the failure this module exists to catch, and ateles#405
    shipped it in `prepare.py` on the first pass (caught by Loxia). This test
    pins the contract the call site depends on: under enforcement the error
    must escape, and it must NOT be a subclass of anything a caller would
    reasonably catch as "the check is unavailable".
    """
    _origin, _work, clone = remote_and_clone
    _commit(clone, "local_only.txt")

    # The shape prepare.py now uses: setup guarded, the check itself is not.
    setup_failed = False
    try:
        fn = warn_on_drift
    except Exception:  # pragma: no cover - defensive
        setup_failed = True
    assert not setup_failed

    with pytest.raises(CheckoutDriftError):
        fn("test-daemon", clone, enforce=True)


def test_drift_error_carries_the_report(remote_and_clone):
    """The raised error must explain itself, or an operator sees only a traceback."""
    _origin, _work, clone = remote_and_clone
    _commit(clone, "local_only.txt")

    with pytest.raises(CheckoutDriftError) as excinfo:
        warn_on_drift("test-daemon", clone, enforce=True)

    err = excinfo.value
    assert isinstance(err.report, DriftReport)
    assert err.report.is_drifted
    assert "DIVERGED" in str(err)
