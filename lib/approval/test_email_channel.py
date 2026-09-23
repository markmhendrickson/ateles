"""Effect tests for lib.approval.email_channel.

Fully mock-based: `subprocess.run` and `shutil.which` are always patched, so no
real `gws` process runs and no network call is made. Every test asserts an
observable effect — the exact gws argv, the env gate, RE:-only filtering, the
explicit --to on replies, and fail-open behavior.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import patch

from lib.approval import email_channel as ec


def _ok(stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr="boom"):
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


_AUTH_PASS_FOR_OPERATOR = object()


def _google_pass_for_operator_domain() -> dict:
    import os as _os
    domain = _os.environ.get("OPERATOR_EMAIL", "").rpartition("@")[2].strip().lower()
    value = (f"mx.google.com; dkim=pass header.i=@{domain} header.s=s; "
             f"dmarc=pass (p=NONE) header.from={domain}")
    return {"id": "m", "payload": {"headers": [
        {"name": "Authentication-Results", "value": value}]}}


@contextmanager
def _mock_inbox(gws_json: Any = None, *, side_effect: Any = None,
                auth: Any = _AUTH_PASS_FOR_OPERATOR) -> Iterator[None]:
    """Patch the inbox path the way production reaches it.

    ``read_replies_with_status`` early-returns ``gws_cli_missing`` when
    ``_gws()`` is falsy, before any mocked ``gws_json``. Tests that only
    patch ``gws_json`` therefore pass on a machine with ``gws`` on PATH and
    fail in CI (ateles#1202). Always stub ``_gws`` present when exercising
    triage/+read behavior.

    ``auth``: by default the Authentication-Results metadata fetch is answered
    with a Google-stamped DMARC/DKIM pass for OPERATOR_EMAIL's domain, so tests
    about sender/body/transport keep testing exactly that. Pass ``auth=None``
    to leave the metadata call to the test's own ``side_effect``.
    """
    if side_effect is not None:
        inner = side_effect
    else:
        def inner(args, timeout=45, _v=gws_json):
            return _v

    def dispatch(args, timeout=45):
        if auth is _AUTH_PASS_FOR_OPERATOR and \
                list(args[:4]) == ["gmail", "users", "messages", "get"]:
            return _google_pass_for_operator_domain()
        return inner(args, timeout=timeout)

    with patch.object(ec, "_gws", return_value="/bin/gws"), \
         patch.object(ec, "gws_json", side_effect=dispatch):
        yield


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

        with _mock_inbox(side_effect=fake_gws_json):
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

        with _mock_inbox(side_effect=fake_gws_json):
            ec.read_replies(["TOK"], on_reply_message=lambda tok, mid: seen.append((tok, mid)))
        assert seen == [("TOK", "m1")]

    def test_triage_failure_is_fail_open_empty(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        with _mock_inbox(None):
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

        with _mock_inbox(side_effect=fake_gws_json):
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

        with _mock_inbox(side_effect=fake_gws_json):
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

        with _mock_inbox(side_effect=fake_gws_json):
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

        with _mock_inbox(side_effect=fake_gws_json):
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

        with _mock_inbox(side_effect=fake_gws_json):
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

        with _mock_inbox(side_effect=fake_gws_json):
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

        with _mock_inbox(side_effect=fake_gws_json):
            assert ec.read_replies(["TOK"]) == []


class TestReadRepliesWithStatus:
    """ateles#1178: statusful read distinguishes empty-ok from transport failure."""

    def test_read_replies_with_status_ok_empty_texts(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
        with _mock_inbox({"messages": []}):
            outcome = ec.read_replies_with_status(["TOK"])
        assert outcome.kind == "ok"
        assert outcome.texts == []

    def test_read_replies_with_status_transport_error_is_not_ok(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
        with _mock_inbox(None):
            outcome = ec.read_replies_with_status(["TOK"])
        assert outcome.kind == "transport_error"
        assert outcome.texts == []
        assert "operator@example.com" not in (outcome.detail or "")

    def test_read_replies_with_status_gws_cli_missing(self, monkeypatch):
        # Effect: missing gws is transport_error with a specific detail, not
        # conflated with an empty inbox (the CI false-green ateles#1202 class).
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
        with patch.object(ec, "_gws", return_value=None):
            outcome = ec.read_replies_with_status(["TOK"])
        assert outcome.kind == "transport_error"
        assert outcome.detail == "gws_cli_missing"
        assert outcome.texts == []

    def test_read_replies_with_status_disabled(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
        outcome = ec.read_replies_with_status(["TOK"])
        assert outcome.kind == "disabled"

    def test_read_replies_wrapper_preserves_fail_open_list(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
        with _mock_inbox(None):
            assert ec.read_replies(["TOK"]) == []


# ── Sender domain authentication + complete-read (PR #1202 security review) ──

import json as _json

_GOOGLE_DMARC_PASS = (
    "mx.google.com;\r\n"
    "       dkim=pass header.i=@example.com header.s=sel header.b=abc;\r\n"
    "       spf=pass (google.com: domain of op@example.com designates 192.0.2.1 "
    "as permitted sender) smtp.mailfrom=op@example.com;\r\n"
    "       dmarc=pass (p=NONE sp=QUARANTINE dis=NONE) header.from=example.com"
)


def _metadata(auth_values: list[str]) -> dict:
    headers = [{"name": "From", "value": "op@example.com"}]
    headers += [{"name": "Authentication-Results", "value": v} for v in auth_values]
    return {"id": "m", "payload": {"headers": headers}}


def _is_metadata_get(args) -> bool:
    return list(args[:4]) == ["gmail", "users", "messages", "get"]


def _metadata_id(args) -> str:
    return _json.loads(args[args.index("--params") + 1])["id"]


class TestSenderDomainAuthentication:
    """A matching From address is necessary but NOT sufficient.

    A From header is caller-supplied text. The verdict counts only when Gmail's
    own receiving server (authserv-id ``mx.google.com``) recorded that the
    operator's domain authorized the message — DMARC pass for that domain, or a
    DKIM pass whose signing domain is that domain. Anything else — header
    missing, failing, unparseable, or stamped by another server — is
    ``unknown``, and unknown is not the operator.
    """

    def _run(self, monkeypatch, auth, *, reads=None):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"messages": [{"id": "m1",
                                      "subject": "RE: x [APPROVE-TOK]",
                                      "from": "Op <op@example.com>"}]}
            if _is_metadata_get(args):
                return auth if isinstance(auth, dict) or auth is None \
                    else _metadata(auth)
            if reads is not None:
                reads.append(args)
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json, auth=None):
            return ec.read_replies_with_status(["TOK"])

    # ── yields its verdict ──

    def test_dmarc_pass_for_operator_domain_yields_verdict(self, monkeypatch):
        out = self._run(monkeypatch, [_GOOGLE_DMARC_PASS])
        assert out.kind == "ok"
        assert len(out.texts) == 1 and "APPROVE" in out.texts[0]

    def test_aligned_dkim_pass_without_dmarc_yields_verdict(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com; dkim=pass header.i=@example.com header.s=s header.b=x"])
        assert out.kind == "ok" and len(out.texts) == 1

    def test_dkim_header_d_aligned_yields_verdict(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com; dkim=pass header.d=example.com header.s=s"])
        assert out.kind == "ok" and len(out.texts) == 1

    # ── held: no verdict ──

    def test_missing_authentication_results_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [])
        assert out.texts == []

    def test_failing_dmarc_and_dkim_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com; dkim=fail header.i=@example.com; spf=softfail "
            "smtp.mailfrom=op@example.com; dmarc=fail (p=NONE) header.from=example.com"])
        assert out.texts == []

    def test_unparseable_authentication_results_is_held(self, monkeypatch):
        out = self._run(monkeypatch, ["%%% not an auth header"])
        assert out.texts == []

    def test_empty_authentication_results_value_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [""])
        assert out.texts == []

    def test_non_google_authserv_id_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.evil.example; dmarc=pass header.from=example.com; "
            "dkim=pass header.i=@example.com"])
        assert out.texts == []

    def test_google_lookalike_authserv_id_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com.evil.example; dmarc=pass header.from=example.com"])
        assert out.texts == []

    def test_pass_for_a_different_domain_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com; dkim=pass header.i=@evil.example; "
            "dmarc=pass header.from=evil.example"])
        assert out.texts == []

    def test_dkim_pass_for_parent_or_sub_domain_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com; dkim=pass header.i=@mail.example.com"])
        assert out.texts == []

    def test_pass_only_inside_a_comment_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com; dmarc=fail (dmarc=pass header.from=example.com) "
            "header.from=example.com"])
        assert out.texts == []

    def test_only_the_topmost_google_result_counts(self, monkeypatch):
        """Gmail prepends its own result; a lower header carrying the same
        authserv-id did not come from this receipt and must not override it."""
        out = self._run(monkeypatch, [
            "mx.google.com; dmarc=fail header.from=example.com",
            "mx.google.com; dmarc=pass header.from=example.com"])
        assert out.texts == []

    def test_body_is_never_fetched_for_an_unauthenticated_reply(self, monkeypatch):
        reads: list = []
        self._run(monkeypatch, [], reads=reads)
        assert reads == []

    def test_unauthenticated_reply_fires_sender_rejected(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        rejected = []

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            if _is_metadata_get(args):
                return _metadata([])
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json, auth=None):
            ec.read_replies_with_status(
                ["TOK"], on_sender_rejected=lambda: rejected.append(1))
        assert rejected == [1]

    def test_unreadable_metadata_response_is_not_ok(self, monkeypatch):
        """We could not read the authentication result at all: that is a read
        failure, never an 'ok, nothing authenticated' and never a pass."""
        assert self._run(monkeypatch, None).kind != "ok"
        assert self._run(monkeypatch, {"unexpected": True}).kind != "ok"

    def test_fail_open_wrapper_also_drops_unauthenticated(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            if _is_metadata_get(args):
                return _metadata([])
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json, auth=None):
            assert ec.read_replies(["TOK"]) == []


