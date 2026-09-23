"""Effect tests for Monedula email consent (ateles#1178).

Assert agent-/daemon-observable effects only: send_request call count,
read_replies_with_status.kind, per-match (token) state, execute count, notify
kwargs, fingerprint file, log event names. Synthetic fixtures only.
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
        self._match_fn = None

    def matches(self, events):
        if self._match_fn is not None:
            return self._match_fn(events)
        return [{"trigger": "calendar" if self.profile.calendar_keywords else "oneoff"}]

    def preview(self, match):
        return f"preview:{self.name}"

    def execute(self, match):
        self.execute_calls.append(match)
        return {"status": "sent", "handler": self.name}


def _install(monkeypatch, handlers, tmp_path):
    """Wire handlers + isolate daemon/consent state under ``tmp_path``.

    Does **not** stub ``_check_already_ran_today`` / ``_mark_ran_today`` — the
    multi-tick lifecycle eval needs the real claim helpers.
    """
    fake_mod = types.ModuleType("handlers")
    fake_mod.load_handlers = lambda strandings=None: list(handlers)
    monkeypatch.setitem(sys.modules, "handlers", fake_mod)
    monkeypatch.setattr(monedula, "STATE_FILE", tmp_path / "last_run")
    monkeypatch.setattr(monedula, "GATE_HEALTH_FILE", tmp_path / "gate_health")
    monkeypatch.setattr(consent_email, "STATE_FILE", tmp_path / "consent.json")
    monkeypatch.setattr(monedula, "fetch_due_payment_tasks", lambda *a, **k: [])
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [{"summary": "x"}])
    monkeypatch.setattr(monedula, "telegram_send", lambda *a, **k: None)
    for h in handlers:
        if h._match_fn is None:
            monkeypatch.setattr(
                h,
                "matches",
                lambda events, _h=h: [{"trigger": "t", "handler": _h.name}],
            )


def _key_for(handler, match, yesterday: str) -> str:
    items = consent_email.build_pending_items([(handler, [match])], yesterday)
    return items[0].token


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
    _install(monkeypatch, [h], tmp_path)
    # Isolate claim for this branch (not a lifecycle eval).
    monkeypatch.setattr(monedula, "_check_already_ran_today", lambda: False)
    monkeypatch.setattr(monedula, "_mark_ran_today", lambda: None)
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
    _install(monkeypatch, [h], tmp_path)
    monkeypatch.setattr(monedula, "_check_already_ran_today", lambda: False)
    monkeypatch.setattr(monedula, "_mark_ran_today", lambda: None)
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None)
    tok = token_for("therapy", session=monedula._yesterday().isoformat())
    with patch.object(
        consent_email,
        "request_and_collect",
        return_value=consent_email.ConsentEmailResult(
            states={tok: "awaiting_approval"},
            awaiting={tok},
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
    result = consent_email.request_and_collect(
        [(h, [{}])], yesterday, state_path=tmp_path / "m.json"
    )
    assert len(sends) == 1
    _subj, body = sends[0]
    tok = token_for("therapy", session=yesterday)
    assert subject_marker(tok) in body
    assert "therapy" in body
    assert result.states[tok] == "awaiting_approval"


def test_fingerprint_persisted_only_after_send_true(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    state = tmp_path / "m.json"
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: False)
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=state
    )
    assert not state.exists()
    tok = token_for("therapy", session="2026-09-22")
    assert result.states[tok] == "blocked"
    assert result.channel_ok is False


def test_second_tick_same_pending_set_does_not_resend(monkeypatch, tmp_path, caplog):
    h = _Handler("therapy", label="Studio Example", amount=60)
    state = tmp_path / "m.json"
    sends = []
    monkeypatch.setattr(
        consent_email,
        "send_request",
        lambda *a, **k: sends.append(1) or True,
    )
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(kind="ok", texts=[]),
    )
    triggered = [(h, [{}])]
    with caplog.at_level(logging.INFO):
        consent_email.request_and_collect(triggered, "2026-09-22", state_path=state)
        consent_email.request_and_collect(triggered, "2026-09-22", state_path=state)
    assert len(sends) == 1
    assert any("consent_request_suppressed" in r.message for r in caplog.records)


def test_new_pending_handler_in_set_causes_resend(monkeypatch, tmp_path):
    h1 = _Handler("therapy", label="Studio Example", amount=60)
    h2 = _Handler("yoga", label="Yoga Studio", amount=60)
    state = tmp_path / "m.json"
    sends = []
    monkeypatch.setattr(
        consent_email,
        "send_request",
        lambda *a, **k: sends.append(1) or True,
    )
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(kind="ok", texts=[]),
    )
    consent_email.request_and_collect([(h1, [{}])], "2026-09-22", state_path=state)
    consent_email.request_and_collect(
        [(h1, [{}]), (h2, [{}])], "2026-09-22", state_path=state
    )
    assert len(sends) == 2


def test_empty_triggered_clears_consent_mark_and_failure_dedupe(monkeypatch, tmp_path):
    state = tmp_path / "m.json"
    state.write_text('{"fingerprint": "x", "sent_at": 1}')
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
    assert "only that match" in body


def test_body_does_not_treat_paid_as_pre_execution_verb():
    items = [
        consent_email.PendingItem(
            "therapy", "Studio Example", 60, "2026-09-22", True, "AAAA1111"
        ),
    ]
    _subj, body = consent_email.build_request_body(items, superseded=False)
    assert "PAID" not in body or "Do not use PAID" in body
    states, _ = consent_email.overlay_verdicts(
        items, ["RE: x\nPAID [APPROVE-AAAA1111]"]
    )
    assert states["AAAA1111"] != "approved"


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
    tok = token_for("therapy", session="2026-09-22")
    assert result.states[tok] == "awaiting_approval"
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
    tok = token_for("therapy", session="2026-09-22")
    assert result.reason_code == "consent_reply_read_failed"
    assert result.states[tok] == "blocked"


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
    tok = token_for("therapy", session="2026-09-22")
    assert result.states[tok] == "awaiting_approval"
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
    tok2 = items_probe[1].token
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
    assert result.states[tok1] == "approved"
    assert result.states[tok2] == "awaiting_approval"
    assert tok1 in result.approved
    assert tok2 in result.awaiting


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
    assert result.states[tok] != "approved"


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
    assert result.states[tok] == "approved"


def test_operator_skip_declines_that_handler_only(monkeypatch, tmp_path):
    h1 = _Handler("therapy", label="Studio Example", amount=60)
    h2 = _Handler("yoga", label="Yoga Studio", amount=60)
    items = consent_email.build_pending_items(
        [(h1, [{}]), (h2, [{}])], "2026-09-22"
    )
    tok1 = items[0].token
    tok2 = items[1].token
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
    assert result.states[tok1] == "skipped"
    assert result.states[tok2] == "awaiting_approval"


def test_skip_is_decisive_over_later_approve_same_token():
    """SKIP wins permanently for a match token (Falco / Pavo alignment)."""
    h = _Handler("invoice", label="Acme", amount=50, calendar=False)
    items = consent_email.build_pending_items([(h, [{}])], "2026-09-22")
    tok = items[0].token
    states, _ = consent_email.overlay_verdicts(
        items,
        [
            f"RE: {subject_marker(tok)}\nSKIP",
            f"RE: {subject_marker(tok)}\nAPPROVE",
        ],
    )
    assert states[tok] == "skipped"


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
    assert not result.approved
    assert result.reason_code == "consent_reply_unrecognized"


def test_stale_session_token_does_not_approve(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    stale = token_for("therapy", session="2026-01-01")
    live = token_for("therapy", session="2026-09-22")
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
    assert result.states[live] != "approved"


def test_consent_path_never_calls_fail_open_read_replies_wrapper(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    called = {"read_replies": 0, "statusful": 0}

    def boom(*a, **k):
        called["read_replies"] += 1
        raise AssertionError("fail-open read_replies must not be called")

    def ok_status(*a, **k):
        called["statusful"] += 1
        return ReadRepliesOutcome(kind="ok", texts=[])

    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr("lib.approval.email_channel.read_replies", boom)
    monkeypatch.setattr(consent_email, "read_replies_with_status", ok_status)
    consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert called["statusful"] == 1
    assert called["read_replies"] == 0


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
            texts=[f"RE: {subject_marker(tok)}\nATTENDED"],
        ),
    )
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert result.states[tok] == "approved"


# ── Match-grain execute effects (PM / UX option a) ───────────────────────────


def test_multi_match_same_handler_approves_only_marked_match(monkeypatch, tmp_path):
    h = _Handler("yoga", label="Yoga Studio", amount=60, calendar=True)
    m1 = {"event_id": "sess-1", "summary": "yoga 1"}
    m2 = {"event_id": "sess-2", "summary": "yoga 2"}
    items = consent_email.build_pending_items([(h, [m1, m2])], "2026-09-22")
    assert len(items) == 2
    tok1, tok2 = items[0].token, items[1].token
    assert tok1 != tok2

    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", "email")
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
    h._match_fn = lambda events: [m1, m2] if events else []
    _install(monkeypatch, [h], tmp_path)
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None)

    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(
            kind="ok",
            texts=[f"RE: x\nATTENDED {subject_marker(tok1)}"],
        ),
    )
    # Force yesterday session keys used above.
    monkeypatch.setattr(monedula, "_yesterday", lambda: __import__("datetime").date(2026, 9, 22))

    monedula.main()
    assert h.execute_calls == [m1]
    assert (tmp_path / "last_run").exists() is False  # sibling still awaiting


def test_multi_match_partial_skip_leaves_sibling_awaiting(monkeypatch, tmp_path):
    h = _Handler("yoga", label="Yoga Studio", amount=60, calendar=True)
    m1 = {"event_id": "sess-1"}
    m2 = {"event_id": "sess-2"}
    items = consent_email.build_pending_items([(h, [m1, m2])], "2026-09-22")
    tok1, tok2 = items[0].token, items[1].token

    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", "email")
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
    h._match_fn = lambda events: [m1, m2] if events else []
    _install(monkeypatch, [h], tmp_path)
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(monedula, "_yesterday", lambda: __import__("datetime").date(2026, 9, 22))
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(
            kind="ok",
            texts=[f"RE: x\nSKIP {subject_marker(tok1)}"],
        ),
    )
    monedula.main()
    assert h.execute_calls == []
    # Overlay grain: tok2 still awaiting (not skipped by sibling).
    overlay, _ = consent_email.overlay_verdicts(
        items, [f"RE: x\nSKIP {subject_marker(tok1)}"]
    )
    assert overlay[tok1] == "skipped"
    assert overlay[tok2] == "awaiting_approval"
    assert (tmp_path / "last_run").exists() is False


def test_multi_match_approve_both_markers_executes_both(monkeypatch, tmp_path):
    h = _Handler("yoga", label="Yoga Studio", amount=60, calendar=True)
    m1 = {"event_id": "sess-1"}
    m2 = {"event_id": "sess-2"}
    items = consent_email.build_pending_items([(h, [m1, m2])], "2026-09-22")
    tok1, tok2 = items[0].token, items[1].token

    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", "email")
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
    h._match_fn = lambda events: [m1, m2] if events else []
    _install(monkeypatch, [h], tmp_path)
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(monedula, "_yesterday", lambda: __import__("datetime").date(2026, 9, 22))
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(
            kind="ok",
            texts=[
                f"RE: x\nATTENDED {subject_marker(tok1)}\n"
                f"ATTENDED {subject_marker(tok2)}"
            ],
        ),
    )
    monedula.main()
    assert h.execute_calls == [m1, m2]
    assert (tmp_path / "last_run").read_text().strip()  # terminal → claimed


# ── Later-tick lifecycle (QA) ────────────────────────────────────────────────


def test_email_consent_approved_on_later_tick_executes_once(monkeypatch, tmp_path):
    """Empty reply on tick 1; ATTENDED on tick 2 executes once; tick 3 no repeat."""
    yesterday = monedula._yesterday()
    yesterday_str = yesterday.isoformat()
    h = _Handler("therapy", label="Studio Example", amount=60, calendar=True)
    match = {"trigger": "calendar", "handler": "therapy"}
    # Single match → session is date only (no event_id suffix).
    tok = token_for("therapy", session=yesterday_str)
    h._match_fn = lambda events: [match] if events else []

    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", "email")
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
    _install(monkeypatch, [h], tmp_path)
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(monedula, "_emit_consent_escalation", lambda *a, **k: True)
    monkeypatch.setattr(monedula, "_post_escalation_entity", lambda *a, **k: True)
    monkeypatch.setattr(monedula, "_clear_notify_dedupe", lambda *a, **k: None)

    sends: list = []
    reads: list = []
    tick = {"n": 0}

    def fake_send(subject, body, to=None):
        sends.append((subject, body))
        return True

    def fake_read(tokens, on_sender_rejected=None, **k):
        reads.append(list(tokens))
        tick["n"] += 1
        if tick["n"] == 1:
            return ReadRepliesOutcome(kind="ok", texts=[])
        return ReadRepliesOutcome(
            kind="ok",
            texts=[f"RE: {subject_marker(tok)}\nATTENDED"],
        )

    monkeypatch.setattr(consent_email, "send_request", fake_send)
    monkeypatch.setattr(consent_email, "read_replies_with_status", fake_read)

    # Tick 1 — send + empty inbox; leave day unclaimed.
    monedula.main()
    assert h.execute_calls == []
    assert len(sends) == 1
    assert len(reads) == 1
    assert (tmp_path / "consent.json").exists()
    assert not (tmp_path / "last_run").exists()

    # Tick 2 — same pending set; ATTENDED reply → execute once; claim day.
    monedula.main()
    assert h.execute_calls == [match]
    assert len(sends) == 1
    assert len(reads) == 2
    assert (tmp_path / "last_run").read_text().strip()
    assert not (tmp_path / "consent.json").exists()

    # Tick 3 — calendar claimed; no re-execute even if reply still present.
    monedula.main()
    assert h.execute_calls == [match]
    assert len(sends) == 1
    assert len(reads) == 2  # no consent sweep once calendar claimed + no triggered
