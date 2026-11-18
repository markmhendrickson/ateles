"""Tests for the Turdus swarm PR-approval-by-email path (approval loop).

Covers the pure detection helpers and the guarded end-to-end handler with the
Apis POST + gws +read stubbed out. The security-critical properties under test:

  - only the operator's own address may approve,
  - an APPROVE inside the QUOTED original notification must NOT count,
  - the correlation token resolves the exact PR,
  - a missing shared secret fails closed (no POST).
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "phoenicurus-release"))

import turdus  # noqa: E402
import prepare  # noqa: E402
from lib.approval import email_channel  # noqa: E402
from lib.approval.test_email_channel import triage_contract_response  # noqa: E402
from lib.notify import Notifier, Priority  # noqa: E402
from lib.notify import neotoma_sink  # noqa: E402


_AUTH_PASS = "mx.google.com; dmarc=pass header.from=example.com"


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_extract_sender_address_from_named():
    assert turdus._extract_sender_address("Mark H <MarkMHendrickson@gmail.com>") == (
        "markmhendrickson@gmail.com"
    )


def test_extract_sender_address_bare():
    assert turdus._extract_sender_address("plain@example.com") == "plain@example.com"


def test_parse_approve_token_ok():
    body = "Looks good.\n\nswarm-approve: markmhendrickson/neotoma#1902\n"
    assert turdus._parse_approve_token(body) == ("markmhendrickson/neotoma", 1902)


def test_parse_approve_token_absent():
    assert turdus._parse_approve_token("no token here") is None


def test_parse_approve_token_tolerates_quote_prefix():
    # Gmail quotes the original with '> '; the regex still finds the token.
    assert turdus._parse_approve_token("> swarm-approve: o/r#7") == ("o/r", 7)


def test_reply_says_approve_unquoted_true():
    assert turdus._reply_says_approve("Approve\n\n> Reply APPROVE to merge") is True


def test_reply_says_approve_only_in_quote_is_false():
    # The word APPROVE appears ONLY in the quoted original → not an approval.
    body = "> PR is READY TO MERGE. Reply APPROVE, approve on GitHub...\n"
    assert turdus._reply_says_approve(body) is False


def test_reply_says_approve_empty_false():
    assert turdus._reply_says_approve("") is False


# ── guarded handler ──────────────────────────────────────────────────────────


class _StubNotifier:
    def __init__(self):
        self.sent = []

    def send(self, message, priority=None, handler=None, email_eligible=True):
        self.sent.append(message)
        return True


def _run(coro):
    return asyncio.run(coro)


def _op_msg(**over):
    base = {
        "id": "m1",
        "sender": "Operator <op@example.com>",
        "subject": "Re: [Ateles] PR markmhendrickson/neotoma#1902 is READY TO MERGE",
    }
    base.update(over)
    return base


def _wire(monkeypatch, *, body, secret="s3cret", posted=None):
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    monkeypatch.delenv("ATELES_MAIL_AUTHSERV_ID", raising=False)
    monkeypatch.setattr(email_channel, "_fetch_auth_results",
                        lambda mid: [_AUTH_PASS])
    monkeypatch.setattr(turdus, "APIS_APPROVE_EMAIL_SECRET", secret)
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(turdus, "_read_message_body", lambda mid: body)
    monkeypatch.setattr(turdus, "_label_gmail_message", lambda *a, **k: True)

    async def fake_post(repository, pr_number, sender):
        if posted is not None:
            posted.append((repository, pr_number, sender))
        return True

    monkeypatch.setattr(turdus, "_post_email_approval", fake_post)


def test_operator_approve_routes_to_apis(monkeypatch):
    posted = []
    _wire(
        monkeypatch,
        body="Approve\n\n> swarm-approve: markmhendrickson/neotoma#1902",
        posted=posted,
    )
    handled = _run(turdus._maybe_handle_swarm_approval(_op_msg(), _StubNotifier()))
    assert handled is True
    assert posted == [("markmhendrickson/neotoma", 1902, "op@example.com")]


@pytest.mark.parametrize("kind", ("swarm", "release"))
@pytest.mark.parametrize("evidence", (
    ["mx.google.com; dmarc=fail header.from=example.com"],
    [],
    ["untrusted.example; dmarc=pass header.from=example.com"],
    None,
    "fetch_error",
))
def test_approval_authentication_failure_never_posts(monkeypatch, kind, evidence, caplog):
    posted = []
    _wire(monkeypatch, body=(
        "Approve\n\nswarm-approve: markmhendrickson/neotoma#1902"
        if kind == "swarm" else
        "approve v1.2.3\nrelease-approve: v1.2.3"
    ), posted=posted)
    if evidence == "fetch_error":
        def fail_fetch(_mid):
            raise OSError("synthetic metadata read failure")
        monkeypatch.setattr(email_channel, "_fetch_auth_results", fail_fetch)
    else:
        monkeypatch.setattr(email_channel, "_fetch_auth_results",
                            lambda _mid: evidence)

    async def fake_release_post(version, sender):
        posted.append((version, sender))
        return True

    monkeypatch.setattr(turdus, "_post_release_approval", fake_release_post)
    msg = _op_msg(subject=(
        "Re: [Ateles] PR markmhendrickson/neotoma#1902 is READY TO MERGE"
        if kind == "swarm" else "Re: Release v1.2.3 ready to approve"
    ))
    handler = (turdus._maybe_handle_swarm_approval if kind == "swarm"
               else turdus._maybe_handle_release_approval)
    notifier = _StubNotifier()
    assert _run(handler(msg, notifier)) is True
    assert posted == []
    assert len(notifier.sent) == 1
    assert "Telegram" in notifier.sent[0]
    assert "sender authentication missing, failed, or unreadable" in caplog.text


def test_release_approval_authenticated_sender_routes(monkeypatch):
    posted = []
    _wire(monkeypatch, body="approve v1.2.3\nrelease-approve: v1.2.3")

    async def fake_post(version, sender):
        posted.append((version, sender))
        return True

    monkeypatch.setattr(turdus, "_post_release_approval", fake_post)
    msg = _op_msg(subject="Re: Release v1.2.3 ready to approve")
    assert _run(turdus._maybe_handle_release_approval(msg, _StubNotifier())) is True
    assert posted == [("v1.2.3", "op@example.com")]


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("auth", ("pass", "fail", "missing", "unreadable"))
def test_two_mailbox_approval_effect(monkeypatch, tmp_path, kind, auth):
    operator_dir = tmp_path / "operator"
    swarm_dir = tmp_path / "swarm"
    operator_dir.mkdir()
    swarm_dir.mkdir()
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(swarm_dir))
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.net")
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", str(operator_dir))
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "SWARM_EMAIL", "swarm@example.net")
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(turdus, "_save_state", lambda _state: None)
    monkeypatch.setattr(turdus, "_poll_gmail_messages", turdus._poll_gmail_messages)
    monkeypatch.setattr(email_channel.shutil, "which", lambda _name: "/fake/gws")
    subject = ("Re: Release v1.2.3 ready to approve" if kind == "release" else
               "Re: [Ateles] PR o/r#7 is READY TO MERGE")
    body = ("approve v1.2.3\n> release-approve: v1.2.3" if kind == "release" else
            "APPROVE\n> swarm-approve: o/r#7")
    calls = []
    tasks = []

    def fake_run(cmd, **kwargs):
        config_dir = kwargs.get("env", {}).get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR",
                                                  str(operator_dir))
        calls.append((config_dir, cmd))
        assert cmd[0] == "/fake/gws" or cmd[0] == "gws"
        if "+send" in cmd:
            assert config_dir == str(swarm_dir)
            return subprocess.CompletedProcess(cmd, 0, "{}", "")
        if "+triage" in cmd:
            if config_dir == str(swarm_dir):
                token = cmd[cmd.index("--query") + 1]
                matching = ("release-approve:" in token) == (kind == "release")
                messages = [{"id": "approval-1", "from": "Op <op@example.com>",
                             "subject": subject}] if matching else []
            else:
                messages = []
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"complete": True, "messages": messages}), "")
        if "get" in cmd:
            assert config_dir == str(swarm_dir)
            if auth == "unreadable":
                return subprocess.CompletedProcess(cmd, 1, "", "synthetic error")
            values = ([{"name": "Authentication-Results", "value": _AUTH_PASS}]
                      if auth == "pass" else
                      [{"name": "Authentication-Results", "value":
                        "mx.google.com; dmarc=fail header.from=example.com"}]
                      if auth == "fail" else [])
            return subprocess.CompletedProcess(cmd, 0,
                json.dumps({"payload": {"headers": values}}), "")
        if "+read" in cmd:
            assert config_dir == str(swarm_dir)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"body_text": body}), "")
        raise AssertionError(cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)

    async def post(*args):
        calls.append(("posted", args))
        return True

    async def task(*args):
        tasks.append(args)

    monkeypatch.setattr(turdus, "_post_release_approval", post)
    monkeypatch.setattr(turdus, "_post_email_approval", post)
    monkeypatch.setattr(turdus, "_create_task_for_email", task)
    if kind == "release":
        assert prepare.email_send("Release v1.2.3 ready to approve", "notes")
    notifier = _StubNotifier()
    state = _run(turdus.poll_once(notifier, {}))
    posts = [c for c in calls if c[0] == "posted"]
    assert len(posts) == (1 if auth == "pass" else 0)
    assert tasks == []
    if auth != "pass":
        assert len(notifier.sent) == 1
        assert ("v1.2.3" if kind == "release" else "o/r#7") in notifier.sent[0]
        assert "Telegram" in notifier.sent[0]
    assert ("approval-1" in state.get("processed_ids", [])) == (auth != "unreadable")
    _run(turdus.poll_once(notifier, state))
    assert len([c for c in calls if c[0] == "posted"]) == len(posts)
    assert len(notifier.sent) == 1
    assert all(config == str(swarm_dir) for config, cmd in calls
               if isinstance(cmd, list) and ("get" in cmd or "+read" in cmd))


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_operator_inbox_lookalike_does_not_approve(monkeypatch, tmp_path, kind):
    operator_dir = tmp_path / "operator"
    swarm_dir = tmp_path / "swarm"
    operator_dir.mkdir()
    swarm_dir.mkdir()
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(swarm_dir))
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.net")
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", str(operator_dir))
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(email_channel.shutil, "which", lambda _name: "/fake/gws")
    posts = []

    def fake_run(cmd, **kwargs):
        config = kwargs.get("env", {}).get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR",
                                            str(operator_dir))
        if "+triage" in cmd:
            messages = [] if config == str(swarm_dir) else [{
                "id": "lookalike", "from": "op@example.com",
                "subject": ("Re: Release v1.2.3 ready to approve" if kind == "release"
                            else "Re: [Ateles] PR o/r#7 is READY TO MERGE") }]
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"complete": True, "messages": messages}), "")
        raise AssertionError(cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(turdus, "_save_state", lambda _state: None)
    async def post(*args):
        posts.append(args)
        return True
    monkeypatch.setattr(turdus, "_post_email_approval", post)
    monkeypatch.setattr(turdus, "_post_release_approval", post)
    async def store(_msg):
        return "synthetic"
    monkeypatch.setattr(turdus, "_store_email_entity", store)
    _run(turdus.poll_once(_StubNotifier(), {}))
    assert posts == []


def test_unconfigured_swarm_mailbox_makes_no_approval_gws_call(monkeypatch):
    monkeypatch.delenv("ATELES_SWARM_GWS_CONFIG_DIR", raising=False)
    monkeypatch.setattr(turdus, "_poll_gmail_messages", lambda *_: [])
    monkeypatch.setattr(turdus, "_save_state", lambda _state: None)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    _run(turdus.poll_once(_StubNotifier(), {}))
    assert calls == []


def test_non_operator_sender_ignored(monkeypatch):
    posted = []
    _wire(monkeypatch, body="Approve\n\nswarm-approve: o/r#1", posted=posted)
    msg = _op_msg(sender="Someone Else <attacker@example.test>")
    handled = _run(turdus._maybe_handle_swarm_approval(msg, _StubNotifier()))
    assert handled is False
    assert posted == []


def test_approve_only_in_quote_does_not_merge(monkeypatch):
    posted = []
    # Body carries the token but APPROVE appears only in the quoted original.
    _wire(
        monkeypatch,
        body="> PR is READY TO MERGE. Reply APPROVE...\n> swarm-approve: o/r#5",
        posted=posted,
    )
    handled = _run(turdus._maybe_handle_swarm_approval(_op_msg(), _StubNotifier()))
    assert handled is False
    assert posted == []


def test_no_token_treated_as_normal_reply(monkeypatch):
    posted = []
    _wire(monkeypatch, body="Approve — go ahead", posted=posted)
    handled = _run(turdus._maybe_handle_swarm_approval(_op_msg(), _StubNotifier()))
    assert handled is False
    assert posted == []


def test_wrong_subject_marker_skips_body_fetch(monkeypatch):
    posted = []
    _wire(monkeypatch, body="Approve\n\nswarm-approve: o/r#5", posted=posted)
    msg = _op_msg(subject="Re: lunch plans")
    handled = _run(turdus._maybe_handle_swarm_approval(msg, _StubNotifier()))
    assert handled is False
    assert posted == []


def test_missing_secret_fails_closed(monkeypatch):
    # Real _post_email_approval with no secret must not POST.
    monkeypatch.setattr(turdus, "APIS_APPROVE_EMAIL_SECRET", "")
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    ok = _run(turdus._post_email_approval("o/r", 1, "op@example.test"))
    assert ok is False


def test_operator_email_unset_disables_path(monkeypatch):
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "")
    handled = _run(turdus._maybe_handle_swarm_approval(_op_msg(), _StubNotifier()))
    assert handled is False


# ── Release email-approval parsing + version matching ───────────────────────
#
# The version guards are security-critical: a stale `approve` from an old
# release thread must NEVER approve a different version, and the quoted original
# email ("Reply approve <TAG>…") must never count as the operator's approval.


def test_parse_release_token_ok():
    assert turdus._parse_release_approve_token("release-approve: v0.20.0") == "v0.20.0"


def test_parse_release_token_in_body_line():
    body = "Notes...\n\nrelease-approve: v1.2.3-rc1\n"
    assert turdus._parse_release_approve_token(body) == "v1.2.3-rc1"


def test_parse_release_token_absent():
    assert turdus._parse_release_approve_token("no token here") is None


def test_reply_approves_exact_version():
    assert turdus._reply_approves_version("approve v0.20.0", "v0.20.0")
    # v-prefix optional in the reply
    assert turdus._reply_approves_version("approve 0.20.0", "v0.20.0")


def test_reply_rejects_different_version():
    # the stale-token bug: an approve for a DIFFERENT version must not match
    assert not turdus._reply_approves_version("approve v0.19.0", "v0.20.0")


def test_reply_rejects_bare_approve():
    # a bare "approve" with no version must not approve a specific release
    assert not turdus._reply_approves_version("approve", "v0.20.0")
    assert not turdus._reply_approves_version("looks good, approve it", "v0.20.0")


def test_reply_ignores_quoted_original():
    # the quoted original email carries "Reply approve v0.20.0 to publish" — it
    # must NOT count as the operator's own approval (Gmail prefixes quotes '>')
    quoted = "> Reply approve v0.20.0 to publish, or skip v0.20.0 to discard"
    assert not turdus._reply_approves_version(quoted, "v0.20.0")


def test_reply_approves_version_among_quoted_lines():
    # operator's own line approves; the quoted block below must be ignored either way
    body = (
        "approve v0.20.0\n"
        "\n"
        "> 🚀 Release v0.20.0 ready to approve\n"
        "> release-approve: v0.20.0\n"
    )
    assert turdus._reply_approves_version(body, "v0.20.0")


# ── _read_message_body must read gws's `body_text` key ──────────────────────
#
# Regression: gws `+read` returns the plaintext under `body_text`, but the
# reader only looked for body/text/plain/snippet — so it returned "" and every
# email `approve <version>` reply "carried no token" and was never routed
# (first live release-approval, 2026-07-27).


def test_read_body_prefers_body_text(monkeypatch):
    import subprocess as _sp

    class _R:
        returncode = 0
        stderr = ""
        stdout = '{"body_text":"approve v0.20.0\\n> release-approve: v0.20.0","body_html":"<p>x</p>"}'

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _R())
    body = turdus._read_message_body("msg-1")
    assert "release-approve: v0.20.0" in body
    assert body.startswith("approve v0.20.0")


def test_read_body_falls_back_to_html_when_no_text(monkeypatch):
    import subprocess as _sp

    class _R:
        returncode = 0
        stderr = ""
        stdout = '{"body_html":"<p>only html</p>"}'

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _R())
    assert turdus._read_message_body("msg-2") == "<p>only html</p>"


# ── notification loop regression (invoice re-notify bug) ─────────────────────
#
# The bug: an unread invoice was re-detected and re-notified on every ~5-min
# poll because (a) the poll query never excluded processed mail and (b) the
# notification fired on a per-cycle count with no per-message-ID dedup.


def _invoice_msg(mid="inv1"):
    return {
        "id": mid,
        "from": "billing@vendor.example",
        "subject": "Factura 2026-07 — payment due",
        "date": "2026-07-15T09:00:00Z",
        "labels": [],
    }


def _wire_poll(monkeypatch, *, messages):
    """Stub the poll cycle's side effects so only classification/notify runs."""
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(turdus, "_poll_gmail_messages", lambda *_: list(messages))

    async def _noop_store(_msg):
        return "ent_email_stub"

    async def _noop_task(*_a, **_k):
        return "ent_task_stub"

    async def _no_approval(_msg, _notifier):
        return False

    monkeypatch.setattr(turdus, "_store_email_entity", _noop_store)
    monkeypatch.setattr(turdus, "_create_task_for_email", _noop_task)
    monkeypatch.setattr(turdus, "_maybe_handle_swarm_approval", _no_approval)
    monkeypatch.setattr(turdus, "_maybe_handle_release_approval", _no_approval)
    monkeypatch.setattr(turdus, "_label_gmail_message", lambda *a, **k: True)


