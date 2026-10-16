"""The size of a re-review round's change: the PR's own interdiff.

The case behind these tests, measured 2026-09-29: a re-round whose branch had
merged the base branch since the last review read as 1744 changed lines
touching security paths, because everything the base brought in sat between the
two heads. The delta must be the PR's OWN change since the last review.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import review_delta  # noqa: E402


def _file(name: str, patch: str | None, **extra) -> dict:
    entry: dict = {"filename": name, "status": "modified"}
    if patch is not None:
        entry["patch"] = patch
        entry["additions"] = sum(1 for l in patch.splitlines() if l[:1] == "+")
        entry["deletions"] = sum(1 for l in patch.splitlines() if l[:1] == "-")
        entry["changes"] = entry["additions"] + entry["deletions"]
    entry.update(extra)
    return entry


def _patch(*changed: str, start: int = 10, context: tuple[str, ...] = ("ctx",)) -> str:
    """A one-hunk patch: context, the changed lines, context."""
    body = [f" {c}" for c in context] + list(changed) + [f" {c}" for c in context]
    return f"@@ -{start},{len(body)} +{start},{len(body)} @@ def f():\n" + "\n".join(body)


def test_an_identical_pr_side_diff_has_no_delta():
    files = [_file("src/a.py", _patch("+one", "-two"))]
    assert review_delta.interdiff(files, files) == review_delta.Delta(0, ())


def test_base_branch_content_between_the_heads_is_not_part_of_the_delta():
    """Merging the base branch shifts the hunks around the PR's own lines. A file
    whose blob is the same at both heads is the same file, whatever the patch
    text around it now says: no delta."""
    before = [
        _file("src/a.py", _patch("+one", "-two", start=10, context=("old ctx",)), sha="s1"),
        _file(".claude/hooks/gate.py", _patch("+guard", start=5), sha="s2"),
    ]
    after = [
        _file("src/a.py", _patch("+one", "-two", start=400, context=("main moved this",)), sha="s1"),
        _file(".claude/hooks/gate.py", _patch("+guard", start=77), sha="s2"),
    ]
    assert review_delta.interdiff(before, after) == review_delta.Delta(0, ())


def test_a_changed_file_is_never_reported_as_no_delta_even_when_its_change_lines_match():
    """Security review of ateles#1368: the same +/- lines at a different place in
    the file (a guard moved past the call it protects) measured 0 lines, so the
    file read as untouched. A file present on both sides whose blob or patch
    differs is changed, at least one line."""
    before = [_file("src/auth/x.ts", _patch("+check()", start=10, context=("a", "b")), sha="s1")]
    after = [_file("src/auth/x.ts", _patch("+check()", start=10, context=("b", "a")), sha="s2")]
    delta = review_delta.interdiff(before, after)
    assert delta.files == ("src/auth/x.ts",) and delta.lines >= 1


def test_without_blobs_a_differing_patch_is_still_a_change():
    before = [_file("src/a.py", _patch("+one", start=10, context=("x",)))]
    after = [_file("src/a.py", _patch("+one", start=90, context=("y",)))]
    assert review_delta.interdiff(before, after).files == ("src/a.py",)


def test_only_what_the_pr_changed_since_the_review_is_counted():
    before = [_file("src/a.py", _patch("+one")), _file("src/b.py", _patch("+b1"))]
    after = [
        _file("src/a.py", _patch("+one", "+added after review")),
        _file("src/b.py", _patch("+b1")),
    ]
    delta = review_delta.interdiff(before, after)
    assert delta == review_delta.Delta(1, ("src/a.py",))


def test_a_reworked_line_counts_as_one_removal_and_one_addition():
    before = [_file("src/a.py", _patch("+value = 1"))]
    after = [_file("src/a.py", _patch("+value = 2"))]
    assert review_delta.interdiff(before, after).lines == 2


def test_a_file_the_pr_started_or_stopped_touching_counts_in_full():
    before = [_file("src/old.py", _patch("+a", "+b", "+c"))]
    after = [_file("execution/hooks/new.py", _patch("+x", "+y"))]
    delta = review_delta.interdiff(before, after)
    assert delta.lines == 5
    assert set(delta.files) == {"src/old.py", "execution/hooks/new.py"}


def test_a_block_of_the_pr_that_only_moved_still_counts_as_changed():
    """Sequence, not set, matching: reordering the PR's own lines is a change
    the reviewer has not seen, so it must not read as zero."""
    before = [_file("src/a.py", _patch("+def a():", "+    pass", "+def b():", "+    pass2"))]
    after = [_file("src/a.py", _patch("+def b():", "+    pass2", "+def a():", "+    pass"))]
    assert review_delta.interdiff(before, after).lines > 0


def test_a_rename_reports_both_paths_so_a_moved_security_path_is_seen():
    before = [_file("docs/x.md", _patch("+a"))]
    after = [
        _file(
            "docs/y.md", _patch("+a", "+b"),
            status="renamed", previous_filename=".claude/hooks/x.md",
        )
    ]
    # docs/x.md vanished (counted in full) and docs/y.md is new to the PR side.
    delta = review_delta.interdiff(before, after)
    assert {"docs/x.md", "docs/y.md", ".claude/hooks/x.md"} <= set(delta.files)


def test_a_file_without_a_patch_is_never_under_measured():
    """A binary or oversized file has no patch: equal blobs are unchanged,
    anything else counts at its full size, never zero."""
    same = [_file("img.png", None, sha="abc", changes=0)]
    assert review_delta.interdiff(same, same).lines == 0
    old = [_file("big.txt", None, sha="abc", changes=900)]
    new = [_file("big.txt", None, sha="def", changes=950)]
    assert review_delta.interdiff(old, new) == review_delta.Delta(950, ("big.txt",))
    binary_old = [_file("img.png", None, sha="abc", changes=0)]
    binary_new = [_file("img.png", None, sha="def", changes=0)]
    assert review_delta.interdiff(binary_old, binary_new).lines >= 1


def test_a_patch_on_one_side_only_is_treated_as_unreadable_not_as_equal():
    old = [_file("src/a.py", _patch("+one"), sha="abc")]
    new = [_file("src/a.py", None, sha="def", changes=40)]
    assert review_delta.interdiff(old, new).lines == 40


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"files": None},
        {"files": [{"patch": "x"}]},  # no filename
        {"files": [_file(f"f{i}.py", _patch("+x")) for i in range(300)]},  # may be truncated
    ],
)
def test_an_untrustworthy_compare_yields_no_file_list(body):
    assert review_delta.compare_files(body) is None


def test_a_normal_compare_yields_its_files():
    files = [_file("a.py", _patch("+x"))]
    assert review_delta.compare_files({"files": files}) == files
    assert review_delta.compare_files(
        {"files": [_file(f"f{i}.py", _patch("+x")) for i in range(299)]}
    ) is not None
