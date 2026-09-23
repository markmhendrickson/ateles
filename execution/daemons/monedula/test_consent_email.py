"""Effect tests for Monedula email consent (ateles#1178).

Assert agent-/daemon-observable effects only: send_request call count,
read_replies_with_status.kind, per-handler state, execute count, notify kwargs,
fingerprint file, log event names. Synthetic fixtures only.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import consent_email  # noqa: E402
import monedula  # noqa: E402
from lib.approval.email_channel import ReadRepliesOutcome
from lib.approval.tokens import subject_marker, token_for


class _Handler:
    def __init__(self, name: str, *, label: str, amount: int, calendar: bool = True):
        self.name = name
        self.execute_calls: list = []
        self.profile = types.SimpleNamespace(
            label=label,
            amount_eur=amount,
            calendar_keywords=[name] if calendar else [],
            one_off=not calendar,
            due_date="" if calendar else "2026-09-22",
        )

    def matches(self, events):
        return [{"trigger": "calendar" if self.profile.calendar_keywords else "oneoff"}]

    def preview(self, match):
        return f"preview:{self.name}"

    def execute(self, match):
        self.execute_calls.append(match)
        return {"status": "sent", "handler": self.name}


def _install(monkeypatch, handlers):
    fake_mod = types.ModuleType("handlers")
    fake_mod.load_handlers = lambda strandings=None: list(handlers)
    monkeypatch.setitem(sys.modules, "handlers", fake_mod)
    monkeypatch.setattr(monedula, "STATE_FILE", Path("/tmp/.monedula_last_run_1178"))
    monkeypatch.setattr(
        monedula, "GATE_HEALTH_FILE", Path("/tmp/.monedula_gate_health_1178")
    )
    monkeypatch.setattr(monedula, "fetch_due_payment_tasks", lambda *a, **k: [])
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [{"summary": "x"}])
    monkeypatch.setattr(monedula, "telegram_send", lambda *a, **k: None)
    monkeypatch.setattr(monedula, "_check_already_ran_today", lambda: False)
    monkeypatch.setattr(monedula, "_mark_ran_today", lambda: None)
    for h in handlers:
        monkeypatch.setattr(
            h, "matches", lambda events, _h=h: [{"trigger": "t", "handler": _h.name}]
        )


# ── Channel select ───────────────────────────────────────────────────────────


def test_no_telegram_plus_email_armed_selects_email_not_channel_error(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("MONEDULA_CONSENT_CHANNEL", raising=False)
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
    channel, source = monedula._resolve_consent_channel()
    assert channel == "email"
    assert source == "automatic"


def test_env_override_email_source_env(monkeypatch):
    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", "email")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    channel, source = monedula._resolve_consent_channel()
    assert channel == "email"
    assert source == "env"


def test_missing_channel_config_is_blocker_not_silent_success(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("MONEDULA_CONSENT_CHANNEL", raising=False)
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
    monkeypatch.delenv("OPERATOR_EMAIL", raising=False)

    h = _Handler("therapy", label="Studio Example", amount=60)
    _install(monkeypatch, [h])
    monkeypatch.setattr(monedula, "STATE_FILE", tmp_path / "last")
    monkeypatch.setattr(monedula, "GATE_HEALTH_FILE", tmp_path / "gate")
    notify_calls = []
    monkeypatch.setattr(
        monedula,
        "_notify",
        lambda msg, priority="info", **k: notify_calls.append((msg, priority, k)),
    )
    monkeypatch.setattr(monedula, "_post_escalation_entity", lambda *a, **k: True)
    monkeypatch.setattr(monedula, "_emit_consent_escalation", lambda *a, **k: True)

    ok = monedula.main()
    assert ok is False
    assert h.execute_calls == []
    assert any("consent_channel_unconfigured" in c[0] for c in notify_calls)
    assert notify_calls[0][2].get("email_eligible") is True


def test_consent_channel_selected_log_names_fields_not_values(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", "email")
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
    h = _Handler("therapy", label="Studio Example", amount=60)
    _install(monkeypatch, [h])
    monkeypatch.setattr(monedula, "STATE_FILE", tmp_path / "last")
    monkeypatch.setattr(monedula, "GATE_HEALTH_FILE", tmp_path / "gate")
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None)
    with patch.object(
        consent_email,
        "request_and_collect",
        return_value=consent_email.ConsentEmailResult(
            states={"therapy": "awaiting_approval"},
            awaiting={"therapy"},
        ),
    ):
        with caplog.at_level(logging.INFO):
            monedula.main()
    joined = " ".join(r.message for r in caplog.records)
    assert "consent_channel_selected" in joined
    assert "channel=email" in joined
    assert "operator@example.com" not in joined


# ── Send-once / fingerprint ──────────────────────────────────────────────────


def test_pending_set_sends_exactly_one_send_request(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    yesterday = "2026-09-22"
    sends = []

    def fake_send(subject, body, to=None):
        sends.append((subject, body))
        return True

    monkeypatch.setattr(consent_email, "send_request", fake_send)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(kind="ok", texts=[]),
    )
    state = tmp_path / ".monedula_consent_email.json"
    result = consent_email.request_and_collect(
        [(h, [{"trigger": "t"}])], yesterday, state_path=state
    )
    assert len(sends) == 1
    subject, body = sends[0]
    tok = token_for("therapy", session=yesterday)
    assert subject_marker(tok) in subject or subject_marker(tok) in body
    assert "Studio Example" in body
    assert "60" in body
    assert "therapy" in body
    assert result.states["therapy"] == "awaiting_approval"
    mark = consent_email._load_mark(state)
    assert mark.get("fingerprint")


def test_fingerprint_persisted_only_after_send_true(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: False)
    state = tmp_path / "mark.json"
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=state
    )
    assert result.reason_code == "consent_request_send_failed"
    assert result.states["therapy"] == "blocked"
    assert not state.exists() or consent_email._load_mark(state).get("fingerprint") != (
        consent_email.pending_fingerprint(
            consent_email.build_pending_items([(h, [{}])], "2026-09-22")
        )
    )


def test_second_tick_same_pending_set_does_not_resend(monkeypatch, tmp_path, caplog):
    h = _Handler("therapy", label="Studio Example", amount=60)
    sends = []
    monkeypatch.setattr(
        consent_email, "send_request", lambda *a, **k: sends.append(1) or True
    )
    reads = []

    def fake_read(*a, **k):
        reads.append(1)
        return ReadRepliesOutcome(kind="ok", texts=[])

    monkeypatch.setattr(consent_email, "read_replies_with_status", fake_read)
    state = tmp_path / "mark.json"
    triggered = [(h, [{}])]
    with caplog.at_level(logging.INFO):
        consent_email.request_and_collect(triggered, "2026-09-22", state_path=state)
        consent_email.request_and_collect(triggered, "2026-09-22", state_path=state)
    assert len(sends) == 1
    assert len(reads) == 2
    assert any("consent_request_suppressed" in r.message for r in caplog.records)


def test_new_pending_handler_in_set_causes_resend(monkeypatch, tmp_path):
    h1 = _Handler("therapy", label="Studio Example", amount=60)
    h2 = _Handler("yoga", label="Yoga Studio", amount=60)
    sends = []
    monkeypatch.setattr(
        consent_email,
        "send_request",
        lambda subject, body, to=None: sends.append((subject, body)) or True,
    )
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(kind="ok", texts=[]),
    )
    state = tmp_path / "mark.json"
    consent_email.request_and_collect([(h1, [{}])], "2026-09-22", state_path=state)
    consent_email.request_and_collect(
        [(h1, [{}]), (h2, [{}])], "2026-09-22", state_path=state
    )
    assert len(sends) == 2
    assert "superseded" in sends[1][1].lower() or "CURRENT" in sends[1][1]


def test_empty_triggered_clears_consent_mark_and_failure_dedupe(monkeypatch, tmp_path):
    state = tmp_path / "mark.json"
    consent_email._save_mark(state, "abc", 1.0)
    cleared = []
    consent_email.request_and_collect(
        [], "2026-09-22", state_path=state, clear_failure_dedupe=lambda: cleared.append(1)
    )
    assert not state.exists()
    assert cleared == [1]


def test_body_lists_all_items_with_runtime_labels_and_markers():
    items = [
        consent_email.PendingItem(
            "therapy", "Studio Example", 60, "2026-09-22", True, "AAAA1111"
        ),
        consent_email.PendingItem(
            "yoga", "Yoga Studio", 60, "2026-09-22", True, "BBBB2222"
        ),
    ]
    _subj, body = consent_email.build_request_body(items, superseded=False)
    assert "Studio Example" in body and "Yoga Studio" in body
    assert subject_marker("AAAA1111") in body
    assert subject_marker("BBBB2222") in body
    assert "MUST include" in body or "every line MUST" in body


def test_body_does_not_treat_paid_as_pre_execution_verb():
    items = [
        consent_email.PendingItem(
            "therapy", "Studio Example", 60, "2026-09-22", True, "AAAA1111"
        ),
    ]
    _subj, body = consent_email.build_request_body(items, superseded=False)
    assert "PAID" not in body or "Do not use PAID" in body
    # Overlay must not map PAID → approved
    states, _ = consent_email.overlay_verdicts(
        items, ["RE: x\nPAID [APPROVE-AAAA1111]"]
    )
    assert states["therapy"] != "approved"


def test_statusful_ok_empty_is_awaiting_approval_not_skipped(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(kind="ok", texts=[]),
    )
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert result.states["therapy"] == "awaiting_approval"
    assert result.reason_code != "consent_reply_read_failed"


def test_transport_error_is_consent_reply_read_failed_no_execute(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(
            kind="transport_error", texts=[], detail="gws_triage_failed"
        ),
    )
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert result.reason_code == "consent_reply_read_failed"
    assert result.states["therapy"] == "blocked"


def test_non_operator_reply_ignored_payments_held(monkeypatch, tmp_path, caplog):
    h = _Handler("therapy", label="Studio Example", amount=60)
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)

    def fake_read(tokens, on_sender_rejected=None, **k):
        if on_sender_rejected:
            on_sender_rejected()
        return ReadRepliesOutcome(kind="ok", texts=[])

    monkeypatch.setattr(consent_email, "read_replies_with_status", fake_read)
    with caplog.at_level(logging.INFO):
        result = consent_email.request_and_collect(
            [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
        )
    assert result.states["therapy"] == "awaiting_approval"
    assert any("consent_reply_sender_rejected" in r.message for r in caplog.records)
    joined = " ".join(r.message for r in caplog.records)
    assert "attacker@" not in joined and "evil.example" not in joined


def test_operator_attended_approves_calendar_handler_only(monkeypatch, tmp_path):
    h1 = _Handler("therapy", label="Studio Example", amount=60, calendar=True)
    h2 = _Handler("yoga", label="Yoga Studio", amount=60, calendar=True)
    items_probe = consent_email.build_pending_items(
        [(h1, [{}]), (h2, [{}])], "2026-09-22"
    )
    tok1 = items_probe[0].token
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(
            kind="ok",
            texts=[f"RE: x\nATTENDED {subject_marker(tok1)}"],
        ),
    )
    result = consent_email.request_and_collect(
        [(h1, [{}]), (h2, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert result.states["therapy"] == "approved"
    assert result.states["yoga"] == "awaiting_approval"


def test_bare_approve_does_not_approve_calendar_handler(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60, calendar=True)
    items = consent_email.build_pending_items([(h, [{}])], "2026-09-22")
    tok = items[0].token
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(
            kind="ok",
            texts=[f"RE: {subject_marker(tok)}\nAPPROVE"],
        ),
    )
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert result.states["therapy"] != "approved"


def test_operator_approve_approves_non_calendar_handler(monkeypatch, tmp_path):
    h = _Handler("invoice", label="Acme Invoice", amount=100, calendar=False)
    items = consent_email.build_pending_items([(h, [{}])], "2026-09-22")
    tok = items[0].token
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(
            kind="ok",
            texts=[f"RE: {subject_marker(tok)}\nAPPROVE"],
        ),
    )
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert result.states["invoice"] == "approved"


def test_operator_skip_declines_that_handler_only(monkeypatch, tmp_path):
    h1 = _Handler("therapy", label="Studio Example", amount=60)
    h2 = _Handler("yoga", label="Yoga Studio", amount=60)
    items = consent_email.build_pending_items(
        [(h1, [{}]), (h2, [{}])], "2026-09-22"
    )
    tok1 = items[0].token
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(
            kind="ok",
            texts=[f"RE: x\nSKIP {subject_marker(tok1)}"],
        ),
    )
    result = consent_email.request_and_collect(
        [(h1, [{}]), (h2, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert result.states["therapy"] == "skipped"
    assert result.states["yoga"] == "awaiting_approval"


def test_multi_item_unbound_bare_verb_is_unrecognized(monkeypatch, tmp_path):
    h1 = _Handler("therapy", label="Studio Example", amount=60)
    h2 = _Handler("yoga", label="Yoga Studio", amount=60)
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(kind="ok", texts=["RE: x\nATTENDED"]),
    )
    result = consent_email.request_and_collect(
        [(h1, [{}]), (h2, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert "therapy" not in result.approved and "yoga" not in result.approved
    assert result.reason_code == "consent_reply_unrecognized"


def test_stale_session_token_does_not_approve(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    stale = token_for("therapy", session="2026-01-01")
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(
            kind="ok",
            texts=[f"RE: x\nATTENDED {subject_marker(stale)}"],
        ),
    )
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert result.states["therapy"] != "approved"


def test_consent_path_never_calls_fail_open_read_replies_wrapper(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    called = {"statusful": 0, "wrapper": 0}

    def statusful(*a, **k):
        called["statusful"] += 1
        return ReadRepliesOutcome(kind="ok", texts=[])

    def wrapper(*a, **k):
        called["wrapper"] += 1
        return []

    monkeypatch.setattr(consent_email, "read_replies_with_status", statusful)
    # If anyone imports read_replies into consent_email, fail the test.
    import lib.approval.email_channel as ec

    monkeypatch.setattr(ec, "read_replies", wrapper)
    consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert called["statusful"] == 1
    assert called["wrapper"] == 0


def test_single_item_allows_bare_verb_with_subject_marker(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    items = consent_email.build_pending_items([(h, [{}])], "2026-09-22")
    tok = items[0].token
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(
            kind="ok",
            texts=[f"RE: [Ateles] Monedula consent {subject_marker(tok)}\nATTENDED"],
        ),
    )
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert result.states["therapy"] == "approved"