class TestPartialReadFailsClosed:
    """Any per-message or per-token fetch failure fails the WHOLE read.

    A verdict set assembled from the messages that happened to load is not the
    operator's verdict set: a SKIP in the message that failed would be silently
    replaced by an APPROVE in the one that loaded.
    """

    def _env(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

    def test_one_body_read_fails_other_succeeds_is_not_ok(self, monkeypatch):
        self._env(monkeypatch)

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"messages": [
                    {"id": "m1", "subject": "RE: x [APPROVE-TOK]", "from": "op@example.com"},
                    {"id": "m2", "subject": "RE: x [APPROVE-TOK]", "from": "op@example.com"},
                ]}
            if "+read" in args and args[args.index("--id") + 1] == "m2":
                return None
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json):
            out = ec.read_replies_with_status(["TOK"])
        assert out.kind == "transport_error"
        assert out.texts == []

    def test_one_metadata_fetch_fails_other_succeeds_is_not_ok(self, monkeypatch):
        self._env(monkeypatch)

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"messages": [
                    {"id": "m1", "subject": "RE: x [APPROVE-TOK]", "from": "op@example.com"},
                    {"id": "m2", "subject": "RE: x [APPROVE-TOK]", "from": "op@example.com"},
                ]}
            if _is_metadata_get(args):
                return None if _metadata_id(args) == "m2" else _metadata([_GOOGLE_DMARC_PASS])
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json, auth=None):
            out = ec.read_replies_with_status(["TOK"])
        assert out.kind == "transport_error"
        assert out.texts == []

    def test_one_token_triage_fails_other_succeeds_is_not_ok(self, monkeypatch):
        self._env(monkeypatch)

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                if any("TOK2" in a for a in args):
                    return None
                return {"messages": [
                    {"id": "m1", "subject": "RE: x [APPROVE-TOK1]", "from": "op@example.com"}]}
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json):
            out = ec.read_replies_with_status(["TOK1", "TOK2"])
        assert out.kind == "transport_error"
        assert out.texts == []
