"""Effect tests for lib.approval.email_channel.

Fully mock-based: `subprocess.run` and `shutil.which` are always patched, so no
real `gws` process runs and no network call is made. Every test asserts an
observable effect — the exact gws argv, the env gate, RE:-only filtering, the
explicit --to on replies, and fail-open behavior.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from lib.approval import email_channel as ec


def _ok(stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr="boom"):
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


class TestGate:
    def test_email_enabled_true_only_when_flag_1(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        assert ec.email_enabled() is True
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
        assert ec.email_enabled() is False
        monkeypatch.delenv("ATELES_NOTIFY_EMAIL", raising=False)
        assert ec.email_enabled() is False


class TestSendRequest:
    def test_disabled_noops(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
        with patch.object(ec.subprocess, "run") as run:
            assert ec.send_request("s", "b") is False
            run.assert_not_called()

    def test_missing_operator_email_returns_false(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.delenv("OPERATOR_EMAIL", raising=False)
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run") as run:
            assert ec.send_request("s", "b") is False
            run.assert_not_called()

    def test_missing_gws_returns_false(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value=None), \
             patch.object(ec.subprocess, "run") as run:
            assert ec.send_request("s", "b") is False
            run.assert_not_called()

    def test_sends_with_expected_argv(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_ok()) as run:
            assert ec.send_request("Subj [APPROVE-TOK]", "body text") is True
        argv = run.call_args.args[0]
        assert argv[:3] == ["/bin/gws", "gmail", "+send"]
        assert "--to" in argv and argv[argv.index("--to") + 1] == "op@example.com"
        assert "--subject" in argv and argv[argv.index("--subject") + 1] == "Subj [APPROVE-TOK]"
        assert "--from" in argv and argv[argv.index("--from") + 1] == "swarm@example.com"

    def test_explicit_to_overrides_operator(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_ok()) as run:
            ec.send_request("s", "b", to="other@example.com")
        argv = run.call_args.args[0]
        assert argv[argv.index("--to") + 1] == "other@example.com"

    def test_nonzero_exit_is_fail_open_false(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_fail()):
            assert ec.send_request("s", "b") is False

    def test_exception_is_fail_open_false(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", side_effect=OSError("nope")):
            assert ec.send_request("s", "b") is False


class TestReadReplies:
    def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
        assert ec.read_replies(["TOK"]) == []

    def test_empty_tokens_returns_empty(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        assert ec.read_replies([]) == []

    def test_only_re_subjects_are_read(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        triage = {"messages": [
            {"id": "m1", "subject": "RE: [ATELES] Approve [APPROVE-TOK]",
             "from": "op@example.com"},
            {"id": "m2", "subject": "[ATELES] Approve [APPROVE-TOK]",
             "from": "op@example.com"},  # our own outbound
        ]}

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return triage
            if "+read" in args:
                return {"body_text": "APPROVE"}
            return None

        with patch.object(ec, "gws_json", side_effect=fake_gws_json):
            texts = ec.read_replies(["TOK"])
        # Only the RE: message's body is read; the outbound one is skipped.
        assert len(texts) == 1
        assert "APPROVE" in texts[0] and texts[0].startswith("RE:")

    def test_on_reply_message_callback_fires_with_token_and_id(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        seen = []

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            return {"body_text": "SKIP"}

        with patch.object(ec, "gws_json", side_effect=fake_gws_json):
            ec.read_replies(["TOK"], on_reply_message=lambda tok, mid: seen.append((tok, mid)))
        assert seen == [("TOK", "m1")]

    def test_triage_failure_is_fail_open_empty(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        with patch.object(ec, "gws_json", return_value=None):
            assert ec.read_replies(["TOK"]) == []

    def test_prefers_body_text_over_html(self, monkeypatch):
        # The operator's verdict + quoted token live in the plaintext part; the
        # HTML part must never win when plaintext is present (ateles#286).
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            return {"body_text": "approve v0.20.0", "body_html": "<p>ignored</p>"}

        with patch.object(ec, "gws_json", side_effect=fake_gws_json):
            texts = ec.read_replies(["TOK"])
        assert "approve v0.20.0" in texts[0]
        assert "ignored" not in texts[0]

    def test_falls_back_to_body_html_when_no_plaintext(self, monkeypatch):
        # An HTML-only reply must not read as an empty body and silently drop the
        # approval — the ateles#286 live-release failure mode. Tags are stripped
        # so the verdict actually PARSES, not merely so the body is non-empty
        # (Loxia #298: `<p>approve` would leave parse_verdict returning None).
        from lib.approval import parse_verdict
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            return {"body_html": "<div dir=\"ltr\">approve v0.20.0</div>"}

        with patch.object(ec, "gws_json", side_effect=fake_gws_json):
            texts = ec.read_replies(["TOK"])
        assert "approve v0.20.0" in texts[0]
        # The real end-to-end guarantee: this HTML-only reply registers as APPROVE.
        assert parse_verdict(texts[0], "v0.20.0") is True


class TestReplyInThread:
    def test_passes_explicit_to_operator(self, monkeypatch, tmp_path):
        # The known-quirk guard: +reply must carry --to OPERATOR_EMAIL explicitly.
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_ok()) as run:
            assert ec.reply_in_thread("m1", "done", cwd=str(tmp_path)) is True
        argv = run.call_args.args[0]
        assert argv[:3] == ["/bin/gws", "gmail", "+reply"]
        assert "--message-id" in argv and argv[argv.index("--message-id") + 1] == "m1"
        assert "--to" in argv and argv[argv.index("--to") + 1] == "op@example.com"

    def test_disabled_noops(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
        with patch.object(ec.subprocess, "run") as run:
            assert ec.reply_in_thread("m1", "b") is False
            run.assert_not_called()

    def test_missing_message_id_returns_false(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"):
            assert ec.reply_in_thread("", "b") is False

    def test_failure_is_fail_open_false(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_fail()):
            assert ec.reply_in_thread("m1", "b", cwd=str(tmp_path)) is False


class TestGwsJson:
    def test_strips_banner_before_json(self, monkeypatch):
        out = "keyring banner line\nWARNING: something\n{\"ok\": true}"
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_ok(out)):
            assert ec.gws_json(["gmail", "+triage"]) == {"ok": True}

    def test_missing_gws_returns_none(self):
        with patch.object(ec.shutil, "which", return_value=None):
            assert ec.gws_json(["gmail", "+triage"]) is None

    def test_nonzero_exit_returns_none(self):
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_fail()):
            assert ec.gws_json(["gmail", "+triage"]) is None


class TestStripHtml:
    def test_verb_leads_line_after_strip(self):
        # The core requirement: the verb must lead its line so parse_verdict sees it.
        assert ec._strip_html("<p>approve v0.20.0</p>") == "approve v0.20.0"
        assert ec._strip_html('<div dir="ltr">approve v0.20.0</div>') == "approve v0.20.0"

    def test_block_boundaries_become_newlines(self):
        out = ec._strip_html("<p>approve v0.20.0</p><p>thanks</p>")
        assert out.splitlines()[0] == "approve v0.20.0"

    def test_scripts_and_styles_dropped(self):
        html = "<style>.x{}</style><p>approve v0.20.0</p><script>x()</script>"
        assert ec._strip_html(html).strip() == "approve v0.20.0"

    def test_entities_unescaped(self):
        assert "&" in ec._strip_html("<p>a &amp; b</p>")

    def test_empty_and_none_safe(self):
        assert ec._strip_html("") == ""


class TestSenderVerification:
    """A valid token must NOT be sufficient to approve.

    The Gmail query `read_replies` issues is a full-text mailbox search for the
    token (`newer_than:3d <token>`), so ANY message carrying the token string
    matches — including one from a third party. Before this guard, the only
    filter was `subject.startswith("RE:")`, which anyone replying satisfies.
    Possession of the token was therefore sufficient to approve: authentication
    with no authorization.

    The token is not a secret that stays secret. It rides in the SUBJECT LINE of
    an email, so it is present in every forward, every auto-reply, every "on
    vacation" bounce, and any thread the operator CCs someone on. Treating it as
    proof of identity conflates "saw the request" with "is the operator".
    """

    @staticmethod
    def _triage(from_addr: str) -> dict:
        return {"messages": [
            {"id": "m1", "subject": "RE: [ATELES] Approve [APPROVE-TOK]",
             "from": from_addr},
        ]}

    def _run(self, monkeypatch, from_addr: str, operator: str = "op@example.com"):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", operator)

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return self._triage(from_addr)
            return {"body_text": "APPROVE"}

        with patch.object(ec, "gws_json", side_effect=fake_gws_json):
            return ec.read_replies(["TOK"])

    # ── The core requirement ────────────────────────────────────────────────

    def test_reply_from_a_stranger_with_a_valid_token_does_not_approve(
            self, monkeypatch):
        """THE deliberate deliverable: valid token + wrong sender ⇒ no approval.

        This is the attack. An attacker who learns the token — by being CC'd, by
        receiving a forward, by compromising any mailbox the thread touched —
        replies "APPROVE". Before the fix this returned the body and the caller
        approved a payment or a release.
        """
        assert self._run(monkeypatch, "attacker@evil.example") == []

    def test_reply_from_the_operator_still_approves(self, monkeypatch):
        """The guard must not break the legitimate path — otherwise it is just
        an outage, and the next person to debug it will remove it."""
        texts = self._run(monkeypatch, "op@example.com")
        assert len(texts) == 1
        assert "APPROVE" in texts[0]

    # ── Fail closed on absent / malformed identity ──────────────────────────

    def test_absent_sender_does_not_approve(self, monkeypatch):
        """An unverifiable sender must not approve. `gws` omitting the field, or
        a triage backend that never populated it, must take the RESTRICTIVE
        branch — the alternative is that a tooling change silently reopens the
        hole."""
        assert self._run(monkeypatch, "") == []

    def test_malformed_sender_does_not_approve(self, monkeypatch):
        assert self._run(monkeypatch, "not-an-address") == []

    def test_unset_operator_email_does_not_approve(self, monkeypatch):
        """No configured principal ⇒ nothing can be authorized. Fail closed on
        the field that carries the safety meaning: if we cannot say who the
        operator IS, we cannot say a reply came from them."""
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.delenv("OPERATOR_EMAIL", raising=False)

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return self._triage("op@example.com")
            return {"body_text": "APPROVE"}

        with patch.object(ec, "gws_json", side_effect=fake_gws_json):
            assert ec.read_replies(["TOK"]) == []

    # ── Address-form handling (must not become a bypass) ────────────────────

    def test_display_name_form_from_the_operator_approves(self, monkeypatch):
        """Real Gmail returns `From: Mark H <op@example.com>`. Parsing must
        extract the address, or the guard rejects every genuine reply and gets
        reverted."""
        assert len(self._run(monkeypatch, "Mark H <op@example.com>")) == 1

    def test_case_and_whitespace_differences_still_approve(self, monkeypatch):
        assert len(self._run(monkeypatch, "  <OP@Example.COM>  ")) == 1

    def test_lookalike_domain_does_not_approve(self, monkeypatch):
        """Substring matching would accept this. The comparison must be on the
        parsed address, whole and exact."""
        assert self._run(monkeypatch, "op@example.com.evil.example") == []

    def test_operator_address_in_display_name_does_not_approve(
            self, monkeypatch):
        """The classic spoof: put the operator's address in the DISPLAY NAME so
        a naive `operator in from_field` check passes while the real envelope
        address is the attacker's."""
        assert self._run(
            monkeypatch, "op@example.com <attacker@evil.example>") == []

    def test_callback_does_not_fire_for_an_unverified_sender(self, monkeypatch):
        """The callback persists which message to reply to. Firing it for a
        stranger's message would point the in-thread confirmation at the
        attacker — leaking that the request exists and what it is for."""
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        seen = []

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return self._triage("attacker@evil.example")
            return {"body_text": "APPROVE"}

        with patch.object(ec, "gws_json", side_effect=fake_gws_json):
            ec.read_replies(["TOK"],
                            on_reply_message=lambda t, m: seen.append((t, m)))
        assert seen == []

    def test_body_is_never_fetched_for_an_unverified_sender(self, monkeypatch):
        """Rejection happens BEFORE the `+read`. A sender we will not trust
        should not cost us a round trip, and the body should never enter the
        process."""
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        reads = []

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return self._triage("attacker@evil.example")
            reads.append(args)
            return {"body_text": "APPROVE"}

        with patch.object(ec, "gws_json", side_effect=fake_gws_json):
            ec.read_replies(["TOK"])
        assert reads == []

    def test_malformed_operator_email_authorizes_nobody(self, monkeypatch):
        """A MISCONFIGURED principal must not become a usable one.

        `parseaddr` is lenient: it returns a bare word like "not-an-address"
        unchanged rather than rejecting it. Without structural validation, an
        OPERATOR_EMAIL typo would become a principal that a sender could match
        exactly — a configuration error silently turning into an approval path.
        This pins the structural check; equality alone does not cover it,
        because here the two sides ARE equal.
        """
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "not-an-address")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return self._triage("not-an-address")
            return {"body_text": "APPROVE"}

        with patch.object(ec, "gws_json", side_effect=fake_gws_json):
            assert ec.read_replies(["TOK"]) == []
