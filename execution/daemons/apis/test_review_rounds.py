"""Genuine review rounds, ordered by first lens-verdict creation time."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parents[2])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import review_rounds as rr  # noqa: E402
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
    assert _status(comments, H2).round_number == 2


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


def test_first_verdict_timestamp_orders_heads_even_when_lens_buckets_reverse_them():
    comments = [_verdict("arch", H2, n=2), _verdict("pm", H1, verdict="BLOCKED", n=0)]
    status = _status(comments, H2)
    assert status.round_number == 2 and status.rounds[0].heads == (H1,)


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
    assert status.rounds[0].heads == (H1,)


def test_reset_to_earlier_clear_head_spends_after_blocking_history():
    comments = [_verdict("pm", head, verdict="BLOCKED" if head != H2 else "SIGNED_OFF", n=i)
                for i, head in enumerate((H1, H2, H3, H4))]
    status = _status(comments, H2)
    assert status.round_number == 4 and status.refused
    assert status.rounds[-1].heads == (H2,)
    assert all(head[:7] in status.describe() for head in (H1, H2, H3, H4))
    assert "whole verdict history" in status.describe()


def test_reset_to_earlier_head_after_all_clear_history_does_not_spend():
    comments = [_verdict("pm", head, n=i) for i, head in enumerate((H1, H2, H3, H4))]
    status = _status(comments, H2)
    assert status.round_number == 1 and not status.refused
    assert status.rounds[0].heads == (H1, H2, H3, H4)


def test_rerun_on_latest_reviewed_head_spends_no_further_round():
    comments = [_verdict("pm", head, verdict="BLOCKED", n=i) for i, head in enumerate((H1, H2, H3, H4))]
    assert _status(comments, H4).round_number == 4
    assert _status(comments + [_verdict("arch", H4, n=5)], H4).round_number == 4
