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
        triage = {"messages": [
            {"id": "m1", "subject": "RE: [ATELES] Approve [APPROVE-TOK]"},
            {"id": "m2", "subject": "[ATELES] Approve [APPROVE-TOK]"},  # our own outbound
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
        seen = []

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]"}]}
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

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]"}]}
            return {"body_text": "approve v0.20.0", "body_html": "<p>ignored</p>"}

        with patch.object(ec, "gws_json", side_effect=fake_gws_json):
            texts = ec.read_replies(["TOK"])
        assert "approve v0.20.0" in texts[0]
        assert "ignored" not in texts[0]

    def test_falls_back_to_body_html_when_no_plaintext(self, monkeypatch):
        # An HTML-only reply must not read as an empty body and silently drop the
        # approval — the ateles#286 live-release failure mode. HTML passes raw.
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]"}]}
            return {"body_html": "<p>approve v0.20.0</p>"}

        with patch.object(ec, "gws_json", side_effect=fake_gws_json):
            texts = ec.read_replies(["TOK"])
        assert "approve v0.20.0" in texts[0]  # tags tolerated; body is non-empty


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
