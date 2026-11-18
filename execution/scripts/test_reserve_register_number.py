"""Offline acceptance tests for create-only register number reservations."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("reserve_register_number.py")


def command(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def git(cwd: Path, *args: str) -> str:
    result = command(cwd, "git", *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def clones(tmp_path: Path) -> tuple[Path, Path, Path]:
    origin = tmp_path / "origin.git"
    first = tmp_path / "first"
    second = tmp_path / "second"
    git(tmp_path, "init", "--bare", str(origin))
    git(tmp_path, "init", "-b", "main", str(first))
    git(first, "config", "user.name", "Synthetic Tester")
    git(first, "config", "user.email", "synthetic@example.invalid")
    foundation = first / "docs" / "foundation"
    foundation.mkdir(parents=True)
    (foundation / "conformance.md").write_text(
        "| # | The question | Argued in | Blocks | Status |\n"
        "|---|---|---|---|---|\n"
        "| 40 | question | `doc.md#q` | — | **open** |\n"
    )
    (foundation / "revisions.md").write_text(
        "## Example\n| Revision | Pass | Change |\n|---|---|---|\n| 17 | pass | change |\n"
    )
    git(first, "add", ".")
    git(first, "commit", "-m", "synthetic base")
    git(first, "remote", "add", "origin", str(origin))
    git(first, "push", "-u", "origin", "main")
    git(tmp_path, "clone", "-b", "main", str(origin), str(second))
    return origin, first, second


def run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return command(cwd, sys.executable, str(SCRIPT), *args)


def test_two_claimants_get_distinct_numbers(clones: tuple[Path, Path, Path]) -> None:
    origin, first, second = clones
    left = subprocess.Popen([sys.executable, str(SCRIPT), "reserve", "decision"],
                            cwd=first, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    right = subprocess.Popen([sys.executable, str(SCRIPT), "reserve", "decision"],
                             cwd=second, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lout, lerr = left.communicate(timeout=15)
    rout, rerr = right.communicate(timeout=15)
    assert left.returncode == 0, lerr
    assert right.returncode == 0, rerr
    assert {int(lout.strip()), int(rout.strip())} == {41, 42}
    refs = git(first, "ls-remote", "origin", "refs/reservations/decision/*")
    assert "refs/reservations/decision/41" in refs
    assert "refs/reservations/decision/42" in refs


def test_same_ref_has_one_winner_and_loser_reserves_next(clones: tuple[Path, Path, Path]) -> None:
    _, first, second = clones
    sha = git(first, "rev-parse", "HEAD")
    ref = "refs/reservations/decision/41"
    argv = ["git", "push", f"--force-with-lease={ref}:", "origin", f"{sha}:{ref}"]
    left = subprocess.Popen(argv, cwd=first, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    right = subprocess.Popen(argv, cwd=second, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    left.communicate(timeout=15)
    right.communicate(timeout=15)
    assert sorted([left.returncode, right.returncode])[0] == 0
    assert sum(code == 0 for code in (left.returncode, right.returncode)) == 1
    loser = first if left.returncode else second
    assert run(loser, "reserve", "decision").stdout.strip() == "42"


def test_next_accounts_for_register_and_refs(clones: tuple[Path, Path, Path]) -> None:
    _, first, _ = clones
    assert run(first, "next", "decision").stdout.strip() == "41"
    assert run(first, "reserve", "decision").stdout.strip() == "41"
    assert run(first, "next", "decision").stdout.strip() == "42"
    assert run(first, "next", "revision").stdout.strip() == "18"


def test_added_row_requires_reservation_and_duplicate_fails(clones: tuple[Path, Path, Path]) -> None:
    _, first, _ = clones
    register = first / "docs" / "foundation" / "conformance.md"
    register.write_text(register.read_text() +
                        "| 41 | next | `doc.md#next` | — | **open** |\n")
    missing = run(first, "check", "decision")
    assert missing.returncode == 1
    assert "reserve_register_number.py reserve decision" in missing.stderr
    assert run(first, "reserve", "decision").stdout.strip() == "41"
    assert run(first, "check", "decision").returncode == 0
    register.write_text(register.read_text() +
                        "| 41 | duplicate | `doc.md#duplicate` | — | **open** |\n")
    duplicate = run(first, "check", "decision")
    assert duplicate.returncode == 1
    assert "duplicate decision" in duplicate.stderr


def test_remote_read_failure_is_not_a_pass(clones: tuple[Path, Path, Path]) -> None:
    _, first, _ = clones
    result = run(first, "check", "decision", "--remote", "missing")
    assert result.returncode == 1
    assert "ls-remote" in result.stderr


def test_revision_row_requires_reservation(clones: tuple[Path, Path, Path]) -> None:
    _, first, _ = clones
    revisions = first / "docs" / "foundation" / "revisions.md"
    revisions.write_text(revisions.read_text() + "| 18 | next | change |\n")
    assert run(first, "check", "revision").returncode == 1
    assert run(first, "reserve", "revision").stdout.strip() == "18"
    assert run(first, "check", "revision").returncode == 0
