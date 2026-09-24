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
import payment_journal  # noqa: E402
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
    tok = _key_for(h, {}, yesterday)
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
    tok = _key_for(h, {}, "2026-09-22")
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
    tok = _key_for(h, {}, "2026-09-22")
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
    tok = _key_for(h, {}, "2026-09-22")
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
    tok = _key_for(h, {}, "2026-09-22")
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
    stale = _key_for(h, {}, "2026-01-01")
    live = _key_for(h, {}, "2026-09-22")
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
    tok = _key_for(h, match, yesterday_str)
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


# ── Real email_channel read path (PR #1202 security review) ─────────────────
#
# These do NOT stub read_replies_with_status: they stub only the gws boundary
# so the channel's own sender-authentication and complete-read rules decide
# what reaches the payment verdict overlay.

import json as _json  # noqa: E402

from lib.approval import email_channel as _ec  # noqa: E402

_GOOGLE_PASS = (
    "mx.google.com; dkim=pass header.i=@example.com header.s=s; "
    "dmarc=pass (p=NONE) header.from=example.com"
)


def _fake_gws(tok: str, *, auth_by_id: dict, body_by_id: dict):
    def fake(args, timeout=45):
        if "+triage" in args:
            return {"messages": [
                {"id": mid, "subject": f"RE: [ATELES] {subject_marker(tok)}",
                 "from": "Op <op@example.com>"}
                for mid in body_by_id
            ]}
        if list(args[:4]) == ["gmail", "users", "messages", "get"]:
            mid = _json.loads(args[args.index("--params") + 1])["id"]
            auth = auth_by_id.get(mid)
            if auth is None:
                return None
            return {"id": mid, "payload": {"headers": [
                {"name": "Authentication-Results", "value": v} for v in auth]}}
        if "+read" in args:
            mid = args[args.index("--id") + 1]
            body = body_by_id.get(mid)
            return None if body is None else {"body_text": body}
        return None
    return fake


def _arm_email(monkeypatch):
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(_ec, "_gws", lambda: "/bin/gws")
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)


