"""Tests for review_rounds: the three-round review cap (operator ruling 2026-10-27).

Synthetic comment sets stand in for a PR's thread. A round is a distinct head
that received a lens verdict, read from the swarm's `<!-- review:<lens>
commit=<sha> -->` markers.
"""

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

H1, H2, H3, H4 = ("1" * 40, "2" * 40, "3" * 40, "4" * 40)
AGENTS = {"pm": "Pavo", "arch": "Waxwing", "qa": "Phoenicurus", "security": "Falco", "ux": "Accipiter"}


def _verdict(lens: str, head: str, *, verdict: str = "SIGNED_OFF", n: int = 0) -> dict:
    marker = swarm_dispatch.compose_lens_review_marker(lens, head)
    body = f"{marker}\n**\U0001f916 {AGENTS[lens]} — Ateles swarm, {lens} review**\n**{verdict}**\n"
    return {"id": n, "body": body, "created_at": f"2026-10-27T10:{n:02d}:00Z"}


def _records(comments: list[dict]):
    return swarm_dispatch.lens_records(comments)


def _round(head: str, lenses=("pm", "arch"), *, start: int = 0) -> list[dict]:
    return [_verdict(lens, head, n=start + i) for i, lens in enumerate(lenses)]


def test_the_cap_is_three():
    # Pins the operator's number. A change to it is a ruling, not a tweak.
    assert rr.ROUND_CAP == 3
    assert rr.CAP_REACHED == "round cap reached: split or cut scope"


class TestCounting:
    def test_a_first_review_is_round_one(self):
        status = rr.round_status(_records(_round(H1)), H1)
        assert status.round_number == 1 and not status.capped and not status.refused

    def test_several_lenses_on_one_head_are_one_round(self):
        comments = _round(H1, ("pm", "arch", "qa", "security", "ux"))
        assert rr.round_status(_records(comments), H1).round_number == 1

    def test_the_third_head_is_the_last_allowed_round(self):
        comments = _round(H1) + _round(H2, start=10) + _round(H3, start=20)
        status = rr.round_status(_records(comments), H3)
        assert status.round_number == 3
        assert not status.capped and not status.refused

    def test_the_fourth_head_is_past_the_cap(self):
        comments = _round(H1) + _round(H2, start=10) + _round(H3, start=20) + _round(H4, start=30)
        status = rr.round_status(_records(comments), H4)
        assert status.round_number == 4
        assert status.capped and status.refused
        assert rr.CAP_REACHED in status.describe()

    def test_a_fourth_head_with_no_verdict_yet_is_already_capped(self):
        # Three heads reviewed and the PR is still open: the next round must not
        # run, so the tool says so before the fourth head has a verdict at all.
        comments = _round(H1) + _round(H2, start=10) + _round(H3, start=20)
        status = rr.round_status(_records(comments), H4)
        assert status.capped and status.refused and not status.current_has_verdict

    def test_a_blocking_round_counts_like_any_other(self):
        comments = [_verdict("security", H1, verdict="REQUEST_CHANGES")] + _round(H2, start=10) + _round(H3, start=20)
        assert rr.round_status(_records(comments), H4).capped

    def test_an_unknown_lens_marker_is_not_a_round(self):
        comments = _round(H1) + _round(H2, start=10)
        comments.append({"id": 99, "body": f"<!-- review:invented commit={H3} -->\n**SIGNED_OFF**\n"})
        assert rr.round_status(_records(comments), H4).round_number == 3  # two prior heads

    @pytest.mark.parametrize("earlier", [4, 5, 9])
    def test_every_head_past_the_cap_stays_capped(self, earlier):
        # Not just the fourth round: a fifth or ninth must refuse as well.
        heads = [f"{i:x}" * 40 for i in range(1, earlier + 1)]
        comments = [c for i, h in enumerate(heads) for c in _round(h, start=i * 10)]
        status = rr.round_status(_records(comments), "f" * 40)
        assert len(status.prior_heads) == earlier
        assert status.capped and status.refused

    def test_a_comment_with_no_marker_is_not_a_round(self):
        comments = _round(H1) + _round(H2, start=10) + [{"id": 98, "body": "Review incomplete: waiting on qa"}]
        assert rr.round_status(_records(comments), H3).round_number == 3 and not rr.round_status(_records(comments), H3).capped

    def test_a_retired_verdict_still_counts_for_its_own_head(self):
        retired = swarm_dispatch.compose_superseded_verdict(_verdict("pm", H1)["body"], H2)
        comments = [{"id": 1, "body": retired}] + _round(H2, start=10) + _round(H3, start=20)
        # The superseded banner keeps the original marker, so H1 is a round.
        assert rr.round_status(_records(comments), H4).capped

    def test_no_comments_is_round_one(self):
        assert rr.round_status({}, H1).round_number == 1

    def test_head_case_does_not_split_a_round(self):
        # Letters in the SHA, so upper-casing it actually changes the string.
        ha, hb, hc = "abcdef" * 6 + "abcd", "bcdefa" * 6 + "bcde", "cdefab" * 6 + "cdef"
        comments = _round(ha) + _round(hb, start=10) + _round(hc, start=20)
        assert ha != ha.upper()
        status = rr.round_status(_records(comments), hc.upper())
        assert status.round_number == 3 and not status.capped and status.current_has_verdict