def test_poll_query_excludes_processed_label(monkeypatch):
    # The poll must ask Gmail to exclude already-processed mail, or the same
    # unread invoice returns every cycle.
    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stdout = '{"complete": True, "messages": []}'
            stderr = ""

        return R()

    monkeypatch.setattr(turdus.subprocess, "run", fake_run)
    turdus._poll_gmail_messages(5)
    assert "--query" in captured["cmd"]
    qi = captured["cmd"].index("--query") + 1
    assert f"-label:{turdus.PROCESSED_LABEL}" in captured["cmd"][qi]


def test_label_uses_real_modify_api_and_marks_read(monkeypatch):
    # Regression: this used to shell out to `gws gmail messages label`, a
    # subcommand that does not exist, so every label write failed silently and
    # `Turdus/processed` was never applied — the mechanical cause of the loop.
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)

        class R:
            returncode = 0
            stderr = ""
            # labels list → the label already exists; modify → echo an id back
            stdout = (
                "Using keyring backend: keyring\n"
                '{"labels":[{"name":"Turdus/processed","id":"Label_42"}]}'
                if "labels" in cmd
                else 'Using keyring backend: keyring\n{"id":"m1"}'
            )

        return R()

    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(turdus, "_LABEL_ID_CACHE", {})
    monkeypatch.setattr(turdus.subprocess, "run", fake_run)

    assert turdus._label_gmail_message("m1", turdus.PROCESSED_LABEL) is True

    modify = [c for c in calls if "modify" in c]
    assert modify, "must call the real users/messages/modify endpoint"
    cmd = modify[0]
    assert cmd[:5] == ["gws", "gmail", "users", "messages", "modify"]
    body = json.loads(cmd[cmd.index("--json") + 1])
    assert body["addLabelIds"] == ["Label_42"]  # resolved ID, not the name
    assert body["removeLabelIds"] == ["UNREAD"]  # marks read → leaves is:unread


