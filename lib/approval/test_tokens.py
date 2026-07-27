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