class TestOverride:
    def _capped(self, reason: str = ""):
        comments = _round(H1) + _round(H2, start=10) + _round(H3, start=20)
        return rr.round_status(_records(comments), H4, override_reason=reason)

    def test_an_override_lifts_the_refusal_and_is_recorded(self):
        status = self._capped("operator granted a fourth round for the migration")
        assert status.capped and not status.refused
        note = status.approval_note()
        assert "Round cap overrun allowed" in note and "operator granted a fourth round" in note
        text = status.describe()
        assert text.startswith("round cap overrun allowed") and rr.CAP_REACHED in text

    def test_an_override_given_but_not_needed_says_so(self):
        status = rr.round_status(_records(_round(H1)), H1, override_reason="not actually needed here")
        assert "not needed" in status.describe() and "not recorded" in status.describe()

    def test_no_override_means_no_note(self):
        assert self._capped().approval_note() == ""

    def test_an_override_on_a_pr_under_the_cap_records_nothing(self):
        status = rr.round_status(_records(_round(H1)), H1, override_reason="not actually needed here")
        assert not status.capped and status.approval_note() == ""

    @pytest.mark.parametrize("raw", ["", "   ", "short", "\n\t "])
    def test_a_missing_or_trivial_reason_is_refused(self, raw):
        with pytest.raises(rr.OverrunReasonError):
            rr.clean_reason(raw)

    def test_a_reason_cannot_open_a_marker_or_run_on(self):
        cleaned = rr.clean_reason("<!-- review:pm commit=" + "a" * 40 + " -->\nlong\n" + "x" * 1000)
        assert "<" not in cleaned and ">" not in cleaned and "\n" not in cleaned

    def test_a_reason_cannot_mention_an_account_or_build_a_link(self):
        cleaned = rr.clean_reason("ping @someone and see [this](https://example.invalid/x) `now`")
        assert "@" not in cleaned and "[" not in cleaned and "]" not in cleaned and "`" not in cleaned

    def test_a_long_reason_is_cut_to_the_limit(self):
        cleaned = rr.clean_reason("x" * 1000)
        assert len(cleaned) == rr.MAX_REASON_CHARS

    def test_control_and_bidi_characters_are_not_kept(self):
        cleaned = rr.clean_reason("granted\x1b[31m by the \u202eoperator\u202c for this PR")
        assert all(ord(ch) >= 32 and ch not in "\u202e\u202c\x1b" for ch in cleaned)
        assert "granted" in cleaned and "operator" in cleaned
