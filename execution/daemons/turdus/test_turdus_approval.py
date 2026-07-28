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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import turdus  # noqa: E402


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
        "sender": "Mark <markmhendrickson@gmail.com>",
        "subject": "Re: [Ateles] PR markmhendrickson/neotoma#1902 is READY TO MERGE",
    }
    base.update(over)
    return base


def _wire(monkeypatch, *, body, secret="s3cret", posted=None):
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "markmhendrickson@gmail.com")
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
    assert posted == [("markmhendrickson/neotoma", 1902, "markmhendrickson@gmail.com")]


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
