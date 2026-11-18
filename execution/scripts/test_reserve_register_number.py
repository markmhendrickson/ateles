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


def _load_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("reserve_register_number_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_same_number_has_exactly_one_winner_even_from_the_same_base(clones: tuple[Path, Path, Path]) -> None:
    """Both claimants start from the same origin/main commit and claim the same number one after the other.

    Pushing that shared commit to the ref twice would report success twice ("up-to-date"); a
    distinct object per claim makes the second claimant lose deterministically.
    """
    _, first, second = clones
    module = _load_module()
    base = git(first, "rev-parse", "origin/main")
    previous = Path.cwd()
    try:
        import os

        os.chdir(first)
        won_first, _ = module.claim("decision", "origin", 41, base)
        os.chdir(second)
        git(second, "fetch", "origin")
        won_second, _ = module.claim("decision", "origin", 41, base)
    finally:
        os.chdir(previous)
    assert won_first is True
    assert won_second is False
    assert git(first, "ls-remote", "origin", "refs/reservations/decision/41").count("\n") == 0
    # the loser then takes the next number
    assert run(second, "reserve", "decision").stdout.strip() == "42"


def test_pushing_the_shared_base_twice_is_not_a_second_claim(clones: tuple[Path, Path, Path]) -> None:
    """Documents why the claim object must be unique: the same commit pushed twice reports success twice."""
    _, first, second = clones
    sha = git(first, "rev-parse", "HEAD")
    ref = "refs/reservations/decision/99"
    argv = ["git", "push", f"--force-with-lease={ref}:", "origin", f"{sha}:{ref}"]
    assert command(first, *argv).returncode == 0
    git(second, "fetch", "origin")
    assert command(second, *argv).returncode == 0


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


def test_range_row_needs_a_reservation_for_every_number_it_covers(clones: tuple[Path, Path, Path]) -> None:
    _, first, _ = clones
    register = first / "docs" / "foundation" / "conformance.md"
    register.write_text(register.read_text() + "| 41\u201343 | a range | `doc.md#r` | \u2014 | **ruled** |\n")
    missing = run(first, "check", "decision")
    assert missing.returncode == 1
    for number in (41, 42, 43):
        assert f"decision {number} has no reservation" in missing.stderr
    for _ in range(3):
        run(first, "reserve", "decision")
    assert run(first, "check", "decision").returncode == 0
    # an ASCII hyphen range is the same row form
    register.write_text(register.read_text().replace("41\u201343", "41-43"))
    assert run(first, "check", "decision").returncode == 0


def test_range_row_occupies_every_number_for_next(clones: tuple[Path, Path, Path]) -> None:
    _, first, _ = clones
    register = first / "docs" / "foundation" / "conformance.md"
    register.write_text(register.read_text() + "| 41\u201348 | a range | `doc.md#r` | \u2014 | **ruled** |\n")
    git(first, "add", ".")
    git(first, "commit", "-m", "range on main")
    git(first, "push", "origin", "main")
    assert run(first, "next", "decision").stdout.strip() == "49"


def test_duplicate_revision_row_fails(clones: tuple[Path, Path, Path]) -> None:
    _, first, _ = clones
    revisions = first / "docs" / "foundation" / "revisions.md"
    revisions.write_text(revisions.read_text() + "| 17 | pass | reuses a number already on main |\n")
    result = run(first, "check", "revision")
    assert result.returncode == 1
    assert "revision 17 already exists" in result.stderr


def test_one_new_revision_may_appear_in_several_document_tables(clones: tuple[Path, Path, Path]) -> None:
    """A pass that amends several documents adds one row to each table under ONE number."""
    _, first, _ = clones
    revisions = first / "docs" / "foundation" / "revisions.md"
    revisions.write_text(revisions.read_text() + "| 18 | pass | first doc |\n\n## Other\n| 18 | pass | second doc |\n")
    assert run(first, "check", "revision").returncode == 1  # not reserved yet
    assert run(first, "reserve", "revision").stdout.strip() == "18"
    assert run(first, "check", "revision").returncode == 0


def test_an_unreasonable_range_is_rejected(clones: tuple[Path, Path, Path]) -> None:
    _, first, _ = clones
    register = first / "docs" / "foundation" / "conformance.md"
    register.write_text(register.read_text() + "| 41\u2013999999 | huge | `doc.md#r` | \u2014 | **ruled** |\n")
    result = run(first, "check", "decision")
    assert result.returncode == 1
    assert "not a valid range" in result.stderr