def test_label_creates_missing_label(monkeypatch):
    # 'Turdus/processed' did not exist in the real mailbox; the resolver must
    # create it rather than silently failing forever.
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)

        class R:
            returncode = 0
            stderr = ""
            stdout = (
                '{"labels":[{"name":"Other","id":"L1"}]}'
                if ("labels" in cmd and "list" in cmd)
                else '{"id":"Label_new"}'
            )

        return R()

    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(turdus, "_LABEL_ID_CACHE", {})
    monkeypatch.setattr(turdus.subprocess, "run", fake_run)

    assert turdus._resolve_label_id("Turdus/processed") == "Label_new"
    assert any("create" in c for c in calls), "must create the absent label"


def test_invoice_notifies_once_then_deduped(monkeypatch):
    # First poll: one invoice → exactly one BLOCKER notification.
    # Second poll returns the SAME message ID (simulating a label-write lag) →
    # no second notification, because the ID is recorded in processed_ids.
    _wire_poll(monkeypatch, messages=[_invoice_msg("inv1")])
    notifier = _StubNotifier()

    state = _run(turdus.poll_once(notifier, {"last_message_id": None}))
    invoice_notes = [m for m in notifier.sent if "invoice(s)" in m]
    assert len(invoice_notes) == 1
    assert "inv1" in state.get("processed_ids", [])

    # Same message id comes back; state carries the dedup set forward.
    notifier2 = _StubNotifier()
    _run(turdus.poll_once(notifier2, state))
    assert [m for m in notifier2.sent if "invoice(s)" in m] == []


def test_dedup_set_guards_when_watermark_does_not_short_circuit(monkeypatch):
    # Exercises the processed_ids skip (turdus.py: the `msg_id in
    # processed_id_set` continue) DIRECTLY, not the last_message_id break.
    # We null out last_message_id on the second poll so the watermark walk
    # does NOT stop early — every message enters new_messages, and inv1's
    # suppression can therefore come ONLY from the dedup set. If that skip
    # were deleted, inv1 would notify a second time and this test would fail.
    _wire_poll(monkeypatch, messages=[_invoice_msg("inv1")])
    notifier = _StubNotifier()
    state = _run(turdus.poll_once(notifier, {"last_message_id": None}))
    assert len([m for m in notifier.sent if "invoice(s)" in m]) == 1
    assert "inv1" in state.get("processed_ids", [])

    # Second poll: inv2 (new) + inv1 (repeat). Force the watermark OFF so it
    # cannot short-circuit inv1 — only processed_ids can.
    state["last_message_id"] = None
    _wire_poll(monkeypatch, messages=[_invoice_msg("inv2"), _invoice_msg("inv1")])
    notifier2 = _StubNotifier()
    state2 = _run(turdus.poll_once(notifier2, state))
    # Exactly one invoice notified this cycle — inv2, the genuinely new one.
    # inv1 reached the loop body but was suppressed purely by the dedup set.
    notes = [m for m in notifier2.sent if "invoice(s)" in m]
    assert len(notes) == 1
    assert "1 invoice(s)" in notes[0]
    assert "inv2" in state2.get("processed_ids", [])


def test_approval_message_recorded_for_dedup(monkeypatch):
    # An operator approval reply must be recorded in processed_ids so it can
    # never be re-routed to the Apis publish gate on a later cycle.
    monkeypatch.setattr(turdus, "_poll_gmail_messages", lambda *_: [])
    monkeypatch.setattr(turdus, "_save_state", lambda _state: None)
    reply = email_channel.ApprovalReply(
        "appr1", "Re: Release v1.2.3 ready to approve",
        "approve v1.2.3\nrelease-approve: v1.2.3")
    monkeypatch.setattr(email_channel, "read_replies_with_status", lambda *_a, **_k:
        email_channel.ReadRepliesOutcome("ok", [], replies=(reply,)))

    async def _yes_release(_msg, _notifier):
        return True

    monkeypatch.setattr(turdus, "_maybe_handle_release_approval", _yes_release)
    notifier = _StubNotifier()
    state = _run(turdus.poll_once(notifier, {"last_message_id": None}))
    assert "appr1" in state.get("processed_ids", [])
    assert [m for m in notifier.sent if "invoice(s)" in m] == []


@pytest.mark.parametrize("failure", ("metadata", "body_none", "body_empty", "body_missing",
                                     "snippet_only", "preview_only", "html_empty"))
@pytest.mark.parametrize("kind", ("release", "pr"))
def test_unreadable_authenticated_reply_notices_once_then_routes_once(
    monkeypatch, tmp_path, kind, failure
):
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.net")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(turdus, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(turdus, "_poll_gmail_messages", lambda *_: [])
    monkeypatch.setattr(email_channel, "_gws", lambda: "/synthetic/gws")
    metadata_attempts = []

    def auth(_mid):
        metadata_attempts.append(_mid)
        return None if failure == "metadata" and len(metadata_attempts) == 1 else [_AUTH_PASS]

    monkeypatch.setattr(email_channel, "_fetch_auth_results", auth)
    subject = ("Re: Release v1.2.3 ready to approve" if kind == "release" else
               "Re: [Ateles] PR o/r#7 is READY TO MERGE")
    body = ("approve v1.2.3\nrelease-approve: v1.2.3" if kind == "release" else
            "APPROVE\nswarm-approve: o/r#7")
    read_attempts = []
    posts = []

    def fake_gws(args, timeout=45):
        if "+triage" in args:
            token = args[args.index("--query") + 1]
            matches = ("release-approve:" in token) == (kind == "release")
            return {"complete": True, "messages": [{"id": "approval-1", "from": "Op <op@example.com>",
                                  "subject": subject}]} if matches else {"complete": True, "messages": []}
        if "+read" in args:
            read_attempts.append(args)
            if len(read_attempts) == 1:
                return {"body_none": None, "body_empty": {},
                        "body_missing": {"id": "approval-1"},
                        "snippet_only": {"snippet": body},
                        "preview_only": {"preview": body},
                        "html_empty": {"body_html": "<div><style>approve</style></div>"}
                        }.get(failure, {"body_text": body})
            return {"body_text": body}
        raise AssertionError(args)

    async def post(*args):
        posts.append(args)
        return True

    monkeypatch.setattr(email_channel, "gws_json", fake_gws)
    monkeypatch.setattr(turdus, "_post_release_approval", post)
    monkeypatch.setattr(turdus, "_post_email_approval", post)
    notices = []
    for poll in range(3):
        notifier = _StubNotifier()
        state = _run(turdus.poll_once(notifier, turdus._load_state()))
        notices.extend(m for m in notifier.sent if "could not be read yet" in m)
        if poll < 1:
            assert "approval-1" not in state.get("processed_ids", [])
            assert posts == []
        else:
            assert state.get("processed_ids", []).count("approval-1") == 1
    assert len(notices) == 1
    assert ("v1.2.3" if kind == "release" else "o/r#7") in notices[0]
    assert "Telegram" in notices[0]
    assert "retries the read on its next poll" in notices[0]
    assert "Another email reply cannot approve" in notices[0]
    assert f"`{'approve v1.2.3' if kind == 'release' else 'approve'}`" in notices[0]
    assert "To retry by email" not in notices[0]
    assert len(posts) == 1