def test_real_read_authenticated_operator_attended_is_approved(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    items = consent_email.build_pending_items([(h, [{}])], "2026-09-22")
    tok = items[0].token
    key = consent_email.match_key(items[0])
    _arm_email(monkeypatch)
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok, auth_by_id={"m1": [_GOOGLE_PASS]}, body_by_id={"m1": "ATTENDED"}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.states[key] == "approved"


def test_real_read_unauthenticated_operator_attended_is_held(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    items = consent_email.build_pending_items([(h, [{}])], "2026-09-22")
    tok = items[0].token
    key = consent_email.match_key(items[0])
    _arm_email(monkeypatch)
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok, auth_by_id={"m1": []}, body_by_id={"m1": "ATTENDED"}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.states[key] != "approved"
    assert key not in result.approved


def test_real_read_partial_failure_holds_every_payment(monkeypatch, tmp_path):
    """m1 (authenticated ATTENDED) loads; m2 fails. Monedula must hold, not act
    on the half it could read."""
    h = _Handler("therapy", label="Studio Example", amount=60)
    items = consent_email.build_pending_items([(h, [{}])], "2026-09-22")
    tok = items[0].token
    key = consent_email.match_key(items[0])
    _arm_email(monkeypatch)
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok,
        auth_by_id={"m1": [_GOOGLE_PASS], "m2": [_GOOGLE_PASS]},
        body_by_id={"m1": "ATTENDED", "m2": None}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.reason_code == "consent_reply_read_failed"
    assert result.states[key] == "blocked"
    assert result.approved == set()
    assert result.channel_ok is False


# ── Payment safety: term-bound consent, durable replay guard, unknown outcome ─
#
# PR #1202 review blockers (security: consent_terms_unbound, payment_replay;
# qa: crash-window double payment). Each drives ``monedula.main()`` across
# ticks and asserts the external-effect call count, the only thing that
# matters for a payment.


class _ProcessDeath(BaseException):
    """Stands in for the process being killed mid-tick (not an Exception)."""


def _email_env(monkeypatch):
    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", "email")
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")


def _quiet_main(monkeypatch, notify_calls=None):
    sink = notify_calls if notify_calls is not None else []
    monkeypatch.setattr(
        monedula,
        "_notify",
        lambda msg, priority="info", **k: sink.append((msg, priority, k)),
    )
    monkeypatch.setattr(monedula, "_emit_consent_escalation", lambda *a, **k: True)
    monkeypatch.setattr(monedula, "_post_escalation_entity", lambda *a, **k: True)
    monkeypatch.setattr(monedula, "_clear_notify_dedupe", lambda *a, **k: None)
    return sink


def _approve_all_read(seen: list):
    """Operator who approves every marker they are asked about, every tick.

    Replies accumulate in the inbox (as they do in Gmail), so each sweep sees
    every approval ever sent — the replay condition.
    """
    inbox: list[str] = []

    def fake_read(tokens, on_sender_rejected=None, **k):
        seen.append(list(tokens))
        for tok in tokens:
            text = f"RE: x\nATTENDED {subject_marker(tok)}"
            if text not in inbox:
                inbox.append(text)
        return ReadRepliesOutcome(kind="ok", texts=list(inbox))

    return fake_read


@pytest.mark.parametrize("change", ["amount", "payee"])
def test_approval_reused_after_terms_change_does_not_pay(monkeypatch, tmp_path, change):
    """An approval given for one amount/payee never pays a different one."""
    h = _Handler("therapy", label="Studio Example", amount=60, calendar=True)
    h.profile.wise_iban = "PAYEE-ORIGINAL"
    match = {"trigger": "calendar", "handler": "therapy"}
    h._match_fn = lambda events: [match]
    _email_env(monkeypatch)
    _install(monkeypatch, [h], tmp_path)
    _quiet_main(monkeypatch)

    sends: list = []
    asked: list = []
    replies: list[str] = []

    def fake_send(subject, body, to=None):
        sends.append((subject, body))
        return True

    def fake_read(tokens, on_sender_rejected=None, **k):
        asked.append(list(tokens))
        return ReadRepliesOutcome(kind="ok", texts=list(replies))

    monkeypatch.setattr(consent_email, "send_request", fake_send)
    monkeypatch.setattr(consent_email, "read_replies_with_status", fake_read)

    # Tick 1 — request sent for EUR 60 to the original payee; no reply yet.
    monedula.main()
    assert h.execute_calls == []
    original_token = asked[-1][0]

    # Terms change before the operator's reply arrives.
    if change == "amount":
        h.profile.amount_eur = 70
    else:
        h.profile.wise_iban = "PAYEE-CHANGED"

    # The operator approves the ORIGINAL request (the one they were shown).
    replies.append(f"RE: {subject_marker(original_token)}\nATTENDED")

    # Tick 2 — the old approval must not authorize the new terms.
    monedula.main()
    assert h.execute_calls == []
    # Changed terms are a new decision: a fresh request goes out, carrying a
    # new marker (the stale approval also draws a "no payment was made"
    # correction, which is not a request).
    requests = [s for s in sends if "correction" not in s[0]]
    assert len(requests) == 2
    assert subject_marker(original_token) not in requests[1][1]
    assert asked[-1][0] != original_token


def test_consumed_approval_replayed_after_pending_set_change_does_not_pay(
    monkeypatch, tmp_path
):
    """A paid obligation is never paid again, whatever the pending set does."""
    a = _Handler("therapy", label="Studio Example", amount=60, calendar=True)
    b = _Handler("yoga", label="Yoga Studio", amount=40, calendar=True)
    ma = {"trigger": "calendar", "handler": "therapy"}
    mb = {"trigger": "calendar", "handler": "yoga"}
    a._match_fn = lambda events: [ma]
    b._match_fn = lambda events: [mb]
    handlers = [a]
    _email_env(monkeypatch)
    _install(monkeypatch, handlers, tmp_path)
    _quiet_main(monkeypatch)
    monkeypatch.setattr(consent_email, "send_request", lambda *a_, **k: True)
    seen: list = []
    monkeypatch.setattr(
        consent_email, "read_replies_with_status", _approve_all_read(seen)
    )

    # Tick 1 — A approved and paid.
    monedula.main()
    assert a.execute_calls == [ma]

    # The pending set changes (B appears) while A's approval is still in the
    # inbox and the operator approves everything they are asked again.
    handlers.append(b)
    monedula.main()
    monedula.main()

    assert a.execute_calls == [ma]  # never twice
    assert b.execute_calls == [mb]


def test_crash_after_execute_before_outcome_recorded_does_not_repay(
    monkeypatch, tmp_path
):
    """Process death right after the transfer: restart holds as unknown."""
    h = _Handler("therapy", label="Studio Example", amount=60, calendar=True)
    match = {"trigger": "calendar", "handler": "therapy"}
    h._match_fn = lambda events: [match]
    _email_env(monkeypatch)
    _install(monkeypatch, [h], tmp_path)
    notify_calls = _quiet_main(monkeypatch)
    monkeypatch.setattr(consent_email, "send_request", lambda *a_, **k: True)
    seen: list = []
    monkeypatch.setattr(
        consent_email, "read_replies_with_status", _approve_all_read(seen)
    )

    real_execute = h.execute
    die = {"armed": True}

    def execute_then_die(m):
        out = real_execute(m)  # the external effect happens
        if die["armed"]:
            die["armed"] = False
            raise _ProcessDeath()  # killed before anything is recorded
        return out

    monkeypatch.setattr(h, "execute", execute_then_die)

    with pytest.raises(_ProcessDeath):
        monedula.main()
    assert len(h.execute_calls) == 1

    # Restart: same approval still in the inbox.
    ok = monedula.main()
    assert len(h.execute_calls) == 1  # not paid again
    assert ok is False  # an unknown outcome is surfaced, not a clean run
    unknown = [c for c in notify_calls if "unknown" in c[0].lower()]
    assert unknown, notify_calls
    assert unknown[0][1] == "blocker"

    # And it stays held on later ticks — never retried automatically.
    monedula.main()
    assert len(h.execute_calls) == 1


def test_email_consent_partial_execute_crash_does_not_repay_completed_match(
    monkeypatch, tmp_path
):
    """Match 1 pays, match 2 raises: re-run pays neither again."""
    h = _Handler("yoga", label="Yoga Studio", amount=60, calendar=True)
    m1 = {"event_id": "sess-1"}
    m2 = {"event_id": "sess-2"}
    h._match_fn = lambda events: [m1, m2]
    _email_env(monkeypatch)
    _install(monkeypatch, [h], tmp_path)
    notify_calls = _quiet_main(monkeypatch)
    monkeypatch.setattr(consent_email, "send_request", lambda *a_, **k: True)
    seen: list = []
    monkeypatch.setattr(
        consent_email, "read_replies_with_status", _approve_all_read(seen)
    )

    calls: list = []

    def execute(m):
        calls.append(m)
        if m is m2:
            raise RuntimeError("rail connection reset")
        return {"status": "sent"}

    monkeypatch.setattr(h, "execute", execute)

    with pytest.raises(RuntimeError):
        monedula.main()
    assert calls == [m1, m2]

    ok = monedula.main()
    assert calls == [m1, m2]  # m1 settled, m2 unknown — neither re-submitted
    assert ok is False
    assert any("unknown" in c[0].lower() for c in notify_calls)


def test_terms_changing_between_approval_and_execute_holds(monkeypatch, tmp_path):
    """Approval read, then the payee changes before execute: nothing pays."""
    h = _Handler("therapy", label="Studio Example", amount=60, calendar=True)
    h.profile.wise_iban = "PAYEE-ORIGINAL"
    match = {"trigger": "calendar", "handler": "therapy"}
    h._match_fn = lambda events: [match]
    _email_env(monkeypatch)
    _install(monkeypatch, [h], tmp_path)
    _quiet_main(monkeypatch)
    monkeypatch.setattr(consent_email, "send_request", lambda *a_, **k: True)

    def read_then_change(tokens, on_sender_rejected=None, **k):
        text = f"RE: x\nATTENDED {subject_marker(tokens[0])}"
        h.profile.wise_iban = "PAYEE-CHANGED"  # changes after the approval
        return ReadRepliesOutcome(kind="ok", texts=[text])

    monkeypatch.setattr(consent_email, "read_replies_with_status", read_then_change)
    monedula.main()
    assert h.execute_calls == []
    # The outstanding request is dropped so the next tick re-asks.
    assert not (tmp_path / "consent.json").exists()


def test_unreadable_payment_journal_holds_every_payment(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60, calendar=True)
    match = {"trigger": "calendar", "handler": "therapy"}
    h._match_fn = lambda events: [match]
    _email_env(monkeypatch)
    _install(monkeypatch, [h], tmp_path)
    notify_calls = _quiet_main(monkeypatch)
    monkeypatch.setattr(consent_email, "send_request", lambda *a_, **k: True)
    seen: list = []
    monkeypatch.setattr(
        consent_email, "read_replies_with_status", _approve_all_read(seen)
    )
    (tmp_path / payment_journal.JOURNAL_NAME).write_text("{not json")

    ok = monedula.main()
    assert ok is False
    assert h.execute_calls == []
    assert any("payment_journal_unreadable" in c[0] for c in notify_calls)
