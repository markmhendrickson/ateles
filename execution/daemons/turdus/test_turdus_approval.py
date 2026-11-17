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

    def send(self, message, priority=None, handler=None):
        self.sent.append(message)


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
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"messages": messages}), "")
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
    assert "approval-1" in state["processed_ids"]
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
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"messages": messages}), "")
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
            stdout = '{"messages": []}'
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


@pytest.mark.parametrize("kind", ("release", "pr"))
def test_unreadable_authenticated_reply_notices_once_then_routes_once(
    monkeypatch, tmp_path, kind
):
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.net")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(turdus, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(turdus, "_poll_gmail_messages", lambda *_: [])
    monkeypatch.setattr(email_channel, "_gws", lambda: "/synthetic/gws")
    monkeypatch.setattr(email_channel, "_fetch_auth_results", lambda _mid: [_AUTH_PASS])
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
            return {"messages": [{"id": "approval-1", "from": "Op <op@example.com>",
                                  "subject": subject}]} if matches else {"messages": []}
        if "+read" in args:
            read_attempts.append(args)
            return None if len(read_attempts) < 3 else {"body_text": body}
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
        if poll < 2:
            assert "approval-1" not in state.get("processed_ids", [])
            assert posts == []
        else:
            assert state.get("processed_ids", []).count("approval-1") == 1
    assert len(notices) == 1
    assert ("v1.2.3" if kind == "release" else "o/r#7") in notices[0]
    assert "Telegram" in notices[0]
    assert len(posts) == 1
