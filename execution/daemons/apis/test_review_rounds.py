"""Genuine review rounds, ordered by the fetched comment history."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parents[2])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import review_rounds as rr  # noqa: E402
import lens_authors  # noqa: E402
import swarm_dispatch  # noqa: E402

H1, H2, H3, H4 = (str(i) * 40 for i in range(1, 5))
AGENTS = {"pm": "Pavo", "arch": "Waxwing", "qa": "Phoenicurus", "security": "Falco"}


def _verdict(lens: str, head: str, *, verdict: str = "SIGNED_OFF", n: int = 0, extra: str = "") -> dict:
    marker = swarm_dispatch.compose_lens_review_marker(lens, head)
    body = f"{marker}\n**\U0001f916 {AGENTS[lens]} — Ateles swarm, {lens} review**\n**{verdict}**\n{extra}"
    return {"id": n, "body": body, "created_at": f"2026-10-27T10:{n:02d}:00Z"}


def _status(comments, head):
    return rr.round_status(swarm_dispatch.lens_records(comments), head)


def test_cap_stays_three():
    assert rr.ROUND_CAP == 3
    assert rr.CAP_REACHED == "round cap reached: split or cut scope"


def test_unreviewed_head_is_a_candidate_not_an_all_clear_round():
    status = _status([], H1)
    assert status.round_number == 1
    assert "round 1 started: first review episode; candidate 1111111 awaiting lens verdicts" in status.describe()
    assert "all clear" not in status.describe()


@pytest.mark.parametrize("prefix", ["    ", "> ", "quoted marker:\n"])
def test_embedded_marker_does_not_enter_round_history(prefix):
    row = _verdict("pm", H1, n=1)
    row["body"] = prefix + row["body"]
    assert swarm_dispatch.lens_records([row]) == {}


@pytest.mark.parametrize("spelling,recognized", [
    ("first", True),
    ("retired", True),
    ("indented", False),
    ("quoted", False),
    ("fenced", False),
    ("mid_prose", False),
    ("uppercase", True),
    ("extra_spaces", True),
    ("crlf", True),
])
def test_author_guard_and_round_builder_agree_on_verdict_marker(spelling, recognized):
    marker = swarm_dispatch.compose_lens_review_marker("pm", H1)
    body = {
        "first": marker + "\nbody",
        "retired": swarm_dispatch.compose_superseded_verdict(marker + "\nbody", H2),
        "indented": "    " + marker + "\nbody",
        "quoted": "> " + marker + "\nbody",
        "fenced": "```\n" + marker + "\n```",
        "mid_prose": "See this marker: " + marker,
        "uppercase": marker.upper() + "\nbody",
        "extra_spaces": marker.replace("<!-- review:", "<!--   review:").replace(" commit=", "  commit=") + "\nbody",
        "crlf": marker + "\r\nbody",
    }[spelling]
    row = {"id": 7, "body": body, "created_at": "2026-10-27T10:00:00Z"}
    assert bool(lens_authors.verdict_marker(body)) is recognized
    assert bool(swarm_dispatch.lens_records([row])) is recognized
    if recognized:
        with pytest.raises(ValueError, match="unresolved author"):
            lens_authors.require_marked_author(row)
    else:
        lens_authors.require_marked_author(row)


@pytest.mark.parametrize("verdict,expected", [
    ("SIGNED_OFF", "all clear"), ("BLOCKED", "blocking lens findings"),
])
def test_fresh_head_keeps_earlier_outcome_and_awaits_verdicts(verdict, expected):
    status = _status([_verdict("pm", H1, verdict=verdict)], H2)
    assert status.round_number == (1 if verdict == "SIGNED_OFF" else 2)
    assert expected in status.describe()
    assert "candidate 2222222 awaiting lens verdicts" in status.describe()


def test_override_requires_repository_owner_to_equal_operator():
    row = {
        "id": 1,
        "body": "/override-round-cap\nA sufficiently detailed synthetic reason.",
        "user": {"login": "other-owner", "type": "User"},
        "author_association": "OWNER",
        "created_at": "2026-10-27T10:00:00Z",
        "updated_at": "2026-10-27T10:00:00Z",
        "html_url": "https://github.com/example/repo/pull/7#issuecomment-1",
    }
    assert rr.operator_override([row], "other-owner", "operator", "example/repo", 7) is None
    assert rr.operator_override([row], "other-owner", "other-owner", "example/repo", 7) is row


@pytest.mark.parametrize("verdict,kind,blocking", [
    ("SIGNED_OFF", "signed_off", False),
    ("APPROVE", "signed_off", False),
    ("BLOCKED", "blocked", True),
    ("REQUEST_CHANGES", "blocked", True),
    ("CHANGES_REQUESTED", "unreadable", True),
    ("COMMENT", "comment", False),
    ("unreadable", "unreadable", True),
])
def test_poster_classification_agrees_with_counter_for_every_verdict_kind(verdict, kind, blocking):
    row = _verdict("pm", H1, verdict=verdict, n=1)
    assert rr.verdict_class(row["body"], "pm") == (kind, blocking)
    record = swarm_dispatch.lens_records([row])["pm"][0]
    assert record.blocking is blocking


def test_comment_with_explicit_objection_uses_counters_blocking_state():
    row = _verdict("pm", H1, verdict="COMMENT", n=1, extra="[BLOCKING] unresolved\n")
    assert rr.verdict_class(row["body"], "pm") == ("comment", True)
    assert swarm_dispatch.lens_records([row])["pm"][0].blocking


def test_clear_repushes_and_sites_private_shape_spend_only_second_round():
    comments = [_verdict("pm", H1, verdict="REQUEST_CHANGES", n=0)]
    heads = [f"{i:040x}" for i in range(2, 22)]
    comments += [_verdict("pm", head, n=i + 1) for i, head in enumerate(heads)]
    status = _status(comments, heads[-1])
    assert status.round_number == 2 and not status.refused
    assert len(status.rounds) == 2 and len(status.rounds[1].heads) == 20
    assert "blocking lens findings" in status.describe()
    assert "all clear" in status.describe()


def test_blocking_verdict_followed_by_new_head_starts_round():
    comments = [_verdict("pm", H1, n=0), _verdict("arch", H1, verdict="BLOCKED", n=1)]
    status = _status(comments, H2)
    assert status.round_number == 2
    assert "round 1 started: first review episode" in status.describe()
    assert "round 2 started: the previous episode was blocking" in status.describe()


def test_three_blocking_rounds_then_fourth_head_refused():
    comments = [_verdict("pm", head, verdict="REQUEST_CHANGES", n=i) for i, head in enumerate((H1, H2, H3))]
    status = _status(comments, H4)
    assert status.round_number == 4 and status.refused
    assert rr.CAP_REACHED in status.describe()
    assert all(head[:7] in status.describe() for head in (H1, H2, H3, H4))


@pytest.mark.parametrize("verdict,extra", [("nonsense", ""), ("SIGNED_OFF", "[BLOCKING] real")])
def test_unreadable_or_tagged_verdict_spends_round(verdict, extra):
    assert _status([_verdict("pm", H1, verdict=verdict, extra=extra)], H2).round_number == 2


def test_current_heads_own_repeat_run_is_not_another_round():
    comments = [_verdict("pm", H1, verdict="BLOCKED", n=0), _verdict("pm", H2, n=1)]
    assert _status(comments, H2).round_number == 2
    assert _status(comments + [_verdict("arch", H2, n=2)], H2).round_number == 2


def test_comment_list_order_governs_even_when_timestamps_disagree():
    comments = [_verdict("arch", H2, n=2), _verdict("pm", H1, verdict="BLOCKED", n=0)]
    status = _status(comments, H2)
    assert status.round_number == 2 and status.rounds[0].heads == (H2, H1)


def test_unknown_lens_and_unmarked_comment_do_not_spend_round():
    comments = [{"body": f"<!-- review:invented commit={H1} -->", "created_at": "2026-10-27T10:00:00Z"}, {"body": "review waiting"}]
    assert _status(comments, H2).round_number == 1


def test_superseded_blocking_verdict_still_spends_round():
    old = _verdict("pm", H1, verdict="BLOCKED", n=0)
    old["body"] = swarm_dispatch.compose_superseded_verdict(old["body"], H2)
    assert _status([old], H2).round_number == 2


@pytest.mark.parametrize("bad_time", [
    "", None, "   ", "yesterday", "2026-10-27T10:00:00", "2026-02-30T10:00:00Z",
    123, "2019-12-31T23:00:00Z", "2101-01-01T00:00:00Z", "2099-01-01T00:00:00Z",
])
@pytest.mark.parametrize("bad_head", [H1, H4])
def test_missing_or_unparseable_timestamp_fails_closed(bad_time, bad_head):
    comments = [_verdict("pm", head, verdict="BLOCKED", n=i) for i, head in enumerate((H1, H2, H3))]
    comments.append(_verdict("pm", H4, n=4))
    affected = next(comment for comment in comments if bad_head in comment["body"])
    affected["created_at"] = bad_time
    status = _status(comments, H4)
    assert status.refused
    assert status.indeterminate_head == bad_head
    assert f"round order indeterminate: a verdict timestamp is missing or malformed for {bad_head}" in status.describe()


@pytest.mark.parametrize("bad_head", [H1, H4])
def test_absent_timestamp_fails_closed(bad_head):
    comments = [_verdict("pm", head, verdict="BLOCKED", n=i) for i, head in enumerate((H1, H2, H3))]
    comments.append(_verdict("pm", H4, n=4))
    next(comment for comment in comments if bad_head in comment["body"]).pop("created_at")
    assert _status(comments, H4).refused


def test_valid_offsets_are_normalized_to_utc_before_ordering():
    first = _verdict("pm", H1, verdict="BLOCKED", n=0)
    second = _verdict("pm", H2, n=1)
    first["created_at"] = "2026-10-27T12:00:00+02:00"
    second["created_at"] = "2026-10-27T10:01:00Z"
    status = _status([second, first], H2)
    assert status.round_number == 2 and not status.refused
    assert status.rounds[0].heads == (H2, H1)


def test_reset_to_earlier_clear_head_spends_after_blocking_history():
    comments = [_verdict("pm", head, verdict="BLOCKED" if head != H2 else "SIGNED_OFF", n=i)
                for i, head in enumerate((H1, H2, H3, H4))]
    status = _status(comments, H2)
    assert status.round_number == 4 and status.refused
    assert status.rounds[-1].heads == ()
    assert status.rounds[-1].awaiting_head == H2
    assert all(head[:7] in status.describe() for head in (H1, H2, H3, H4))
    assert "whole verdict history" in status.describe()


def test_reset_to_earlier_head_after_all_clear_history_does_not_spend():
    comments = [_verdict("pm", head, n=i) for i, head in enumerate((H1, H2, H3, H4))]
    status = _status(comments, H2)
    assert status.round_number == 1 and not status.refused
    assert status.rounds[0].heads == (H1, H2, H3, H4)
    assert status.rounds[0].awaiting_head == H2


def test_rerun_on_latest_reviewed_head_spends_no_further_round():
    comments = [_verdict("pm", head, verdict="BLOCKED", n=i) for i, head in enumerate((H1, H2, H3, H4))]
    assert _status(comments, H4).round_number == 4
    assert _status(comments + [_verdict("arch", H4, n=5)], H4).round_number == 4


def test_revisited_heads_form_distinct_episodes_and_refuse_at_cap():
    comments = [
        _verdict("pm", head, verdict=verdict, n=i + 1)
        for i, (head, verdict) in enumerate((
            (H1, "BLOCKED"), (H2, "SIGNED_OFF"), (H1, "BLOCKED"),
            (H3, "BLOCKED"), (H4, "SIGNED_OFF"),
        ))
    ]
    status = _status(comments, H4)
    # Returning to H1 after a blocking episode is a new round even though the
    # episode before it (H2) was clear: five rounds in all.
    assert status.round_number == 5 and status.refused
    assert [round_.heads for round_ in status.rounds] == [
        (H1,), (H2,), (H1,), (H3,), (H4,),
    ]


def test_block_then_clear_on_one_episode_still_spends_next_round():
    comments = [_verdict("pm", H1, verdict="BLOCKED", n=1),
                _verdict("pm", H1, n=2), _verdict("pm", H2, n=3)]
    assert _status(comments, H2).round_number == 2


def test_revisit_with_each_previous_episode_blocking_spends_five_rounds():
    comments = [_verdict("pm", head, verdict="BLOCKED", n=i + 1)
                for i, head in enumerate((H1, H2, H1, H3))]
    comments.append(_verdict("pm", H4, n=5))
    status = _status(comments, H4)
    assert status.round_number == 5 and status.refused
    assert [round_.heads for round_ in status.rounds] == [(H1,), (H2,), (H1,), (H3,), (H4,)]


def test_security_revisit_spends_four_rounds():
    comments = [_verdict("security", head, verdict="BLOCKED", n=i + 1)
                for i, head in enumerate((H1, H2, H1))]
    comments.append(_verdict("security", H3, n=4))
    assert _status(comments, H3).round_number == 4


def test_equal_timestamps_use_comment_list_order_not_ids_or_lens_buckets():
    high = "f" * 40
    low = "0" * 40
    comments = [_verdict("arch", low, verdict="BLOCKED", n=20),
                _verdict("pm", high, verdict="BLOCKED", n=10),
                _verdict("pm", high, n=30)]
    comments[0]["created_at"] = comments[1]["created_at"]
    status = _status(comments, high)
    assert status.round_number == 2 and status.refused is False
    assert [round_.heads for round_ in status.rounds] == [(low,), (high,)]


def test_equal_timestamp_same_comment_id_uses_source_order():
    comments = [_verdict("pm", H1, verdict="BLOCKED", n=1),
                _verdict("arch", H2, verdict="BLOCKED", n=2)]
    comments[1]["created_at"] = comments[0]["created_at"]
    comments[1]["id"] = comments[0]["id"]
    assert _status(comments, H2).rounds[0].heads == (H1,)


@pytest.mark.parametrize("timestamp", [None, "2026-10-27T10:00:00Z"])
def test_source_order_keeps_later_objection_after_clearance(timestamp):
    comments = [_verdict("arch", H1, n=20),
                _verdict("arch", H2, verdict="BLOCKED", n=10)]
    comments[0]["created_at"] = timestamp
    comments[1]["created_at"] = timestamp
    records = swarm_dispatch.lens_records(comments)["arch"]
    ordered, known = rr.order_lens_records(reversed(records))
    assert known and [r.head for r in ordered] == [H1, H2]


def test_panel_completion_uses_each_lens_latest_comment():
    clear = _verdict("arch", H1, n=1)
    unreadable = _verdict("arch", H1, verdict="unreadable", n=2)
    assert not swarm_dispatch.panel_finished_at_head([clear, unreadable], H1, {"arch"})
    assert swarm_dispatch.panel_finished_at_head([unreadable, clear], H1, {"arch"})


def test_missing_comment_id_with_unique_timestamp_still_has_known_order():
    comments = [_verdict("pm", H1, verdict="BLOCKED", n=1),
                _verdict("pm", H2, n=2)]
    comments[0]["id"] = None
    status = _status(comments, H2)
    assert status.round_number == 2 and not status.refused


@pytest.mark.parametrize("bad_id", [None, "not-an-integer", 0, True])
def test_equal_timestamps_without_usable_comment_id_are_indeterminate(bad_id):
    comments = [_verdict("pm", H1, verdict="BLOCKED", n=1),
                _verdict("arch", H2, n=2)]
    comments[1]["created_at"] = comments[0]["created_at"]
    comments[1]["id"] = bad_id
    status = _status(comments, H2)
    assert status.refused
    assert "round order indeterminate" in status.describe()
    assert "comment ids" in status.describe()


def test_many_clear_heads_with_revisits_remain_one_round():
    comments = [_verdict("pm", head, n=i + 1)
                for i, head in enumerate((H1, H2, H1, H3, H2, H4))]
    status = _status(comments, H4)
    assert status.round_number == 1 and status.rounds[0].heads == (H1, H2, H1, H3, H2, H4)


def test_return_to_an_earlier_head_after_a_blocking_episode_spends_a_new_round_even_after_a_clear_one():
    # pm's sequence: a blocking verdict on F, a clear verdict on Z at the SAME timestamp, then a clear re-review
    # back on F. Ordered by comment id this is three rounds (F blocks, Z clears, F again), not one or two.
    f, z = "f" * 40, "0" * 40
    same = "2026-10-27T10:00:00Z"
    comments = [
        {**_verdict("pm", f, verdict="REQUEST_CHANGES", n=10), "created_at": same},
        {**_verdict("pm", z, n=11), "created_at": same},
        {**_verdict("pm", f, n=12), "created_at": "2026-10-27T10:05:00Z"},
    ]
    status = _status(comments, f)
    assert status.round_number == 3 and not status.refused
    assert f"round 3 started: returned to {f[:7]} after an earlier blocking episode" in status.describe()


def test_return_to_an_earlier_head_after_all_clear_history_is_the_same_round():
    comments = [_verdict("pm", H1, n=0), _verdict("pm", H2, n=1), _verdict("pm", H1, n=2)]
    status = _status(comments, H1)
    assert status.round_number == 1 and not status.refused
