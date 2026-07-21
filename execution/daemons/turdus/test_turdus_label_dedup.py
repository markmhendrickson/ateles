"""Tests for the Turdus label-write + poll-dedup fix (ateles#224).

Root cause: `_label_gmail_message` shelled out to a nonexistent
`gws gmail messages label` subcommand, which always failed and was silently
swallowed. Every unread invoice stayed unread and got re-notified on every
poll — one or two real invoices produced ~23 duplicate operator notifications
in under an hour.

Covers, per the assembled spec's acceptance criteria:
  - effect-level verification that a real `gws gmail users messages modify`
    call is issued with a resolved label ID and UNREAD removed (not just that
    the shell call exits 0),
  - `_resolve_label_id` absent/present/create-fallback,
  - label-write failures are logged, not swallowed,
  - a previously-processed message is excluded from the next poll's query,
  - exactly one notification for one new invoice across rapid repeated polls
    (reproduces the reported burst-of-five symptom),
  - a failed label write does not poison dedup state (message stays eligible
    for retry on the next poll),
  - bounded processed_ids growth,
  - static grep proving the dead command path is gone,
  - `_is_invoice` classifier is untouched (#205 non-regression smoke check).
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import turdus  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _StubNotifier:
    def __init__(self):
        self.sent = []

    def send(self, message, priority=None, handler=None):
        self.sent.append(message)


def _msg(msg_id="m1", sender="billing@acme.test", subject="Invoice #42 due"):
    return {
        "id": msg_id,
        "sender": sender,
        "subject": subject,
        "snippet": "",
        "date_iso": "",
        "labels": [],
    }


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── 1. Dead-subcommand regression (static grep) ─────────────────────────────


def test_no_dead_label_subcommand_remains():
    src = Path(turdus.__file__).read_text()
    assert '"messages", "label"' not in src
    assert "gmail messages label" not in src


# ── 2. _resolve_label_id ─────────────────────────────────────────────────────


def test_resolve_label_id_found(monkeypatch):
    turdus._label_id_cache.clear()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted(
            0,
            json.dumps(
                {"labels": [{"id": "Label_123", "name": "Turdus/processed"}]}
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    label_id = turdus._resolve_label_id("Turdus/processed")
    assert label_id == "Label_123"
    assert calls[0][:5] == ["gws", "gmail", "users", "labels", "list"]
    # second call must hit the in-process cache, not re-invoke gws
    calls.clear()
    assert turdus._resolve_label_id("Turdus/processed") == "Label_123"
    assert calls == []


def test_resolve_label_id_creates_when_absent(monkeypatch):
    turdus._label_id_cache.clear()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "list" in cmd:
            return _FakeCompleted(0, json.dumps({"labels": []}))
        if "create" in cmd:
            return _FakeCompleted(
                0, json.dumps({"id": "Label_new", "name": "Turdus/processed"})
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    label_id = turdus._resolve_label_id("Turdus/processed")
    assert label_id == "Label_new"
    assert any("create" in c for c in calls)


def test_resolve_label_id_case_collision_does_not_match(monkeypatch):
    turdus._label_id_cache.clear()

    def fake_run(cmd, **kwargs):
        if "list" in cmd:
            return _FakeCompleted(
                0, json.dumps({"labels": [{"id": "Label_x", "name": "turdus/processed"}]})
            )
        if "create" in cmd:
            return _FakeCompleted(0, json.dumps({"id": "Label_created"}))
        raise AssertionError(cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    label_id = turdus._resolve_label_id("Turdus/processed")
    # exact-name match only — a case-collision on an existing label must not
    # be silently reused; the label gets created instead.
    assert label_id == "Label_created"


def test_resolve_label_id_list_failure_returns_none_and_logs(monkeypatch, caplog):
    turdus._label_id_cache.clear()

    def fake_run(cmd, **kwargs):
        return _FakeCompleted(1, "", "permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with caplog.at_level("ERROR"):
        label_id = turdus._resolve_label_id("Turdus/processed")
    assert label_id is None
    assert any("labels list failed" in r.message for r in caplog.records)


# ── 3. _label_gmail_message — effect-verified ────────────────────────────────


def test_label_gmail_message_issues_real_modify_call(monkeypatch):
    turdus._label_id_cache.clear()
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(turdus, "_resolve_label_id", lambda name: "Label_abc")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted(
            0, json.dumps({"id": "m1", "labelIds": ["Label_abc", "INBOX"]})
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok = turdus._label_gmail_message("m1", "Turdus/processed")

    assert ok is True
    assert calls[0][:5] == ["gws", "gmail", "users", "messages", "modify"]
    params_idx = calls[0].index("--params") + 1
    body_idx = calls[0].index("--json") + 1
    params = json.loads(calls[0][params_idx])
    body = json.loads(calls[0][body_idx])
    assert params == {"userId": "me", "id": "m1"}
    assert body["addLabelIds"] == ["Label_abc"]
    assert body["removeLabelIds"] == ["UNREAD"]


def test_label_gmail_message_verifies_effect_not_just_exit_code(monkeypatch):
    """A returncode==0 that reports UNREAD still present, or the target label
    missing, must NOT be treated as success — this is the effect-level check
    the issue's acceptance criteria requires (not merely 'the shell call
    succeeded')."""
    turdus._label_id_cache.clear()
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(turdus, "_resolve_label_id", lambda name: "Label_abc")

    def fake_run(cmd, **kwargs):
        # exit 0, but the response shows UNREAD was never removed.
        return _FakeCompleted(
            0, json.dumps({"id": "m1", "labelIds": ["Label_abc", "UNREAD"]})
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok = turdus._label_gmail_message("m1", "Turdus/processed")
    assert ok is False


def test_label_gmail_message_forced_failure_is_logged_not_swallowed(
    monkeypatch, caplog
):
    turdus._label_id_cache.clear()
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(turdus, "_resolve_label_id", lambda name: "Label_abc")

    def fake_run(cmd, **kwargs):
        return _FakeCompleted(1, "", "quotaExceeded")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with caplog.at_level("ERROR"):
        ok = turdus._label_gmail_message("m1", "Turdus/processed")

    assert ok is False
    assert any(
        "label write failed" in r.message and "m1" in r.message
        for r in caplog.records
    )


def test_label_gmail_message_no_resolvable_id_fails_closed(monkeypatch, caplog):
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(turdus, "_resolve_label_id", lambda name: None)
    with caplog.at_level("ERROR"):
        ok = turdus._label_gmail_message("m1", "Turdus/processed")
    assert ok is False
    assert any("could not resolve label id" in r.message for r in caplog.records)


# ── 4. _poll_gmail_messages — exclusion query ────────────────────────────────


def test_poll_gmail_messages_passes_exclusion_query(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted(0, json.dumps({"messages": []}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    turdus._poll_gmail_messages(20)

    assert calls, "expected a subprocess call"
    cmd = calls[0]
    assert "--query" in cmd
    query = cmd[cmd.index("--query") + 1]
    assert query == "is:unread in:inbox -label:Turdus/processed"


# ── 5. poll_once — two-cycle exclusion (root cause #2) ───────────────────────


def _wire_poll(monkeypatch, *, inbox, label_writes_ok=True):
    """Fixture inbox: `inbox` is a mutable list of message dicts, mutated by
    the fake `_poll_gmail_messages` to reflect Gmail-side exclusion once a
    message is confirmed labeled — mirrors real Gmail's `-label:` query
    filtering, independent of in-memory state."""
    labeled_ids = set()

    def fake_poll(max_count):
        return [m for m in inbox if m["id"] not in labeled_ids]

    def fake_label(msg_id, label):
        if label_writes_ok:
            labeled_ids.add(msg_id)
            return True
        return False

    async def fake_store_email_entity(msg):
        return None

    async def fake_create_task(msg, email_entity_id):
        return None

    monkeypatch.setattr(turdus, "_poll_gmail_messages", fake_poll)
    monkeypatch.setattr(turdus, "_label_gmail_message", fake_label)
    monkeypatch.setattr(turdus, "_store_email_entity", fake_store_email_entity)
    monkeypatch.setattr(turdus, "_create_task_for_email", fake_create_task)
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "")  # disable approval path
    return labeled_ids


def test_processed_invoice_excluded_from_next_poll(monkeypatch):
    inbox = [_msg("inv-1", sender="billing@acme.test", subject="Invoice due")]
    _wire_poll(monkeypatch, inbox=inbox)

    notifier = _StubNotifier()
    state = {}

    state = _run(turdus.poll_once(notifier, state))
    first_ids = {m["id"] for m in turdus._poll_gmail_messages(20)}
    assert "inv-1" not in first_ids  # Gmail-side exclusion after label write

    state = _run(turdus.poll_once(notifier, state))
    # Second cycle must not re-process inv-1: no new notification triggered.
    assert state["processed_ids"] == ["inv-1"]


# ── 6. poll_once — exactly one notification across rapid repeated polls ─────


def test_single_new_invoice_notifies_exactly_once_across_rapid_polls(monkeypatch):
    inbox = [_msg("inv-1", sender="billing@acme.test", subject="Invoice due")]
    _wire_poll(monkeypatch, inbox=inbox)

    notifier = _StubNotifier()
    state = {}

    for _ in range(5):
        state = _run(turdus.poll_once(notifier, state))

    invoice_notifications = [s for s in notifier.sent if "invoice" in s.lower()]
    assert len(invoice_notifications) == 1


# ── 7. poll_once — failed write does not poison dedup ordering ──────────────


def test_failed_label_write_does_not_add_to_processed_ids(monkeypatch, caplog):
    inbox = [_msg("inv-1", sender="billing@acme.test", subject="Invoice due")]
    _wire_poll(monkeypatch, inbox=inbox, label_writes_ok=False)

    notifier = _StubNotifier()
    state = {}
    with caplog.at_level("ERROR"):
        state = _run(turdus.poll_once(notifier, state))

    assert state["processed_ids"] == []
    assert notifier.sent == []  # not counted as handled → no notification
    assert any("label write failed" in r.message for r in caplog.records)


def test_message_already_in_processed_ids_is_skipped(monkeypatch):
    inbox = [_msg("inv-1", sender="billing@acme.test", subject="Invoice due")]
    _wire_poll(monkeypatch, inbox=inbox)

    calls = {"create_task": 0}

    async def counting_create_task(msg, email_entity_id):
        calls["create_task"] += 1

    monkeypatch.setattr(turdus, "_create_task_for_email", counting_create_task)

    notifier = _StubNotifier()
    state = {"processed_ids": ["inv-1"]}
    state = _run(turdus.poll_once(notifier, state))

    assert calls["create_task"] == 0
    assert notifier.sent == []


# ── 8. poll_once — zero-new / multi-invoice count accuracy ──────────────────


def test_zero_new_messages_sends_nothing(monkeypatch):
    _wire_poll(monkeypatch, inbox=[])
    notifier = _StubNotifier()
    state = _run(turdus.poll_once(notifier, {}))
    assert notifier.sent == []
    assert state.get("processed_ids", []) == []


def test_multiple_new_invoices_counted_accurately(monkeypatch):
    inbox = [
        _msg("inv-1", sender="billing@acme.test", subject="Invoice due"),
        _msg("inv-2", sender="billing@acme.test", subject="Invoice due"),
    ]
    _wire_poll(monkeypatch, inbox=inbox)
    notifier = _StubNotifier()
    state = _run(turdus.poll_once(notifier, {}))

    assert sorted(state["processed_ids"]) == ["inv-1", "inv-2"]
    invoice_msgs = [s for s in notifier.sent if "invoice" in s.lower()]
    assert len(invoice_msgs) == 1
    assert "2 invoice" in invoice_msgs[0]


# ── 9. bounded processed_ids growth ──────────────────────────────────────────


def test_processed_ids_bounded_at_cap(monkeypatch):
    inbox = [_msg(f"m{i}") for i in range(turdus._PROCESSED_IDS_CAP + 10)]
    _wire_poll(monkeypatch, inbox=inbox)
    monkeypatch.setattr(turdus, "MAX_MESSAGES", len(inbox))

    notifier = _StubNotifier()
    state = _run(turdus.poll_once(notifier, {}))

    assert len(state["processed_ids"]) <= turdus._PROCESSED_IDS_CAP


# ── 10. #205 classifier non-regression smoke check ──────────────────────────


def test_is_invoice_classifier_untouched_smoke():
    assert turdus._is_invoice("billing@acme.test", "Invoice #1", "") is True
    assert turdus._is_invoice("friend@example.com", "Lunch tomorrow?", "") is False


# ── 11. watermark must not freeze on an unhandled message ───────────────────
# Regression coverage for a self-review finding: advancing last_message_id to
# the newest FETCHED message (rather than the newest HANDLED one) causes the
# `msg_id == last_seen_id` break in the next poll's new_messages scan to
# permanently drop any message that was never labeled — informational/noise
# mail, or an actionable message whose label write kept failing.


def test_watermark_does_not_freeze_on_informational_message(monkeypatch):
    # Newest message is informational (never labeled); an older invoice is
    # genuinely new. The watermark must not jump past the invoice.
    inbox = [
        _msg("info-1", sender="someone@example.com", subject="FYI: heads up"),
        _msg("inv-1", sender="billing@acme.test", subject="Invoice due"),
    ]
    _wire_poll(monkeypatch, inbox=inbox)
    notifier = _StubNotifier()

    state = _run(turdus.poll_once(notifier, {}))

    assert "inv-1" in state["processed_ids"]
    assert len([s for s in notifier.sent if "invoice" in s.lower()]) == 1


def test_watermark_does_not_freeze_when_newest_label_write_fails(monkeypatch):
    # Two invoices arrive in one poll; the label write for the newest one
    # (first in the fixture) fails, the older one succeeds. The failed one
    # must remain eligible for retry on the next poll, not get silently
    # dropped by a frozen watermark.
    inbox = [
        _msg("inv-new", sender="billing@acme.test", subject="Invoice due"),
        _msg("inv-old", sender="billing@acme.test", subject="Invoice due"),
    ]
    labeled_ids = set()

    def fake_poll(max_count):
        return [m for m in inbox if m["id"] not in labeled_ids]

    def fake_label(msg_id, label):
        if msg_id == "inv-new":
            return False
        labeled_ids.add(msg_id)
        return True

    async def fake_store_email_entity(msg):
        return None

    async def fake_create_task(msg, email_entity_id):
        return None

    monkeypatch.setattr(turdus, "_poll_gmail_messages", fake_poll)
    monkeypatch.setattr(turdus, "_label_gmail_message", fake_label)
    monkeypatch.setattr(turdus, "_store_email_entity", fake_store_email_entity)
    monkeypatch.setattr(turdus, "_create_task_for_email", fake_create_task)
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "")

    notifier = _StubNotifier()
    state = _run(turdus.poll_once(notifier, {}))

    assert state["processed_ids"] == ["inv-old"]
    assert state["last_message_id"] == "inv-old"

    # Next poll: inv-new is still returned (Gmail never got the label) and
    # must still be retried, not skipped as already-seen or excluded by a
    # frozen watermark.
    calls = {"create_task": 0}

    async def counting_create_task(msg, email_entity_id):
        calls["create_task"] += 1

    monkeypatch.setattr(turdus, "_create_task_for_email", counting_create_task)
    _run(turdus.poll_once(notifier, state))
    assert calls["create_task"] == 1


# ── 12. swarm-approval path: failed label write is logged, not swallowed ────


def test_swarm_approval_label_write_failure_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(turdus, "OPERATOR_EMAIL", "markmhendrickson@gmail.com")
    monkeypatch.setattr(turdus, "APIS_APPROVE_EMAIL_SECRET", "s3cret")
    monkeypatch.setattr(turdus, "DRY_RUN", False)
    monkeypatch.setattr(
        turdus, "_read_message_body", lambda mid: "Approve\n\nswarm-approve: o/r#7"
    )
    monkeypatch.setattr(turdus, "_label_gmail_message", lambda *a, **k: False)

    async def fake_post(repository, pr_number, sender):
        return True

    monkeypatch.setattr(turdus, "_post_email_approval", fake_post)

    msg = {
        "id": "m1",
        "sender": "Mark <markmhendrickson@gmail.com>",
        "subject": "Re: PR o/r#7 is READY TO MERGE",
    }
    with caplog.at_level("ERROR"):
        handled = _run(turdus._maybe_handle_swarm_approval(msg, _StubNotifier()))

    # The approval itself still routed successfully — that's the side effect
    # that matters and must not be masked by the label write failing.
    assert handled is True
    assert any("label write failed" in r.message for r in caplog.records)
