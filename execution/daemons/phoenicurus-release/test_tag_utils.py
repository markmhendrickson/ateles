"""
Tests for tag_utils.py — the calendar-tag and release-guard logic used by
.github/workflows/ateles-release.yml (extracted from inline bash per the
Phoenicurus QA review on ateles#253, so it runs in the regular pytest suite
instead of needing an `act`/Docker-based YAML execution to exercise).

Run: pytest execution/daemons/phoenicurus-release/test_tag_utils.py -v
"""

from __future__ import annotations

import tag_utils


def test_first_tag_of_day():
    assert tag_utils.next_calendar_tag(set(), "2026.07.23") == "v2026.07.23"


def test_second_tag_same_day_gets_dot_one():
    existing = {"v2026.07.23"}
    assert tag_utils.next_calendar_tag(existing, "2026.07.23") == "v2026.07.23.1"


def test_third_tag_same_day_gets_dot_two():
    existing = {"v2026.07.23", "v2026.07.23.1"}
    assert tag_utils.next_calendar_tag(existing, "2026.07.23") == "v2026.07.23.2"


def test_zero_non_merge_commits_guard_returns_false():
    assert tag_utils.has_releasable_commits("v2026.07.23", 0) is False


def test_no_previous_tag_initial_release():
    assert tag_utils.has_releasable_commits(None, 0) is True
    assert tag_utils.has_releasable_commits("", 0) is True


def test_nonzero_non_merge_commits_is_releasable():
    assert tag_utils.has_releasable_commits("v2026.07.23", 3) is True