def _poll_authored_reply(monkeypatch, tmp_path, kind, body_data, *, polls=1,
                         include_token=True, triage_response=None,
                         authenticated=True, sent_body=None):
    """Exercise the real shared reader and both Turdus POST routes with fake gws."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.net")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(turdus, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(turdus, "_poll_gmail_messages", lambda *_: [])
    monkeypatch.setattr(email_channel, "_gws", lambda: "/synthetic/gws")
    monkeypatch.setattr(email_channel, "_fetch_auth_results",
                        lambda _mid: [_AUTH_PASS] if authenticated else [])
    subject = ("Re: Release v1.2.3 ready to approve" if kind == "release" else
               "Re: [Ateles] PR o/r#7 is READY TO MERGE")
    token_text = ("release-approve: v1.2.3" if kind == "release" else
                  "swarm-approve: o/r#7")
    if include_token and not any(prefix in str(body_data) for prefix in
                                 ("release-approve:", "swarm-approve:")):
        body_data = dict(body_data)
        if isinstance(body_data.get("body_text"), str):
            body_data["body_text"] += f"\n> {token_text}"
        elif isinstance(body_data.get("body_html"), str):
            body_data["body_html"] += f"<blockquote>{token_text}</blockquote>"

    def fake_gws(args, timeout=45):
        if "+triage" in args:
            token = args[args.index("--query") + 1]
            if token.startswith("in:sent"):
                return {"complete": True, "messages": [{
                    "id": "sent-1", "from": "Swarm <swarm@example.net>",
                    "subject": subject.removeprefix("Re: ")}] if sent_body else []}
            if ("release-approve:" in token) == (kind == "release"):
                base = {"complete": True, "messages": [{"id": "approval-1", "from": "Op <op@example.com>",
                                                       "subject": subject}]}
            else:
                base = {"complete": True, "messages": []}
            return triage_response(token, base) if triage_response else base
        if "+read" in args:
            return {"body_text": sent_body} if "sent-1" in args else body_data
        raise AssertionError(args)

    posts = []

    async def post(*args):
        posts.append(args)
        return True

    monkeypatch.setattr(email_channel, "gws_json", fake_gws)
    monkeypatch.setattr(turdus, "_post_release_approval", post)
    monkeypatch.setattr(turdus, "_post_email_approval", post)
    notices = _StubNotifier()
    state = {"last_message_id": None}
    for _ in range(polls):
        notifier = _StubNotifier()
        state = _run(turdus.poll_once(notifier, state))
        notices.sent.extend(notifier.sent)
    return posts, state, notices


@pytest.mark.parametrize("kind", ("pr", "release"))
@pytest.mark.parametrize("reply", ("yes", "Can you clarify?", "approve"))
def test_subject_only_reply_never_routes_and_notifies_once(
        monkeypatch, tmp_path, kind, reply):
    def subject_only(query, base):
        if "subject:" not in query:
            return {"complete": True, "messages": []}
        subject = ("Re: Release v1.2.3 ready to approve" if kind == "release" else
                   "Re: [Ateles] PR o/r#7 is READY TO MERGE")
        return {"complete": True, "messages": [
            {"id": "approval-1", "from": "Op <op@example.com>", "subject": subject}]}

    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_text": reply}, polls=2,
        include_token=False, triage_response=subject_only)
    target = "v1.2.3" if kind == "release" else "o/r#7"
    command = "approve v1.2.3" if kind == "release" else "approve"
    corrections = [message for message in notices.sent if "did not contain" in message]
    assert posts == [] and "approval-1" not in state.get("processed_ids", [])
    assert len(corrections) == 1
    assert target in corrections[0] and f"`{command}`" in corrections[0]
    assert "quote the request" in corrections[0]


@pytest.mark.parametrize("kind", ("pr", "release"))
def test_unauthenticated_subject_only_reply_gets_auth_notice(
        monkeypatch, tmp_path, kind):
    def subject_only(query, base):
        if "subject:" not in query:
            return {"complete": True, "messages": []}
        subject = ("Re: Release v1.2.3 ready to approve" if kind == "release" else
                   "Re: [Ateles] PR o/r#7 is READY TO MERGE")
        return {"complete": True, "messages": [
            {"id": "approval-1", "from": "Op <op@example.com>", "subject": subject}]}

    posts, _, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_text": "yes"},
        include_token=False, triage_response=subject_only,
        authenticated=False)
    command = "approve v1.2.3" if kind == "release" else "approve"
    assert posts == []
    assert any("sender could not be verified" in message and
               f"`{command}`" in message for message in notices.sent)


@pytest.mark.parametrize("kind", ("pr", "release"))
@pytest.mark.parametrize("container", (
    "<blockquote>{}</blockquote>", '<div class="gmail_quote">{}</div>',
    "<blockquote><blockquote>{}</blockquote></blockquote>",
))
def test_unproven_quote_with_withdrawal_holds_consumer(
        monkeypatch, tmp_path, kind, container):
    approval = "approve v1.2.3" if kind == "release" else "approve"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    html = f"<p>{approval}</p>" + container.format(f"<p>{token}</p><p>I withdraw</p>")
    posts, _, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_html": html})
    assert posts == []
    assert any("could not be read yet" in message for message in notices.sent)


@pytest.mark.parametrize("kind", ("pr", "release"))
def test_known_quote_still_routes_consumer(monkeypatch, tmp_path, kind):
    approval = "approve v1.2.3" if kind == "release" else "approve"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    posts, _, _ = _poll_authored_reply(
        monkeypatch, tmp_path, kind,
        {"body_html": f"<p>{approval}</p><blockquote><p>{token}</p></blockquote>"})
    assert len(posts) == 1


@pytest.mark.parametrize("kind", ("pr", "release"))
@pytest.mark.parametrize("shape", ("real_object", "array", "results"))
@pytest.mark.parametrize("fault", ("none", "full_page", "invalid_row"))
def test_cli_triage_contract_routes_only_complete_approval(
        monkeypatch, tmp_path, kind, shape, fault):
    approval = "APPROVE" if kind == "pr" else "approve v1.2.3"

    def response(token, base):
        if not base["messages"]:
            return base
        return triage_contract_response(
            shape, fault, {**base["messages"][0], "date": "2026-11-18T10:00:00Z"}, 40)

    posts, state, _ = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_text": approval},
        triage_response=response)
    assert len(posts) == (1 if fault == "none" else 0)
    assert ("approval-1" in state.get("processed_ids", [])) == (fault == "none")


_PLAIN_QUOTES = (
    "> Reply approve v1.2.3 to publish\n> release-approve: v1.2.3",
    "On Tue, Nov 17, 2026 Example wrote:\nReply APPROVE\nswarm-approve: o/r#7",
    "-----Original Message-----\nReply APPROVE\nswarm-approve: o/r#7",
    "From: Swarm <swarm@example.net>\nSent: Tuesday\nTo: Op\nSubject: READY TO MERGE\nReply APPROVE",
    "---------- Forwarded message ---------\nReply APPROVE",
    "Begin forwarded message:\nReply APPROVE",
    "-- \nReply APPROVE",
)


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("quote", _PLAIN_QUOTES)
def test_plain_quoted_notification_never_posts(monkeypatch, tmp_path, kind, quote):
    posts, state, _ = _poll_authored_reply(monkeypatch, tmp_path, kind,
                                           {"body_text": quote})
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])


_HTML_QUOTES = (
    '<blockquote>{quoted}</blockquote>',
    '<div class="gmail_quote">{quoted}</div>',
    '<div class="gmail_extra">{quoted}</div>',
    '<div class="gmail_attr">On Tue wrote:</div><div>{quoted}</div>',
    '<div class="yahoo_quoted">{quoted}</div>',
    '<div id="divRplyFwdMsg">From: swarm</div><div>{quoted}</div>',
    '<div id="appendonsend">{quoted}</div>',
    '<div type="cite">{quoted}</div>',
    '<div class="moz-cite-prefix">On Tue wrote:</div><div>{quoted}</div>',
    '<hr><div>From: swarm</div><div>{quoted}</div>',
)


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("quote", _HTML_QUOTES)
def test_html_quoted_notification_never_posts(monkeypatch, tmp_path, kind, quote):
    quoted = ("<p>Reply APPROVE, approve v1.2.3 to publish</p>"
              "<p>release-approve: v1.2.3</p><p>swarm-approve: o/r#7</p>")
    posts, state, _ = _poll_authored_reply(monkeypatch, tmp_path, kind,
                                           {"body_html": quote.format(quoted=quoted)})
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])


@pytest.mark.parametrize("kind,approval", (
    ("pr", "APPROVE"), ("pr", "approve 7"), ("pr", "approve #7"),
    ("pr", "approve o/r#7"), ("pr", "Approve #7!"),
    ("release", "approve v1.2.3"), ("release", "APPROVE 1.2.3"),
    ("release", "approve 1.2.3."),
))
@pytest.mark.parametrize("format", ("plain", "html"))
def test_authored_approval_routes_once(monkeypatch, tmp_path, kind, approval, format):
    quote = "swarm-approve: o/r#7" if kind == "pr" else "release-approve: v1.2.3"
    body = ({"body_text": f"{approval}\n> {quote}"} if format == "plain" else
            {"body_html": f"<p>{approval}</p><blockquote>{quote}</blockquote>"})
    posts, state, _ = _poll_authored_reply(monkeypatch, tmp_path, kind, body)
    assert len(posts) == 1
    assert state.get("processed_ids", []).count("approval-1") == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_matching_correlation_metadata_routes_once(monkeypatch, tmp_path, kind):
    body = ("approve v1.2.3\nrelease-approve: v1.2.3" if kind == "release" else
            "approve\nswarm-approve: o/r#7")
    posts, state, notices = _poll_authored_reply(monkeypatch, tmp_path, kind,
                                                  {"body_text": body}, polls=2)
    assert len(posts) == 1
    assert state.get("processed_ids", []).count("approval-1") == 1
    assert not any("no approval was recorded" in note for note in notices.sent)


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("format", ("plain", "html"))
def test_authored_approval_below_inline_quote_is_unreadable(monkeypatch, tmp_path, kind, format):
    approval = "APPROVE" if kind == "pr" else "approve v1.2.3"
    body = ({"body_text": f"> Different quoted line\n{approval}"} if format == "plain" else
            {"body_html": f"<blockquote>Different quoted line</blockquote><p>{approval}</p>"})
    posts, state, notices = _poll_authored_reply(monkeypatch, tmp_path, kind, body, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([note for note in notices.sent if "could not be read yet" in note]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_html_unmarked_notification_copy_is_unreadable(monkeypatch, tmp_path, kind):
    instruction = ("Reply approve v1.2.3 to publish" if kind == "release" else
                   "Reply APPROVE, approve on GitHub, or comment /approve to merge")
    token = ("release-approve: v1.2.3" if kind == "release" else
             "swarm-approve: o/r#7")
    body = {"body_html": f"<p>{instruction}</p><p>{token}</p>"}
    posts, state, notifier = _poll_authored_reply(monkeypatch, tmp_path, kind, body,
                                                  polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([m for m in notifier.sent if "could not be read yet" in m]) == 1


@pytest.mark.parametrize("kind,reply", (
    ("pr", "do not approve"), ("pr", "not approved yet"),
    ("pr", "no, approve later"), ("pr", "approve? no"),
    ("pr", "approve if CI passes"), ("pr", "approve but wait"),
    ("pr", "please approve"), ("pr", "approve #8"),
    ("pr", "approve o/r#8"), ("release", "I don't approve 1.2.3"),
    ("release", "approve v1.2.3?"), ("release", "approve v1.2.3 later"),
    ("release", "approve v1.2.3 unless ready"),
    ("release", "please approve v1.2.3"),
    ("release", "approve v1.2.4"),
))
def test_nonaffirmative_reply_never_posts(monkeypatch, tmp_path, kind, reply):
    posts, state, _ = _poll_authored_reply(monkeypatch, tmp_path, kind,
                                           {"body_text": reply})
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_conflicting_body_correlation_never_posts(monkeypatch, tmp_path, kind):
    body = ("approve v1.2.3\nrelease-approve: v1.2.4" if kind == "release" else
            "APPROVE\nswarm-approve: o/r#8")
    posts, state, notices = _poll_authored_reply(monkeypatch, tmp_path, kind,
                                                 {"body_text": body}, polls=2)
    assert posts == []
    assert state.get("approval_correction_ids") == ["approval-1"]
    matching = [n for n in notices.sent if "correlation target" in n]
    assert len(matching) == 1
    assert ("o/r#7" if kind == "pr" else "v1.2.3") in matching[0]
    assert ("o/r#8" if kind == "pr" else "v1.2.4") in matching[0]
    assert ("`approve`" if kind == "pr" else "`approve v1.2.3`") in matching[0]


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_missing_correlation_refuses_even_exact_approval(monkeypatch, tmp_path, kind):
    approval = "approve v1.2.3" if kind == "release" else "approve"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_text": approval}, polls=2,
        include_token=False)
    assert posts == []
    assert state.get("approval_correction_ids") == ["approval-1"]
    matching = [n for n in notices.sent if "did not contain" in n]
    assert len(matching) == 1
    assert ("o/r#7" if kind == "pr" else "v1.2.3") in matching[0]
    assert ("`approve`" if kind == "pr" else "`approve v1.2.3`") in matching[0]


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_quoted_matching_correlation_routes_once(monkeypatch, tmp_path, kind):
    approval = "approve v1.2.3" if kind == "release" else "approve"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    posts, state, _ = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_text": f"{approval}\n> {token}"}, polls=2)
    assert len(posts) == 1
    assert state.get("processed_ids") == ["approval-1"]


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("suffix", (
    "wait", "actually no", "unless X", "but not 0.9.2", "approve #8",
    "approve v1.2.4", "why?", "thank you", "approve o/r#8",
))
def test_second_authored_line_refuses_and_corrects_once(
    monkeypatch, tmp_path, kind, suffix
):
    approval = "approve v1.2.3" if kind == "release" else "approve"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_text": f"{approval}\n{suffix}"}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    corrections = [note for note in notices.sent if "no approval was recorded" in note]
    assert len(corrections) == 1
    assert ("v1.2.3" if kind == "release" else "o/r#7") in corrections[0]
    assert ("approve v1.2.3" if kind == "release" else "approve") in corrections[0]


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("reply", (
    "before\n{approval}", "{approval}\n{approval}",
    "please {approval} now", "{approval}\n{other_metadata}",
    "", "{approval} if CI passes", "{approval}?", "{approval} #8",
))
def test_closed_grammar_refuses_other_authored_text(
    monkeypatch, tmp_path, kind, reply
):
    approval = "approve v1.2.3" if kind == "release" else "approve"
    other_metadata = ("release-approve: v1.2.4" if kind == "release" else
                      "swarm-approve: o/r#8")
    body = reply.format(approval=approval, other_metadata=other_metadata)
    posts, state, notices = _poll_authored_reply(monkeypatch, tmp_path, kind,
                                                  {"body_text": body}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    if body:
        assert len([note for note in notices.sent if "no approval was recorded" in note]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("reply", ("Thank you for the update.", "yes", "skip",
                                     "Can we wait?", "approve maybe"))
def test_readable_nonapproval_gets_one_correction(monkeypatch, tmp_path, kind, reply):
    posts, _, notices = _poll_authored_reply(monkeypatch, tmp_path, kind,
                                              {"body_text": reply}, polls=2)
    assert posts == []
    assert len([note for note in notices.sent if "no approval was recorded" in note]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_empty_reply_above_quote_gets_correction(monkeypatch, tmp_path, kind):
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    posts, _, notices = _poll_authored_reply(monkeypatch, tmp_path, kind,
                                              {"body_text": f"> {token}"}, polls=2)
    assert posts == []
    assert len([note for note in notices.sent if "no approval was recorded" in note]) == 1


class _Transport:
    def __init__(self):
        self.sent = []
        self.deliver = True

    def notify(self, *, body):
        if self.deliver:
            self.sent.append(body)
        return self.deliver


def _real_notifier(monkeypatch, tmp_path, *, silence=False):
    monkeypatch.setattr(neotoma_sink, "record", lambda **_kw: None)
    notifier = Notifier(rubric={"silence_start": "00:00", "silence_end": "23:59"})
    notifier._email_primary = False
    notifier._digest_path = tmp_path / "held.json"
    notifier._dedupe_path = tmp_path / "dedupe.json"
    monkeypatch.setattr(notifier, "_in_silence_window", lambda: silence)
    monkeypatch.setattr(notifier, "should_flush_digest", lambda: False)
    transport = _Transport()
    notifier._apprise = transport
    return notifier, transport


def test_real_notifier_drops_info_and_holds_warn_in_silence(monkeypatch, tmp_path):
    notifier, transport = _real_notifier(monkeypatch, tmp_path, silence=True)
    assert notifier.send("synthetic routine", priority=Priority.INFO) is False
    assert notifier.send("synthetic warning", priority=Priority.WARN) is False
    assert transport.sent == []
    assert notifier.send("synthetic approval correction", priority=Priority.BLOCKER)
    assert transport.sent == ["synthetic approval correction"]


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("case", ("matching", "mismatch", "missing", "ordinary",
                                    "unreadable", "unauthenticated"))
def test_approval_notices_use_real_notifier_and_retry_failed_delivery(
    monkeypatch, tmp_path, kind, case
):
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(turdus, "_poll_gmail_messages", lambda *_: [])
    monkeypatch.setattr(turdus, "_save_state", lambda _state: None)
    subject = ("Re: Release v1.2.3 ready to approve" if kind == "release" else
               "Re: [Ateles] PR o/r#7 is READY TO MERGE")
    matching = "v1.2.3" if kind == "release" else "o/r#7"
    other = "v1.2.4" if kind == "release" else "o/r#8"
    target = None if case == "missing" else other if case == "mismatch" else matching
    approval = "approve v1.2.3" if kind == "release" else "approve"
    body = "Thank you for the update." if case == "ordinary" else approval
    reply = email_channel.ApprovalReply("synthetic-1", subject, body,
                                        authenticated=case != "unauthenticated")
    object.__setattr__(reply, "correlation_target", target)
    if case == "unreadable":
        outcome = email_channel.ReadRepliesOutcome(
            kind="transport_error", texts=[], unreadable_replies=(reply,))
    elif case == "unauthenticated":
        outcome = email_channel.ReadRepliesOutcome(
            kind="ok", texts=[], rejected_replies=(reply,))
    else:
        outcome = email_channel.ReadRepliesOutcome(kind="ok", texts=[], replies=(reply,))
    monkeypatch.setattr(email_channel, "read_replies_with_status", lambda *_a, **_kw: outcome)
    posts = []

    async def post(*args):
        posts.append(args)
        return True

    monkeypatch.setattr(turdus, "_post_release_approval", post)
    monkeypatch.setattr(turdus, "_post_email_approval", post)
    notifier, transport = _real_notifier(monkeypatch, tmp_path, silence=True)
    notifier._email_primary = True
    email_calls = []
    monkeypatch.setattr(notifier, "_deliver_email",
                        lambda message: email_calls.append(message) or True)
    state = {"last_message_id": None}
    if case != "matching":
        transport.deliver = False
        state = _run(turdus.poll_once(notifier, state))
        assert transport.sent == []
        assert "synthetic-1" not in state.get("approval_correction_ids", [])
        assert "synthetic-1" not in state.get("unreadable_notice_ids", [])
        transport.deliver = True
    state = _run(turdus.poll_once(notifier, state))
    state = _run(turdus.poll_once(notifier, state))
    assert len(transport.sent) == 1
    assert email_calls == []
    if case == "matching":
        assert len(posts) == 1
        assert "synthetic-1" in state.get("processed_ids", [])
    else:
        assert posts == []
        assert matching in transport.sent[0]
        if case == "mismatch":
            assert other in transport.sent[0]
        if case == "missing":
            assert "missing correlation token" in transport.sent[0]
        if case == "unreadable":
            assert state.get("unreadable_notice_ids") == ["synthetic-1"]
            assert f"`{approval}`" in transport.sent[0]
        elif case == "unauthenticated":
            assert f"`{approval}`" in transport.sent[0]
        elif case != "unauthenticated":
            assert state.get("approval_correction_ids") == ["synthetic-1"]


_BROKEN_QUOTE_HTML = (
    "<blockquote><p>{quoted}</blockquote></p>",
    "<blockquote><p>{quoted}</div></p></blockquote>",
    "<blockquote><div>{quoted}</div>",
    "<div><blockquote>{quoted}</blockquote>",
    "<BLOCKQUOTE><P>{quoted}</BLOCKQUOTE></P>",
    '<blockquote data-note="a > b"><p>{quoted}</blockquote></p>',
    "<!-- quote --><blockquote><p>{quoted}</blockquote></p>",
    "<![CDATA[quote]]><blockquote>{quoted}</blockquote>",
    "<div><blockquote>{quoted}</div></blockquote>",
    "<blockquote><blockquote><p>{quoted}</blockquote></p></blockquote>",
)


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("broken", _BROKEN_QUOTE_HTML)
def test_malformed_html_quote_is_unreadable_on_both_routes(
    monkeypatch, tmp_path, kind, broken
):
    approval = "approve v1.2.3" if kind == "release" else "approve"
    html = f"<p>{approval}</p>" + broken.format(quoted="quoted instruction")
    extracted, _ = email_channel._extract_authored_html(html)
    assert "quoted instruction" not in extracted
    posts, state, notices = _poll_authored_reply(monkeypatch, tmp_path, kind,
                                                  {"body_html": html}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([note for note in notices.sent if "could not be read yet" in note]) == 1
    assert not any("no approval was recorded" in note for note in notices.sent)


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_multipart_requires_agreement(monkeypatch, tmp_path, kind):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    for name, plain, html in (
        ("plain_approves", approval, "hold"),
        ("html_approves", "hold", approval),
        ("plain_blank", "> quoted text", approval),
    ):
        posts, state, notices = _poll_authored_reply(
            monkeypatch, tmp_path / name, kind,
            {"body_text": plain, "body_html": f"<p>{html}</p>"}, polls=2)
        assert posts == []
        assert "approval-1" not in state.get("processed_ids", [])
        assert len([note for note in notices.sent if "could not be read yet" in note]) == 1
        assert any(f"`{approval.lower()}`" in note.lower() for note in notices.sent)
    for name, payload in (
        ("matching", {"body_text": approval, "body_html": f"<p>{approval}</p>"}),
        ("plain_only", {"body_text": approval}),
        ("html_only", {"body_html": f"<p>{approval}</p>"}),
    ):
        posts, _, _ = _poll_authored_reply(monkeypatch, tmp_path / name, kind, payload)
        assert len(posts) == 1


_NON_RENDERED_TAGS = (
    "head", "title", "script", "style", "template", "noscript", "object",
    "iframe", "svg", "math", "canvas", "audio", "video", "select", "option",
    "datalist", "textarea",
)
_HIDING_ATTRIBUTES = (
    "hidden", 'aria-hidden="true"', 'style="display:none"',
    'style="visibility:hidden"', 'style="visibility:collapse"',
    'style="opacity:0"', 'style="font-size:0"',
    'style="font-size:0px"', 'style="font-size:0pt"',
    'style="max-height:0"', 'style="max-width:0"',
    'style="height:0;overflow:hidden"', 'style="width:0;overflow:hidden"',
    'style="mso-hide:all"', 'style="position:absolute;left:-9999px"',
    'style="position:fixed;top:-9999px"',
    'style="text-indent:-9999px"',
    'style="color:#fff;background-color:#fff"',
    'style="color:white;background:white"', 'style="clip:rect(0,0,0,0)"',
    'style="clip-path:inset(100%)"',
    'style="content-visibility:hidden"',
    'style="filter:opacity(0)"', 'style="transform:scale(0)"',
    'style="color:transparent"',
    'style="-webkit-text-fill-color:transparent"',
    'style="line-height:0"', 'style="transform:translateX(9999px)"',
)


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("tag", _NON_RENDERED_TAGS)
def test_html_non_rendered_affirmative_is_unreadable(monkeypatch, tmp_path, kind, tag):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    if tag in {"head", "title"}:
        html = f"<html><head><{tag}>{approval}</{tag}></head><body><blockquote>{token}</blockquote></body></html>"
    else:
        html = f"<body><{tag}>{approval}</{tag}><blockquote>{token}</blockquote></body>"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_html": html}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([m for m in notices.sent if "could not be read yet" in m]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("attr", _HIDING_ATTRIBUTES)
def test_html_hidden_affirmative_is_unreadable(monkeypatch, tmp_path, kind, attr):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    html = f"<body><div {attr}>{approval}</div><blockquote>{token}</blockquote></body>"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_html": html}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([m for m in notices.sent if "could not be read yet" in m]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("css", (
    ".hidden{display:none}", ".hidden{visibility:hidden}",
    "@media screen{.hidden{display:none}}", ".hidden{clip-path:inset(100%)}",
    "@import url('hidden.css');",
))
def test_html_stylesheet_hiding_is_unreadable(monkeypatch, tmp_path, kind, css):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    html = f"<html><head><style>{css}</style></head><body><div class='hidden'>{approval}</div><blockquote>{token}</blockquote></body></html>"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_html": html}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([m for m in notices.sent if "could not be read yet" in m]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("style", ("color:#333", "color:rgb(34,34,34);font-family:Arial;font-size:14px;margin:0"))
def test_visible_styled_html_body_still_approves(monkeypatch, tmp_path, kind, style):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    html = f"<html><head><title>Reply</title></head><body style='background:#fff'><p style='{style}'>{approval}</p><blockquote>{token}</blockquote></body></html>"
    posts, state, _ = _poll_authored_reply(monkeypatch, tmp_path, kind, {"body_html": html})
    assert len(posts) == 1
    assert state.get("processed_ids") == ["approval-1"]


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("wrapper", (
    "<span style='font-size:50%'>" * 20 + "{approval}" + "</span>" * 20,
    "<span style='font-size:.5em'>" * 20 + "{approval}" + "</span>" * 20,
    "<span style='font-size:smaller'>" * 20 + "{approval}" + "</span>" * 20,
    *[f"<p style='{side}:99999px'>{{approval}}</p>" for side in (
        "margin", "padding", "margin-left", "margin-right", "margin-top",
        "margin-bottom", "padding-left", "padding-right", "padding-top", "padding-bottom")],
    "<p style='margin:0 0 0 99999px'>{approval}</p>",
    "<p style='padding:0 99999px'>{approval}</p>",
    "<p style='width:99999px'>{approval}</p>",
    "<p style='position:relative;left:99999px'>{approval}</p>",
    "<details><p>{approval}</p></details>",
    "<dialog>{approval}</dialog>", "<menu>{approval}</menu>",
    '<p style="font-size:14px" style="font-size:0">{approval}</p>',
    '<p class="ordinary" class="gmail_quote">{approval}</p>',
    '<p hidden="" hidden="false">{approval}</p>',
    '<p id="ordinary" id="divRplyFwdMsg">{approval}</p>',
))
def test_ambiguous_html_never_routes_on_either_turdus_surface(
        monkeypatch, tmp_path, kind, wrapper):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    html = f"<body>{wrapper.format(approval=approval)}<blockquote>{token}</blockquote></body>"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_html": html}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([note for note in notices.sent if "could not be read yet" in note]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_open_details_summary_and_normal_styling_route(monkeypatch, tmp_path, kind):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    html = (f"<body><details open><summary>{approval}</summary></details>"
            f"<blockquote>{token}</blockquote></body>")
    posts, _, _ = _poll_authored_reply(monkeypatch, tmp_path, kind, {"body_html": html})
    assert len(posts) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("tail", (
    "I withdraw approval", "Name\nTitle\nLocation\nPhone\nExtra",
    "Please approve later", "Release v1.2.3", "PR #7",
))
def test_signature_tail_gets_correction(monkeypatch, tmp_path, kind, tail):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind,
        {"body_text": f"{approval}\n-- \n{tail}"}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([note for note in notices.sent if "no approval was recorded" in note]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_benign_signature_gets_exact_form_correction(monkeypatch, tmp_path, kind):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    posts, _, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind,
        {"body_text": f"{approval}\n-- \nExample Name\nEngineer"})
    assert posts == []
    assert any("no approval was recorded" in note for note in notices.sent)


def test_release_email_skip_correction_points_to_telegram(monkeypatch, tmp_path):
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, "release", {"body_text": "skip v1.2.3"}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([note for note in notices.sent if "Telegram release command" in note]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("property_name", ("color", "background"))
@pytest.mark.parametrize("colour", (
    "#0000", "#fff0", "hsla(0,0%,0%,0)", "hwb(0 0% 0% / 0)",
    "lab(0 0 0 / 0)", "lch(0 0 0 / 0)", "color(srgb 0 0 0 / 0)",
    "hsl(0,0%,0%)", "#00000000", "currentcolor", "var(--ink)",
))
def test_unsafe_html_colour_never_routes(monkeypatch, tmp_path, kind, property_name, colour):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind,
        {"body_html": f"<body><p style='{property_name}:{colour}'>{approval}</p></body>"}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([note for note in notices.sent if "could not be read yet" in note]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("style", (
    "color:#222;background:rgb(34,34,34)",
    "color:black;background:rgb(0,0,0)",
    "color:white;background:#fff",
    "color:hsl(0,0%,0%);background:black",
))
def test_equal_or_dark_background_never_routes(monkeypatch, tmp_path, kind, style):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    posts, state, _ = _poll_authored_reply(
        monkeypatch, tmp_path, kind,
        {"body_html": f"<body><p style='{style}'>{approval}</p></body>"})
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("tail", (
    "From: Swarm <swarm@example.net>\nI withdraw approval",
    "From: Swarm <swarm@example.net>\n> old message\nI withdraw approval",
    "On Tuesday Example wrote:\n> old message\nI withdraw approval",
))
def test_header_followed_by_authored_withdrawal_never_routes(monkeypatch, tmp_path, kind, tail):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_text": f"{approval}\n{tail}"}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([note for note in notices.sent if
                "could not be read yet" in note or "no approval was recorded" in note]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_unproven_header_and_signature_never_route(monkeypatch, tmp_path, kind):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    headers = "From: Example\nSent: Tuesday\nTo: Operator\nSubject: Request"
    for index, suffix in enumerate((
        "From: Swarm <swarm@example.net>\n> old message",
        "-- \nOperator",
        f"{headers}\n> old message",
        f"-----Original Message-----\n{headers}\nI withdraw",
        f"Begin forwarded message:\n{headers}\nI withdraw",
    )):
        posts, state, notices = _poll_authored_reply(
            monkeypatch, tmp_path / str(index), kind,
            {"body_text": f"{approval}\n{suffix}"}, polls=2)
        assert posts == []
        assert "approval-1" not in state.get("processed_ids", [])
        if "I withdraw" in suffix:
            assert len([note for note in notices.sent if "could not be read yet" in note]) == 1
            assert any(f"`{approval.lower()}`" in note.lower() for note in notices.sent)


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("tail", (
    "Subject: I rescind my consent", "From: I rescind my consent",
    "Date: I rescind my consent", "-- \nI rescind my consent",
    "--\nI rescind my consent",
    "-----Original Message-----\nI rescind my consent",
    "From: Example\nSent: Tuesday\nTo: Operator\nSubject: Request\nI rescind my consent",
))
def test_authored_withdrawal_never_routes(monkeypatch, tmp_path, kind, tail):
    approval = "approve v1.2.3" if kind == "release" else "approve"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_text": f"{approval}\n{tail}"}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([note for note in notices.sent if "no approval was recorded" in note]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("quote", (
    "On Tuesday Example wrote:\n> original",
    "-----Original Message-----\nFrom: Example\nSent: Tuesday\nTo: Operator\nSubject: Request\n> original",
    "Begin forwarded message:\nFrom: Example\nSent: Tuesday\nTo: Operator\nSubject: Request\n> original",
))
def test_proven_bottom_quote_still_routes(monkeypatch, tmp_path, kind, quote):
    approval = "approve v1.2.3" if kind == "release" else "approve"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    posts, _, _ = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_text": f"{approval}\n{quote}"},
        sent_body=f"original\n{token}")
    assert len(posts) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("markup", (
    "<p style='font-family:Wingdings'>{approval}</p>",
    "<font face='Webdings'>{approval}</font>",
    "<p style='font:14px Symbol'>{approval}</p>",
    "<p style='font-variant:small-caps'>{approval}</p>",
    "<p style='font-feature-settings:normal'>{approval}</p>",
    "<p style='font-synthesis:none'>{approval}</p>",
    "<p style='unicode-range:U+0-FFFF'>{approval}</p>",
    "<p style='text-transform:uppercase'>{approval}</p>",
    "<p style='direction:rtl'>{approval}</p>",
    "<p style='unicode-bidi:bidi-override'>{approval}</p>",
    "<p style='writing-mode:vertical-rl'>{approval}</p>",
    "<p style='text-orientation:sideways'>{approval}</p>",
))
def test_unsafe_font_never_routes(monkeypatch, tmp_path, kind, markup):
    approval = "approve v1.2.3" if kind == "release" else "approve"
    posts, state, _ = _poll_authored_reply(
        monkeypatch, tmp_path, kind,
        {"body_html": f"<body>{markup.format(approval=approval)}</body>"})
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("markup", (
    "<html><head><meta content='{approval}'></head><body><blockquote>{token}</blockquote></body></html>",
    "{approval}<html><body><blockquote>{token}</blockquote></body></html>",
    "<html><body><blockquote>{token}</blockquote></body>{approval}</html>",
    "<body><!-- {approval} --><blockquote>{token}</blockquote></body>",
))
def test_html_outside_body_and_metadata_do_not_approve(monkeypatch, tmp_path, kind, markup):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind,
        {"body_html": markup.format(approval=approval, token=token)}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([m for m in notices.sent if "could not be read yet" in m]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("location", ("before", "after_body", "after_html", "between"))
def test_html_unaccounted_text_holds_approval(monkeypatch, tmp_path, kind, location):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    body = f"<p>{approval}</p><blockquote>{token}</blockquote>"
    html = {
        "before": f"I withdraw<html><body>{body}</body></html>",
        "after_body": f"<html><body>{body}</body>I withdraw</html>",
        "after_html": f"<html><body>{body}</body></html>I withdraw",
        "between": f"<p>{approval}</p>I withdraw<blockquote>{token}</blockquote>",
    }[location]
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_html": html}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([m for m in notices.sent if "could not be read yet" in m]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("html", ("", " \n ", "<p></p>"))
def test_present_empty_html_holds_plain_approval(monkeypatch, tmp_path, kind, html):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind,
        {"body_text": approval, "body_html": html}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([m for m in notices.sent if "could not be read yet" in m]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("plain", ("", " \n "))
def test_present_empty_plain_holds_html_approval(monkeypatch, tmp_path, kind, plain):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind,
        {"body_text": plain, "body_html": f"<p>{approval}</p><blockquote>{token}</blockquote>"},
        polls=2, include_token=False)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([m for m in notices.sent if "could not be read yet" in m]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("fault", ("continuation", "invalid_envelope", "invalid_row"))
@pytest.mark.parametrize("query", ("active", "other"))
def test_incomplete_or_invalid_triage_never_posts_approval(
        monkeypatch, tmp_path, kind, fault, query):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"

    def response(token, base):
        active = ("release-approve:" in token) == (kind == "release")
        if active != (query == "active"):
            return base
        if fault == "continuation":
            return {**base, "nextPageToken": "next"}
        if fault == "invalid_row":
            return {"complete": True, "messages": [{"id": [], "subject": "RE: x",
                                                     "from": "op@example.com"}]}
        return {"invalid": True}

    posts, state, _ = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_text": approval},
        triage_response=response)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("tag,attribute", (
    ("body", "background='image.png'"),
    ("table", "background='image.png'"),
    ("td", "background='image.png'"),
    ("p", "class='ordinary'"),
    ("p", "bgcolor='#fff'"),
))
def test_unaccounted_html_attribute_holds_approval(
        monkeypatch, tmp_path, kind, tag, attribute):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    html = (f"<{tag} {attribute}>{approval}</{tag}>"
            f"<blockquote>{token}</blockquote>")
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind, {"body_html": html}, polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([m for m in notices.sent if "could not be read yet" in m]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_unknown_html_element_is_unreadable(monkeypatch, tmp_path, kind):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    posts, state, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind,
        {"body_html": f"<body><x-unknown>{approval}</x-unknown><blockquote>{token}</blockquote></body>"},
        polls=2)
    assert posts == []
    assert "approval-1" not in state.get("processed_ids", [])
    assert len([m for m in notices.sent if "could not be read yet" in m]) == 1


@pytest.mark.parametrize("kind", ("release", "pr"))
@pytest.mark.parametrize("readable", ("Thank you for the update.", "approval"))
@pytest.mark.parametrize("failure", ("unreadable", "fetch"))
def test_partial_sweep_corrects_readable_but_never_approves(monkeypatch, tmp_path, kind, readable, failure):
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.net")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(turdus, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(turdus, "_poll_gmail_messages", lambda *_: [])
    monkeypatch.setattr(email_channel, "_gws", lambda: "/synthetic/gws")
    monkeypatch.setattr(email_channel, "_fetch_auth_results", lambda _mid: [_AUTH_PASS])
    subject = ("Re: Release v1.2.3 ready to approve" if kind == "release" else
               "Re: [Ateles] PR o/r#7 is READY TO MERGE")
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    body = approval if readable == "approval" else readable
    fail_read = True

    def fake_gws(args, timeout=45):
        if "+triage" in args:
            query = args[args.index("--query") + 1]
            if ("release-approve:" in query) == (kind == "release"):
                return {"complete": True, "messages": [{"id": mid, "from": "Op <op@example.com>",
                                      "subject": subject} for mid in ("readable", "unreadable")]}
            return {"complete": True, "messages": []}
        if "+read" in args:
            mid = args[args.index("--id") + 1]
            if mid == "unreadable" and fail_read:
                if failure == "fetch":
                    return None
                return {"body_html": f"<body><div hidden>{approval}</div><blockquote>{token}</blockquote></body>"}
            return {"body_text": f"{body if mid == 'readable' else 'hold'}\n> {token}"}
        raise AssertionError(args)

    posts = []

    async def post(*args):
        posts.append(args)
        return True

    monkeypatch.setattr(email_channel, "gws_json", fake_gws)
    monkeypatch.setattr(turdus, "_post_release_approval", post)
    monkeypatch.setattr(turdus, "_post_email_approval", post)
    outcome = email_channel.read_replies_with_status(
        ["release-approve:", "swarm-approve:"], require_enabled=False)
    assert outcome.kind == "transport_error"
    assert outcome.texts == []
    assert [reply.message_id for reply in outcome.replies] == ["readable"]

    state = {"last_message_id": None}
    notices = []
    for _ in range(2):
        notifier = _StubNotifier()
        state = _run(turdus.poll_once(notifier, state))
        notices.extend(notifier.sent)
    assert posts == []
    assert "readable" not in state.get("processed_ids", [])
    assert len([note for note in notices if "could not be read yet" in note]) == 1
    notice = next(note for note in notices if "could not be read yet" in note)
    assert "retries the read on its next poll" in notice
    assert "Another email reply cannot approve" in notice
    assert "Telegram" in notice
    assert "To retry by email" not in notice
    if readable == "approval":
        assert state.get("approval_correction_ids", []) == []
        assert state.get("held_approval_notice_ids") == ["readable"]
        assert len([note for note in notices if "is held" in note]) == 1
        assert ("v1.2.3" if kind == "release" else "o/r#7") in next(
            note for note in notices if "is held" in note)
    else:
        assert state.get("approval_correction_ids") == ["readable"]
        assert len([note for note in notices if "no approval was recorded" in note]) == 1

    fail_read = False
    state = _run(turdus.poll_once(_StubNotifier(), state))
    assert len(posts) == (1 if readable == "approval" else 0)
    assert ("readable" in state.get("processed_ids", [])) == (readable == "approval")


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_partial_sweep_retries_failed_held_notice(monkeypatch, tmp_path, kind):
    monkeypatch.setattr(turdus, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(turdus, "_poll_gmail_messages", lambda *_: [])
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "op@example.com")
    target = "v1.2.3" if kind == "release" else "o/r#7"
    subject = ("Re: Release v1.2.3 ready to approve" if kind == "release" else
               "Re: [Ateles] PR o/r#7 is READY TO MERGE")
    reply = email_channel.ApprovalReply("m1", subject,
        "approve v1.2.3" if kind == "release" else "APPROVE",
        correlation_target=target)
    monkeypatch.setattr(email_channel, "read_replies_with_status", lambda *a, **k:
        email_channel.ReadRepliesOutcome("transport_error", [], replies=(reply,)))
    attempts = []

    class FlakyNotifier(_StubNotifier):
        def send(self, message, **kwargs):
            attempts.append((message, kwargs))
            return len(attempts) > 1

    state = {"last_message_id": None}
    for _ in range(3):
        state = _run(turdus.poll_once(FlakyNotifier(), state))
    assert len(attempts) == 2
    assert all("is held" in message for message, _ in attempts)
    assert all(kwargs["email_eligible"] is False for _, kwargs in attempts)
    assert state["held_approval_notice_ids"] == ["m1"]


@pytest.mark.parametrize("kind", ("pr", "release"))
@pytest.mark.parametrize("format", ("plain", "html"))
def test_complete_sent_notice_quote_routes_approval(monkeypatch, tmp_path, kind, format):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    sent = f"Ready for review.\n{token}"
    if format == "plain":
        body = {"body_text": f"{approval}\n> Ready for review.\n> {token}"}
    else:
        body = {"body_html": (f"<p>{approval}</p><blockquote>"
                              f"Ready for review.<br>{token}</blockquote>")}
    posts, _, _ = _poll_authored_reply(
        monkeypatch, tmp_path, kind, body, sent_body=sent)
    assert len(posts) == 1


@pytest.mark.parametrize("kind", ("pr", "release"))
@pytest.mark.parametrize("format", ("plain", "html"))
def test_quote_not_in_sent_notice_is_unreadable(monkeypatch, tmp_path, kind, format):
    approval = "approve v1.2.3" if kind == "release" else "APPROVE"
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    body = ({"body_text": f"{approval}\n> Other notice\n> {token}"}
            if format == "plain" else
            {"body_html": f"<p>{approval}</p><blockquote>Other notice<br>{token}</blockquote>"})
    posts, _, notices = _poll_authored_reply(
        monkeypatch, tmp_path, kind,
        body,
        sent_body=f"Ready for review.\n{token}")
    assert posts == []
    assert any("could not be read yet" in note for note in notices.sent)


@pytest.mark.parametrize("kind", ("pr", "release"))
@pytest.mark.parametrize("reverse", (False, True))
@pytest.mark.parametrize("correction,authenticated", (
    ("do not merge", True), ("approve", True), ("do not merge", False)))
def test_tokenless_reply_holds_matching_approval_sweep(
        monkeypatch, tmp_path, kind, reverse, correction, authenticated):
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.net")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(turdus, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(turdus, "_poll_gmail_messages", lambda *_: [])
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(email_channel, "_gws", lambda: "/synthetic/gws")
    subject = ("Re: Release v1.2.3 ready to approve" if kind == "release" else
               "Re: [Ateles] PR o/r#7 is READY TO MERGE")
    token = "release-approve: v1.2.3" if kind == "release" else "swarm-approve: o/r#7"
    approval = "approve v1.2.3" if kind == "release" else "approve"
    order = ["m2", "m1"] if reverse else ["m1", "m2"]

    def fake_gws(args, timeout=45):
        if "+triage" in args:
            query = args[args.index("--query") + 1]
            active = (("release-approve:" in query or
                       "ready to approve" in query) if kind == "release" else
                      ("swarm-approve:" in query or "READY TO MERGE" in query))
            mids = (["m1"] if "subject:" not in query else order) if active else []
            return {"complete": True, "messages": [
                {"id": mid, "subject": subject, "from": "op@example.com"}
                for mid in mids]}
        if "+read" in args:
            mid = args[args.index("--id") + 1]
            return {"body_text": f"{approval}\n> {token}" if mid == "m1" else correction}
        raise AssertionError(args)

    monkeypatch.setattr(email_channel, "gws_json", fake_gws)
    monkeypatch.setattr(email_channel, "_fetch_auth_results", lambda mid:
                        [_AUTH_PASS] if mid == "m1" or authenticated else [])
    posts = []

    async def post(*args):
        posts.append(args)
        return True

    monkeypatch.setattr(turdus, "_post_email_approval", post)
    monkeypatch.setattr(turdus, "_post_release_approval", post)
    notices = _StubNotifier()
    _run(turdus.poll_once(notices, {"last_message_id": None}))
    assert len(posts) == (0 if authenticated else 1)
    assert not any("no approval was recorded" in note for note in notices.sent)
    if authenticated:
        assert any("is held" in note for note in notices.sent)
