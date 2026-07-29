"""Effect tests for lib.approval.tokens.

Every test asserts an observable result of the token / verdict logic — the
safety-critical bits the shared lib exists to make un-drift-able: session
scoping, SKIP-wins, and quoted-original stripping.
"""

from __future__ import annotations

from lib.approval.tokens import token_for, subject_marker, parse_verdict


class TestTokenFor:
    def test_stable_for_same_inputs(self):
        assert token_for("ent_abc", "2026-07-21") == token_for("ent_abc", "2026-07-21")

    def test_session_changes_token(self):
        # The whole point: a new session must get a different token so a stale
        # reply for the prior session cannot approve the next one.
        assert token_for("ent_abc", "2026-07-21") != token_for("ent_abc", "2026-07-28")

    def test_entity_changes_token(self):
        assert token_for("ent_abc", "2026-07-21") != token_for("ent_xyz", "2026-07-21")

    def test_legacy_entity_only_differs_from_sessioned(self):
        assert token_for("ent_abc") != token_for("ent_abc", "2026-07-21")

    def test_shape_is_8_upper_hex(self):
        t = token_for("ent_abc", "s")
        assert len(t) == 8 and t == t.upper() and all(c in "0123456789ABCDEF" for c in t)

    def test_subject_marker_wraps_token(self):
        assert subject_marker("1A2B3C4D") == "[APPROVE-1A2B3C4D]"


class TestParseVerdict:
    def test_bare_approve(self):
        assert parse_verdict("APPROVE", "TOK") is True

    def test_bare_skip(self):
        assert parse_verdict("SKIP", "TOK") is False

    def test_yes_is_approve(self):
        assert parse_verdict("yes", "TOK") is True

    def test_tokened_forms(self):
        assert parse_verdict("APPROVE TOK", "TOK") is True
        assert parse_verdict("APPROVE-TOK", "TOK") is True
        assert parse_verdict("SKIP TOK", "TOK") is False

    def test_no_verdict_returns_none(self):
        assert parse_verdict("thanks, looks good", "TOK") is None

    def test_skip_wins_over_approve_when_both_present(self):
        # Ambiguous reply must never act (never pay/publish by accident).
        assert parse_verdict("APPROVE\nSKIP", "TOK") is False
        assert parse_verdict("SKIP\nAPPROVE", "TOK") is False

    def test_quoted_original_is_ignored(self):
        # The operator typed nothing decisive; the quoted request below the
        # "On ... wrote:" line contains APPROVE/SKIP but must NOT count.
        reply = (
            "Hmm not sure yet\n"
            "On Mon, Jul 21, 2026 at 9:00 AM Ateles wrote:\n"
            "> APPROVE — to pay it\n"
            "> SKIP — to decline\n"
        )
        assert parse_verdict(reply, "TOK") is None

    def test_approve_only_in_quote_is_not_a_verdict(self):
        reply = (
            "let me think\n"
            "On Mon wrote:\n"
            "> Just hit Reply and send: APPROVE\n"
        )
        assert parse_verdict(reply, "TOK") is None

    def test_real_reply_above_quote_counts(self):
        reply = (
            "APPROVE\n"
            "On Mon, Jul 21 Ateles wrote:\n"
            "> [ATELES] Approve payment ... [APPROVE-TOK]\n"
            "> SKIP — to decline\n"
        )
        assert parse_verdict(reply, "TOK") is True

    def test_subject_line_alone_is_not_a_verdict(self):
        # A reply that only quotes the subject (which always has APPROVE-<token>)
        # and types nothing must be no-verdict.
        assert parse_verdict("RE: [ATELES] Approve release [APPROVE-TOK]", "TOK") is None

    def test_trailing_punctuation_tolerated(self):
        assert parse_verdict("APPROVE.", "TOK") is True
        assert parse_verdict("SKIP!", "TOK") is False

    def test_case_insensitive(self):
        assert parse_verdict("approve", "TOK") is True


class TestParseVerdictVerbPrefix:
    """The verb-with-trailing-content line form and token case-normalization —
    needed so a natural `approve <version>` reply (the release-approval UX) is
    recognized, and so a lowercase-bearing token still matches its APPROVE-<token>
    form. Both are additive; the bare/exact-token forms above still hold.
    """

    def test_approve_with_named_version(self):
        # The release UX: operator types `approve v0.20.0` on one line.
        assert parse_verdict("approve v0.20.0", "v0.20.0") is True
        assert parse_verdict("approve 0.20.0", "v0.20.0") is True

    def test_skip_with_named_version_blocks(self):
        assert parse_verdict("skip v0.20.0", "v0.20.0") is False

    def test_skip_with_trailing_content_still_wins(self):
        # A skip line with trailing words is still decisive over an approve line.
        assert parse_verdict("approve v0.20.0\nskip this one", "v0.20.0") is False

    def test_lowercase_bearing_token_matches(self):
        # Token case-normalization: text is uppercased internally, so the token
        # must be too — a lowercase-bearing token used to silently never match.
        assert parse_verdict("APPROVE-abc123", "abc123") is True
        assert parse_verdict("approve abc123", "abc123") is True

    def test_verb_prefix_requires_leading_verb_not_mid_sentence(self):
        # "i approve this" does NOT start with the verb → not a verdict, so a
        # chatty sentence that merely contains the word never publishes.
        assert parse_verdict("i approve this release", "TOK") is None

    def test_quoted_named_version_still_ignored(self):
        # The verb-prefix form must NOT resurrect a quoted instruction as a verdict.
        reply = (
            "not yet\n"
            "On Mon Ateles wrote:\n"
            "> approve v0.20.0 to publish\n"
        )
        assert parse_verdict(reply, "v0.20.0") is None

    def test_bare_forms_unchanged(self):
        # Regression guard: the pre-existing exact forms still behave.
        assert parse_verdict("APPROVE", "TOK") is True
        assert parse_verdict("SKIP", "TOK") is False
        assert parse_verdict("APPROVE TOK", "TOK") is True
        assert parse_verdict("thanks", "TOK") is None
