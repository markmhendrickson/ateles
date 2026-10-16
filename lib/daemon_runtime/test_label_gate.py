"""Unit tests for the shared swarm label gate (ateles#1269).

Apis and Anthus both decide "is this issue/PR labelled?" through
lib/daemon_runtime/label_gate. These pin the rules both inherit: unset is
off, the match is exact, and malformed data denies instead of raising.
"""

from __future__ import annotations

import pytest

from lib.daemon_runtime import label_gate as lg

CANARY = "swarm-canary"


@pytest.mark.parametrize(
    "env,expected",
    [({}, ""), ({lg.REQUIRE_LABEL_ENV: ""}, ""), ({lg.REQUIRE_LABEL_ENV: "  "}, ""),
     ({lg.REQUIRE_LABEL_ENV: f" {CANARY} "}, CANARY)],
)
def test_required_label_normalizes(env, expected):
    assert lg.required_label(env) == expected


def test_carries_label_is_exact():
    assert lg.carries_label(CANARY, ["bug", CANARY])
    assert not lg.carries_label(CANARY, ["Swarm-Canary", f"{CANARY} ", None, 5])
    assert not lg.carries_label("", [""]), "an empty gate is off, not a match"


@pytest.mark.parametrize(
    "obj,expected",
    [
        ({"labels": [{"name": CANARY}, {"name": "bug"}]}, [CANARY, "bug"]),
        ({"labels": [CANARY]}, []),
        ({"labels": None}, []),
        ({"labels": CANARY}, []),
        ({"labels": {"name": CANARY}}, []),
        ({"labels": [{"name": None}, {"name": 5}, {}, 7]}, []),
        (None, []),
        ("x", []),
    ],
)
def test_label_names_never_raises(obj, expected):
    assert lg.label_names(obj) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ([CANARY, "bug"], [CANARY, "bug"]),
        ([{"name": CANARY}], [CANARY]),
        (f'["{CANARY}", "bug"]', [CANARY, "bug"]),
        (f"bug, {CANARY}", ["bug", CANARY]),
        ("[broken", []),
        ("", []),
        (None, []),
        (3, []),
        ([None, 4, {"nam": CANARY}, ""], []),
    ],
)
def test_snapshot_label_names(raw, expected):
    assert lg.snapshot_label_names(raw) == expected


@pytest.mark.parametrize(
    "body,repo,expected",
    [
        ("Closes #12", "o/r", 12),
        ("Part of #7 and closes #9", "o/r", 7),
        ("Refs o/r#3", "o/r", 3),
        ("Closes other/repo#3", "o/r", None),
        ("see #5 for background", "o/r", None),
        ("", "o/r", None),
        (None, "o/r", None),
    ],
)
def test_parent_issue_number(body, repo, expected):
    assert lg.parent_issue_number(body, repo) == expected
