"""
A verdict written against an old head must not gate a merge.

## The bug

GitHub's `dismiss_stale_reviews` is enabled on both repos. It dismisses a native
*review* when the head moves — and it is genuinely sufficient for anything that
reads `reviewDecision`.

`_pr_review_is_clear` does not read `reviewDecision`. It reads the body of the
Vanellus aggregation ISSUE COMMENT, and GitHub neither dismisses nor marks
issue comments when the head moves. So the native setting cannot reach this
path at all.

The consequence, before this change:

    1. Vanellus reviews PR #N at commit A and posts APPROVE.
    2. Someone pushes commit B. GitHub dismisses the native review — correctly.
       The aggregation comment is untouched and still says APPROVE.
    3. CI goes green on B. `_handle_ci_status` validates the check_suite event
       against the CURRENT head (it does this correctly), then calls
       `_pr_review_is_clear` — and passed it nothing.
    4. The old APPROVE reads as clear, `_gate_merge_readiness` files the merge
       checkpoint, and the operator is emailed "READY TO MERGE" for code no
       reviewer has seen.

The head SHA was in scope two dozen lines above the call and was discarded.

## The fix

Stamp `Reviewed commit: <sha>` into every aggregation, and compare it to the
PR head before treating the verdict as current.

## Why unstamped verdicts deliberately still pass

Every aggregation posted before this shipped carries no stamp. Treating
unstamped as stale would strand every already-reviewed open PR the moment this
lands — re-creating, at the exact moment we are trying to clear it, the backlog
this work exists to drain. Those verdicts are no less trustworthy than they were
yesterday. So `review_verdict_matches_head` fails OPEN on a missing stamp and
only ever fails CLOSED on a stamp that is present and different.

Run: pytest execution/daemons/apis/test_review_verdict_head_sha.py -v
"""

from __future__ import annotations

import pytest

import swarm_dispatch as sd

SHA_A = "a" * 40
SHA_B = "b" * 40


def _body(verdict: str = "**APPROVE**", sha: str | None = None) -> str:
    lines = [sd._VANELLUS_COMMENT_MARKER, "**Vanellus**"]
    if sha is not None:
        lines.append(f"Reviewed commit: {sha}")
    lines += ["", verdict, "", "Blocking: 0"]
    return "\n".join(lines)


class TestComposeReviewedCommitLine:
    def test_emits_the_stamp_for_a_real_sha(self) -> None:
        assert sd.compose_reviewed_commit_line(SHA_A) == f"Reviewed commit: {SHA_A}"

    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_emits_nothing_when_the_head_is_unknown(self, empty) -> None:
        # An unresolved head must produce NO stamp rather than a stamp
        # asserting the empty commit, which would read as a real mismatch.
        assert sd.compose_reviewed_commit_line(empty) == ""


class TestParseReviewedCommit:
    def test_extracts_the_sha(self) -> None:
        assert sd.parse_reviewed_commit(_body(sha=SHA_A)) == SHA_A

    def test_tolerates_backticks_and_padding(self) -> None:
        body = f"{sd._VANELLUS_COMMENT_MARKER}\n   Reviewed commit:  `{SHA_A}`  \n"
        assert sd.parse_reviewed_commit(body) == SHA_A

    def test_returns_none_when_unstamped(self) -> None:
        assert sd.parse_reviewed_commit(_body()) is None

    @pytest.mark.parametrize("body", ["", None])
    def test_survives_an_empty_body(self, body) -> None:
        assert sd.parse_reviewed_commit(body) is None


class TestReviewVerdictMatchesHead:
    def test_matching_sha_is_current(self) -> None:
        assert sd.review_verdict_matches_head(_body(sha=SHA_A), SHA_A) is True

    def test_different_sha_is_stale(self) -> None:
        # The whole point: this is the case dismiss_stale_reviews cannot see.
        assert sd.review_verdict_matches_head(_body(sha=SHA_A), SHA_B) is False

    def test_unstamped_verdict_fails_open(self) -> None:
        # Pre-existing aggregations carry no stamp; failing closed here would
        # strand every already-reviewed open PR on the day this ships.
        assert sd.review_verdict_matches_head(_body(), SHA_A) is True

    def test_unknown_head_fails_open(self) -> None:
        # An unread head is not evidence of staleness. Failing closed would
        # convert an unrelated API hiccup into a blocked merge.
        assert sd.review_verdict_matches_head(_body(sha=SHA_A), "") is True


class TestFallbackCommentCarriesTheStamp:
    def test_stamps_when_the_head_is_known(self) -> None:
        body = sd.compose_vanellus_fallback_comment("**APPROVE**", SHA_A)
        assert sd.parse_reviewed_commit(body) == SHA_A
        # The marker must survive: dedup and latest-selection both key on it.
        assert sd._VANELLUS_COMMENT_MARKER in body
        assert "**APPROVE**" in body

    def test_omits_the_stamp_when_the_head_is_unknown(self) -> None:
        body = sd.compose_vanellus_fallback_comment("**APPROVE**")
        assert sd.parse_reviewed_commit(body) is None
        assert sd._VANELLUS_COMMENT_MARKER in body

    def test_a_stamped_fallback_round_trips_as_current(self) -> None:
        body = sd.compose_vanellus_fallback_comment("**APPROVE**", SHA_A)
        assert sd.review_verdict_matches_head(body, SHA_A) is True
        assert sd.review_verdict_matches_head(body, SHA_B) is False


class TestTheOriginalScenario:
    """The end-to-end shape of the bug, at the predicate level."""

    def test_approve_at_a_does_not_clear_a_pr_now_at_b(self) -> None:
        approved_at_a = sd.compose_vanellus_fallback_comment("**APPROVE**", SHA_A)
        # Before the fix this read as clear and filed merge-readiness.
        assert sd.review_verdict_matches_head(approved_at_a, SHA_B) is False

    def test_re_review_at_b_clears_it_again(self) -> None:
        # And the unblock path works: a fresh aggregation against the new head
        # is current, so this gate never becomes a dead end.
        approved_at_b = sd.compose_vanellus_fallback_comment("**APPROVE**", SHA_B)
        assert sd.review_verdict_matches_head(approved_at_b, SHA_B) is True
