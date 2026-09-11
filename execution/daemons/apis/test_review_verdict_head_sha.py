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

Stamp ``<!-- vanellus-aggregation commit=<full40hex> -->`` into every
aggregation (ateles#507 Eng lock). ``review_verdict_matches_head`` reads that
HTML attribute only — prose ``Reviewed commit:`` is optional human redundancy
and is never the gate input.

## Why unstamped verdicts deliberately still pass

Every aggregation posted before this shipped carries a bare marker with no
``commit=``. Treating unstamped as stale would strand every already-reviewed
open PR the moment this lands — re-creating, at the exact moment we are trying
to clear it, the backlog this work exists to drain. Those verdicts are no less
trustworthy than they were yesterday. So `review_verdict_matches_head` fails
OPEN on a missing ``commit=`` and only ever fails CLOSED on a stamp that is
present and different.

Run: pytest execution/daemons/apis/test_review_verdict_head_sha.py -v
"""

from __future__ import annotations

import pytest

import swarm_dispatch as sd

SHA_A = "a" * 40
SHA_B = "b" * 40


def _body(
    verdict: str = "**APPROVE**",
    sha: str | None = None,
    *,
    prose_sha: str | None = None,
) -> str:
    """Build an aggregation body.

    ``sha`` stamps the authoritative HTML marker. ``prose_sha`` adds an optional
    human ``Reviewed commit:`` line the gate must ignore.
    """
    marker = (
        sd.compose_vanellus_aggregation_marker(sha)
        if sha is not None
        else sd._VANELLUS_COMMENT_MARKER
    )
    lines = [marker, "**Vanellus**"]
    if prose_sha is not None:
        lines.append(f"Reviewed commit: {prose_sha}")
    lines += ["", verdict, "", "Blocking: 0"]
    return "\n".join(lines)


class TestComposeVanellusAggregationMarker:
    def test_stamps_commit_when_the_head_is_known(self) -> None:
        assert (
            sd.compose_vanellus_aggregation_marker(SHA_A)
            == f"<!-- vanellus-aggregation commit={SHA_A} -->"
        )

    def test_normalizes_hex_case(self) -> None:
        mixed = ("A" * 20) + ("b" * 20)
        assert (
            sd.compose_vanellus_aggregation_marker(mixed)
            == f"<!-- vanellus-aggregation commit={mixed.lower()} -->"
        )

    @pytest.mark.parametrize("empty", ["", "   ", None, "deadbeef", "g" * 40])
    def test_emits_bare_marker_when_the_head_is_unusable(self, empty) -> None:
        # Unknown / non-40-hex heads must produce the bare legacy marker —
        # never a stamp asserting an empty or truncated commit.
        assert sd.compose_vanellus_aggregation_marker(empty) == sd._VANELLUS_COMMENT_MARKER


class TestComposeReviewedCommitLine:
    def test_emits_optional_human_line(self) -> None:
        assert sd.compose_reviewed_commit_line(SHA_A) == f"Reviewed commit: {SHA_A}"

    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_emits_nothing_when_the_head_is_unknown(self, empty) -> None:
        assert sd.compose_reviewed_commit_line(empty) == ""


class TestParseAggregationCommit:
    def test_extracts_commit_from_html_marker(self) -> None:
        assert sd.parse_aggregation_commit(_body(sha=SHA_A)) == SHA_A

    def test_returns_none_for_bare_legacy_marker(self) -> None:
        assert sd.parse_aggregation_commit(_body()) is None

    def test_ignores_prose_reviewed_commit_line(self) -> None:
        # Prose alone must NEVER be authoritative — even when it names a SHA.
        body = _body(prose_sha=SHA_A)
        assert "Reviewed commit:" in body
        assert sd._VANELLUS_COMMENT_MARKER in body or sd.has_vanellus_aggregation_marker(
            body
        )
        assert sd.parse_aggregation_commit(body) is None

    @pytest.mark.parametrize("body", ["", None])
    def test_survives_an_empty_body(self, body) -> None:
        assert sd.parse_aggregation_commit(body) is None

    def test_alias_matches_html_parser(self) -> None:
        # Back-compat alias must also ignore prose (same contract).
        body = _body(sha=SHA_A, prose_sha=SHA_B)
        assert sd.parse_reviewed_commit(body) == SHA_A
        assert sd.parse_reviewed_commit(_body(prose_sha=SHA_B)) is None


class TestHasVanellusAggregationMarker:
    def test_detects_bare_and_stamped(self) -> None:
        assert sd.has_vanellus_aggregation_marker(_body()) is True
        assert sd.has_vanellus_aggregation_marker(_body(sha=SHA_A)) is True

    def test_bare_constant_is_not_a_substring_of_stamped(self) -> None:
        # Guard: naive ``_VANELLUS_COMMENT_MARKER in body`` would miss stamped
        # markers because ``commit=`` sits before ``-->``.
        stamped = sd.compose_vanellus_aggregation_marker(SHA_A)
        assert sd._VANELLUS_COMMENT_MARKER not in stamped
        assert sd.has_vanellus_aggregation_marker(stamped) is True


class TestReviewVerdictMatchesHead:
    def test_matching_sha_is_current(self) -> None:
        assert sd.review_verdict_matches_head(_body(sha=SHA_A), SHA_A) is True

    def test_different_sha_is_stale(self) -> None:
        # The whole point: this is the case dismiss_stale_reviews cannot see.
        assert sd.review_verdict_matches_head(_body(sha=SHA_A), SHA_B) is False

    def test_unstamped_verdict_fails_open(self) -> None:
        # Pre-existing aggregations carry no commit=; failing closed here would
        # strand every already-reviewed open PR on the day this ships.
        assert sd.review_verdict_matches_head(_body(), SHA_A) is True

    def test_prose_only_with_bare_marker_fails_open(self) -> None:
        # Prose is non-authoritative: body with ONLY ``Reviewed commit:`` and a
        # legacy bare marker must fail-open (same as unstamped).
        body = _body(prose_sha=SHA_A)
        assert sd.parse_aggregation_commit(body) is None
        assert sd.review_verdict_matches_head(body, SHA_B) is True

    def test_prose_cannot_override_html_marker(self) -> None:
        # HTML says A; prose lying about B must not clear head B.
        body = _body(sha=SHA_A, prose_sha=SHA_B)
        assert sd.review_verdict_matches_head(body, SHA_B) is False
        assert sd.review_verdict_matches_head(body, SHA_A) is True

    def test_unknown_head_fails_open(self) -> None:
        # An unread head is not evidence of staleness. Failing closed would
        # convert an unrelated API hiccup into a blocked merge.
        assert sd.review_verdict_matches_head(_body(sha=SHA_A), "") is True


class TestFallbackCommentCarriesTheStamp:
    def test_stamps_html_marker_when_the_head_is_known(self) -> None:
        body = sd.compose_vanellus_fallback_comment("**APPROVE**", SHA_A)
        assert sd.parse_aggregation_commit(body) == SHA_A
        assert body.startswith(f"<!-- vanellus-aggregation commit={SHA_A} -->")
        assert sd.has_vanellus_aggregation_marker(body)
        assert "**APPROVE**" in body
        # Optional human line may be present; gate must not depend on it.
        assert "Reviewed commit:" in body

    def test_omits_commit_when_the_head_is_unknown(self) -> None:
        body = sd.compose_vanellus_fallback_comment("**APPROVE**")
        assert sd.parse_aggregation_commit(body) is None
        assert body.startswith(sd._VANELLUS_COMMENT_MARKER)
        assert sd.has_vanellus_aggregation_marker(body)

    def test_a_stamped_fallback_round_trips_as_current(self) -> None:
        body = sd.compose_vanellus_fallback_comment("**APPROVE**", SHA_A)
        assert sd.review_verdict_matches_head(body, SHA_A) is True
        assert sd.review_verdict_matches_head(body, SHA_B) is False


class TestTheOriginalScenario:
    """The end-to-end shape of the bug, at the predicate level."""

    def test_approve_at_a_does_not_clear_a_pr_now_at_b(self) -> None:
        approved_at_a = sd.compose_vanellus_fallback_comment("**APPROVE**", SHA_A)
        # Before the fix this read as clear and filed merge-readiness.
        assert sd.parse_aggregation_commit(approved_at_a) == SHA_A
        assert sd.review_verdict_matches_head(approved_at_a, SHA_B) is False

    def test_re_review_at_b_clears_it_again(self) -> None:
        # And the unblock path works: a fresh aggregation against the new head
        # is current, so this gate never becomes a dead end.
        approved_at_b = sd.compose_vanellus_fallback_comment("**APPROVE**", SHA_B)
        assert sd.parse_aggregation_commit(approved_at_b) == SHA_B
        assert sd.review_verdict_matches_head(approved_at_b, SHA_B) is True
