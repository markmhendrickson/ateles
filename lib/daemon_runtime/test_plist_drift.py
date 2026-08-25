"""
Tests for lib/daemon_runtime/plist_drift.py.

These write real plist files to tmp_path rather than mocking ``plistlib``. The
bug being guarded against is a *file state* — a reviewed plist that disagrees
with the launchd environment a daemon was actually booted with — and a mocked
reader would only assert that the module calls what the author expected, not
that it detects the divergence.

The scenario that matters most is ``test_undeclared_autonomy_flag_is_drift``:
that is the exact shape found on 2026-08-25, where ``ATELES_SWARM_AUTO_BUILD``
was absent from the repo plist while the running daemon carried ``=0``, so the
swarm was configured to stop before every build and no reviewed file said so.

Run: pytest lib/daemon_runtime/test_plist_drift.py -v
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from plist_drift import (
    ENFORCE_ENV,
    PlistConfigDriftError,
    check_plist_drift,
    warn_on_plist_drift,
)

LABEL = "com.ateles.test"


def _write_plist(path: Path, env: dict[str, str]) -> Path:
    path.write_bytes(
        plistlib.dumps({"Label": LABEL, "EnvironmentVariables": dict(env)})
    )
    return path


def test_identical_config_is_clean(tmp_path: Path) -> None:
    env = {"ATELES_SWARM_AUTO_BUILD": "1", "APIS_AUTONOMY_AUTO_MERGE": "0"}
    repo = _write_plist(tmp_path / "repo.plist", env)
    live = _write_plist(tmp_path / "live.plist", env)

    report = check_plist_drift(LABEL, repo, live_plist=live)

    assert report.state == "clean"
    assert not report.is_drifted


def test_undeclared_autonomy_flag_is_drift(tmp_path: Path) -> None:
    """The 2026-08-25 case: the flag runs but the reviewed file never mentions it.

    An absent key is indistinguishable from an unmade decision, which is how
    auto-build stayed off for four days after the bug justifying its rollback
    was fixed.
    """
    repo = _write_plist(tmp_path / "repo.plist", {"APIS_AUTONOMY_AUTO_MERGE": "0"})
    live = _write_plist(
        tmp_path / "live.plist",
        {"APIS_AUTONOMY_AUTO_MERGE": "0", "ATELES_SWARM_AUTO_BUILD": "0"},
    )

    report = check_plist_drift(LABEL, repo, live_plist=live)

    assert report.is_drifted
    assert report.live_only == ("ATELES_SWARM_AUTO_BUILD",)
    assert "UNDECLARED" in report.summary()
    assert "ATELES_SWARM_AUTO_BUILD" in report.summary()


def test_differing_value_is_drift_and_names_both_sides(tmp_path: Path) -> None:
    repo = _write_plist(tmp_path / "repo.plist", {"ATELES_SWARM_AUTO_BUILD": "1"})
    live = _write_plist(tmp_path / "live.plist", {"ATELES_SWARM_AUTO_BUILD": "0"})

    report = check_plist_drift(LABEL, repo, live_plist=live)

    assert report.is_drifted
    assert report.changed == {"ATELES_SWARM_AUTO_BUILD": ("0", "1")}
    summary = report.summary()
    assert "running='0'" in summary
    assert "reviewed='1'" in summary


def test_declared_but_not_running_is_drift(tmp_path: Path) -> None:
    """A reviewed flag the daemon never received is drift in the other direction."""
    repo = _write_plist(
        tmp_path / "repo.plist",
        {"ATELES_SWARM_AUTO_BUILD": "1", "ATELES_SWARM_AUTO_REREVIEW": "1"},
    )
    live = _write_plist(tmp_path / "live.plist", {"ATELES_SWARM_AUTO_BUILD": "1"})

    report = check_plist_drift(LABEL, repo, live_plist=live)

    assert report.is_drifted
    assert report.repo_only == ("ATELES_SWARM_AUTO_REREVIEW",)
    assert "NOT running" in report.summary()


def test_missing_live_plist_is_unknown_not_drift(tmp_path: Path) -> None:
    """Hand-launched daemons and CI have no launchd plist to compare against.

    Reporting that as drift would train operators to ignore the warning — the
    same reasoning as checkout_drift's UNKNOWN state for a failed fetch.
    """
    repo = _write_plist(tmp_path / "repo.plist", {"ATELES_SWARM_AUTO_BUILD": "1"})

    report = check_plist_drift(LABEL, repo, live_plist=tmp_path / "absent.plist")

    assert report.state == "unknown"
    assert not report.is_drifted


def test_unparseable_plist_is_unknown_not_drift(tmp_path: Path) -> None:
    repo = _write_plist(tmp_path / "repo.plist", {"A": "1"})
    live = tmp_path / "live.plist"
    live.write_bytes(b"this is not a plist")

    report = check_plist_drift(LABEL, repo, live_plist=live)

    assert report.state == "unknown"
    assert not report.is_drifted


def test_machine_specific_keys_are_ignored(tmp_path: Path) -> None:
    """HOME/PATH legitimately differ per machine and must not be drift."""
    repo = _write_plist(
        tmp_path / "repo.plist", {"HOME": "/Users/a", "PATH": "/bin", "X": "1"}
    )
    live = _write_plist(
        tmp_path / "live.plist", {"HOME": "/Users/b", "PATH": "/usr/bin", "X": "1"}
    )

    report = check_plist_drift(LABEL, repo, live_plist=live)

    assert report.state == "clean"


def test_warn_is_advisory_by_default(tmp_path: Path, monkeypatch) -> None:
    """Drift must not stop a daemon booting unless enforcement is opted into."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    repo = _write_plist(tmp_path / "repo.plist", {"ATELES_SWARM_AUTO_BUILD": "1"})
    live = _write_plist(tmp_path / "live.plist", {"ATELES_SWARM_AUTO_BUILD": "0"})

    report = warn_on_plist_drift(LABEL, repo, live_plist=live)

    assert report.is_drifted  # logged, not raised


def test_warn_raises_when_enforced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ENFORCE_ENV, "1")
    repo = _write_plist(tmp_path / "repo.plist", {"ATELES_SWARM_AUTO_BUILD": "1"})
    live = _write_plist(tmp_path / "live.plist", {"ATELES_SWARM_AUTO_BUILD": "0"})

    with pytest.raises(PlistConfigDriftError):
        warn_on_plist_drift(LABEL, repo, live_plist=live)


def test_enforcement_does_not_fire_on_unknown(tmp_path: Path, monkeypatch) -> None:
    """Even enforcing daemons must boot when there is nothing to compare."""
    monkeypatch.setenv(ENFORCE_ENV, "1")
    repo = _write_plist(tmp_path / "repo.plist", {"A": "1"})

    report = warn_on_plist_drift(LABEL, repo, live_plist=tmp_path / "absent.plist")

    assert report.state == "unknown"
