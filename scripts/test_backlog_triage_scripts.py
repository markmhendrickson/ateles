"""Tests for the two one-shot backlog scripts required by ateles#511.

Both are operator-assisted tools that act on the PR thread, so the properties
worth pinning are the ones that make them safe to run: exactly one verdict per
PR, no silent bulk action, a dry run by default, and — for the drain — that it
never merges anything.

The `gh` boundary is stubbed; these assert the DECISIONS, not GitHub.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import drain_approved_prs as drain  # noqa: E402
import triage_cr_backlog as triage  # noqa: E402


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class _Gh:
    """Records every gh invocation and replays canned JSON."""

    def __init__(self, prs: list[dict], comments: str = ""):
        self.prs = prs
        self.comments = comments
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if args[:2] == ["pr", "list"]:
            return json.dumps(self.prs)
        if args[:2] == ["pr", "view"]:
            return json.dumps({"comments": [{"body": self.comments}]})
        return ""

    def did(self, *fragment: str) -> bool:
        return any(list(fragment) == c[: len(fragment)] for c in self.calls)


# ── triage_cr_backlog ───────────────────────────────────────────────────────


def test_only_pre_cutover_changes_requested_prs_are_candidates(monkeypatch):
    gh = _Gh(
        [
            {"number": 1, "title": "old CR", "createdAt": "2026-07-01T00:00:00Z",
             "reviewDecision": "CHANGES_REQUESTED", "url": ""},
            {"number": 2, "title": "new CR", "createdAt": "2026-08-20T00:00:00Z",
             "reviewDecision": "CHANGES_REQUESTED", "url": ""},
            {"number": 3, "title": "old approved", "createdAt": "2026-07-01T00:00:00Z",
             "reviewDecision": "APPROVED", "url": ""},
        ]
    )
    monkeypatch.setattr(triage, "_gh", gh)

    assert [p["number"] for p in triage.list_candidates("o/r")] == [1]


def test_a_pr_with_a_standing_verdict_is_not_offered_again(monkeypatch):
    gh = _Gh(
        [{"number": 1, "title": "x", "createdAt": "2026-07-01T00:00:00Z",
          "reviewDecision": "CHANGES_REQUESTED", "url": ""}],
        comments=triage.REVIVE_MARKER,
    )
    monkeypatch.setattr(triage, "_gh", gh)

    assert triage.list_candidates("o/r") == []


def test_dry_run_posts_nothing(monkeypatch):
    gh = _Gh([])
    monkeypatch.setattr(triage, "_gh", gh)

    out = triage.record_verdict("o/r", 5, "close", "abandoned", apply=False)

    assert "DRY RUN" in out
    assert gh.calls == [], "a dry run must not touch the PR"


def test_close_verdict_comments_then_closes(monkeypatch):
    gh = _Gh([])
    monkeypatch.setattr(triage, "_gh", gh)

    triage.record_verdict("o/r", 5, "close", "superseded by #600", apply=True)

    assert gh.did("pr", "comment", "5")
    assert gh.did("pr", "close", "5")
    body = next(c for c in gh.calls if c[:2] == ["pr", "comment"])[-1]
    assert triage.CLOSE_MARKER in body
    assert "superseded by #600" in body


def test_revive_verdict_never_closes(monkeypatch):
    gh = _Gh([])
    monkeypatch.setattr(triage, "_gh", gh)

    triage.record_verdict("o/r", 5, "revive", "author still pushing", apply=True)

    assert gh.did("pr", "comment", "5")
    assert not gh.did("pr", "close", "5"), "revive must never close the PR"


def test_a_second_verdict_is_refused(monkeypatch):
    gh = _Gh([], comments=triage.CLOSE_MARKER)
    monkeypatch.setattr(triage, "_gh", gh)

    out = triage.record_verdict("o/r", 5, "revive", "changed my mind", apply=True)

    assert "SKIPPED" in out
    assert not gh.did("pr", "comment", "5"), "exactly one verdict per PR (#511)"


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_verdict_without_a_reason_is_rejected(monkeypatch, reason):
    monkeypatch.setattr(triage, "_gh", _Gh([]))
    with pytest.raises(ValueError):
        triage.record_verdict("o/r", 5, "close", reason, apply=True)


# ── drain_approved_prs ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state, expect_hold",
    [("CLEAN", False), ("DIRTY", True), ("BLOCKED", True), ("BEHIND", True)],
)
def test_merge_state_decides_whether_a_hold_is_recorded(state, expect_hold):
    reason, _ = drain.classify({"mergeStateStatus": state})
    assert bool(reason) is expect_hold


def test_only_approved_prs_are_drained(monkeypatch):
    gh = _Gh(
        [
            {"number": 1, "reviewDecision": "APPROVED", "mergeStateStatus": "DIRTY",
             "title": "a", "updatedAt": _iso(40), "url": ""},
            {"number": 2, "reviewDecision": "CHANGES_REQUESTED",
             "mergeStateStatus": "CLEAN", "title": "b", "updatedAt": _iso(3), "url": ""},
        ]
    )
    monkeypatch.setattr(drain, "_gh", gh)

    assert [p["number"] for p in drain.list_approved("o/r")] == [1]


def test_the_drain_never_merges(monkeypatch):
    gh = _Gh(
        [
            {"number": 1, "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN",
             "title": "ready", "updatedAt": _iso(50), "url": ""},
            {"number": 2, "reviewDecision": "APPROVED", "mergeStateStatus": "DIRTY",
             "title": "rotted", "updatedAt": _iso(33), "url": ""},
        ]
    )
    monkeypatch.setattr(drain, "_gh", gh)

    drain.main(["--repo", "o/r", "--apply"])

    # The gh SUBCOMMAND, not the word: the hold comment legitimately explains
    # why a PR is not merging, so a substring match over the whole argv would
    # fail on its own prose.
    assert not any(c[:2] == ["pr", "merge"] for c in gh.calls), (
        "merging stays behind the existing merge gates (#511)"
    )


def test_dry_run_records_no_hold(monkeypatch):
    gh = _Gh(
        [{"number": 2, "reviewDecision": "APPROVED", "mergeStateStatus": "DIRTY",
          "title": "rotted", "updatedAt": _iso(33), "url": ""}]
    )
    monkeypatch.setattr(drain, "_gh", gh)

    drain.main(["--repo", "o/r"])

    assert not gh.did("pr", "comment", "2")


def test_hold_marker_carries_the_reason(monkeypatch):
    gh = _Gh([])
    monkeypatch.setattr(drain, "_gh", gh)

    drain.record_hold("o/r", 7, "conflict", "needs a rebase", apply=True)

    body = next(c for c in gh.calls if c[:2] == ["pr", "comment"])[-1]
    assert "<!-- apis-approved-hold:conflict -->" in body


def test_the_same_hold_is_not_posted_twice(monkeypatch):
    gh = _Gh([], comments="<!-- apis-approved-hold:conflict -->")
    monkeypatch.setattr(drain, "_gh", gh)

    out = drain.record_hold("o/r", 7, "conflict", "needs a rebase", apply=True)

    assert "SKIPPED" in out
    assert not gh.did("pr", "comment", "7")
