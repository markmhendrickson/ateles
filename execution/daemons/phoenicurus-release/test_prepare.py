"""
Tests for prepare.py idempotency locks — the auto-release --on-merge path.

Two independent locks:
  - the scheduled path (once per calendar day, .phoenicurus_prepare_last_run)
  - the on-merge path (once per origin/main commit, .phoenicurus_prepare_last_sha)

The subtle case is a TRANSIENT deferral (CI in progress or CI red) in on-merge
mode: it must NOT stamp the SHA, so the check_suite-completion retry can prepare
that same head once CI settles. Stamping there is the bug Loxia flagged on
ateles#253 — it would burn the per-commit lock on a run that did nothing.

Run: pytest execution/daemons/phoenicurus-release/test_prepare.py -v
"""

from __future__ import annotations

import pytest

import prepare


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point both lock files at a temp dir so tests never touch real state."""
    monkeypatch.setattr(prepare, "MERGE_STATE_FILE", tmp_path / ".sha")
    monkeypatch.setattr(prepare, "STATE_FILE", tmp_path / ".day")
    return tmp_path


SHA = "a" * 40


def test_on_merge_terminal_run_stamps_sha(isolated_state):
    assert not prepare._already_ran_for_sha(SHA)
    prepare._mark_ran(on_merge=True, head=SHA)
    assert prepare._already_ran_for_sha(SHA), "a completed on-merge run locks its SHA"


def test_on_merge_new_commit_not_locked_by_prior(isolated_state):
    prepare._mark_ran(on_merge=True, head=SHA)
    assert not prepare._already_ran_for_sha("b" * 40), "a new merge commit is a fresh run"


def test_transient_deferral_does_not_stamp_sha(isolated_state):
    # CI in-progress / red: the SHA must stay unlocked so the check_suite retry
    # can prepare this head once CI goes green.
    prepare._mark_ran(on_merge=True, head=SHA, transient=True)
    assert not prepare._already_ran_for_sha(SHA), (
        "a transient on-merge deferral must leave the SHA unstamped for retry"
    )


def test_scheduled_path_stamps_day_even_when_transient(isolated_state):
    # The scheduled sweep is deliberately once-a-day; a same-day retry is not
    # wanted there, so it stamps regardless of transience.
    prepare._mark_ran(on_merge=False, head="", transient=True)
    assert prepare._already_ran_today()


def test_locks_are_independent(isolated_state):
    # An on-merge run must not consume the daily lock (or the scheduled safety
    # net would be suppressed by webhook activity), and vice versa.
    prepare._mark_ran(on_merge=True, head=SHA)
    assert not prepare._already_ran_today(), "on-merge run must not consume the daily lock"
    prepare._mark_ran(on_merge=False, head="")
    assert prepare._already_ran_for_sha(SHA), "daily stamp must not clear the SHA lock"
