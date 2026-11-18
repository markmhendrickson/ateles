"""Effect tests for Monedula email consent (ateles#1178).

Assert agent-/daemon-observable effects only: send_request call count,
read_replies_with_status.kind, per-match (token) state, execute count, notify
kwargs, fingerprint file, log event names. Synthetic fixtures only.
"""

from __future__ import annotations

import html
import logging
import sys
import types
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import consent_email  # noqa: E402
import payment_journal  # noqa: E402
import monedula  # noqa: E402
from lib.approval.email_channel import ReadRepliesOutcome
from lib.approval.tokens import subject_marker, token_for
from lib.approval.test_email_channel import triage_contract_response


@pytest.fixture(autouse=True)
def _swarm_mailbox(monkeypatch, tmp_path):
    """Email consent requires a separate swarm mailbox (ateles#1221).

    Configure one by default so the flow under test is the one that can
    actually succeed; tests of the unconfigured case remove it explicitly.
    """
    cfg = tmp_path / "swarm-gws"
    cfg.mkdir(exist_ok=True)
    # The gate compares the swarm address against OPERATOR_EMAIL, so a test
    # must not depend on the host supplying one (monedula loads the host's
    # ~/.config/neotoma/.env at import; CI has none). Tests that need a
    # specific operator address override this.
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.net")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(cfg))


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


def test_default_channel_is_payment_approval_needing_no_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("MONEDULA_CONSENT_CHANNEL", raising=False)
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
    channel, source = monedula._resolve_consent_channel()
    assert channel == "payment_approval"
    assert source == "automatic"
    # Telegram credentials and TELEGRAM_ALLOWED_USER_ID are not required.
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)
    assert monedula._resolve_consent_channel() == ("payment_approval", "automatic")


def test_checkpoint_is_accepted_as_an_alias_of_payment_approval(monkeypatch):
    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", "checkpoint")
    assert monedula._resolve_consent_channel() == ("payment_approval", "env")


def test_env_override_email_source_env(monkeypatch):
    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", "email")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    channel, source = monedula._resolve_consent_channel()
    assert channel == "email"
    assert source == "env"


def test_unreachable_neotoma_is_blocker_not_silent_success(
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

    import payment_approval

    def unreachable(*a, **k):
        raise payment_approval.ApprovalError("Neotoma unreachable")

    monkeypatch.setattr(payment_approval, "_request", unreachable)

    ok = monedula.main()
    assert ok is False
    assert h.execute_calls == []
    assert any("payment_approval_unavailable" in c[0] for c in notify_calls)
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
    assert "keep the code in brackets" in body
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


@pytest.mark.parametrize("calendar,verb", ((True, "ATTENDED"), (False, "APPROVE")))
@pytest.mark.parametrize("extra", ("wait", "actually no", "unless X",
                                  "APPROVE [APPROVE-BBBB2222]", "thank you"))
def test_payment_reply_with_second_authored_line_never_approves(calendar, verb, extra):
    item = consent_email.PendingItem(
        "synthetic", "Synthetic", 25, "2026-09-22", calendar, "AAAA1111")
    states, unrecognized = consent_email.overlay_verdicts(
        [item], [f"RE: [APPROVE-AAAA1111]\n{verb}\n{extra}"])
    assert states["AAAA1111"] != "approved"
    assert unrecognized


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
            if tok not in args[args.index("--query") + 1]:
                return {"complete": True, "messages": []}
            return {"complete": True, "messages": [
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
            return None if body is None else body if isinstance(body, dict) else {"body_text": body}
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


@pytest.mark.parametrize("format", ("plain", "html"))
@pytest.mark.parametrize("authored", (False, True))
def test_consent_reader_excludes_quoted_request(monkeypatch, tmp_path, format, authored):
    h = _Handler("therapy", label="Studio Example", amount=60)
    items = consent_email.build_pending_items([(h, [{}])], "2026-09-22")
    tok = items[0].token
    request = next(line for line in consent_email.build_request_body(
        items, superseded=False)[1].splitlines() if line.startswith("Single-item:"))
    if format == "plain":
        body = ("ATTENDED\n" if authored else "") + "> " + request
        payload = {"body_text": body}
    else:
        body = ("<p>ATTENDED</p>" if authored else "") + \
               f"<div class='gmail_quote'>{request}</div>"
        payload = {"body_html": body}
    _arm_email(monkeypatch)
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok, auth_by_id={"m1": [_GOOGLE_PASS]}, body_by_id={"m1": payload}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert (tok in result.approved) is authored


@pytest.mark.parametrize("batch", (False, True))
@pytest.mark.parametrize("reply", ("yes", "Can you clarify?", "ATTENDED"))
def test_subject_only_payment_reply_cannot_authorize(
        monkeypatch, tmp_path, batch, reply):
    handlers = [_Handler("session", label="Synthetic", amount=60)]
    if batch:
        handlers.append(_Handler("invoice", label="Invoice", amount=25,
                                 calendar=False))
    _arm_email(monkeypatch)
    subject = "RE: [Ateles] Monedula consent 2026-09-22"

    def fake(args, timeout=45):
        if "+triage" in args:
            query = args[args.index("--query") + 1]
            rows = ([{"id": "reply-1", "subject": subject,
                      "from": "op@example.com"}]
                    if "subject:" in query else [])
            return {"complete": True, "messages": rows}
        if list(args[:4]) == ["gmail", "users", "messages", "get"]:
            return {"payload": {"headers": [
                {"name": "Authentication-Results", "value": _GOOGLE_PASS}]}}
        if "+read" in args:
            return {"body_text": reply}
        raise AssertionError(args)

    monkeypatch.setattr(_ec, "gws_json", fake)
    notices = []
    result = consent_email.request_and_collect(
        [(h, [{}]) for h in handlers], "2026-09-22",
        state_path=tmp_path / "consent.json",
        on_missing_token_reply=lambda msg, key: notices.append((msg, key)))
    assert result.approved == set()
    assert len(notices) == 1 and "reply-1" in notices[0][1]
    assert "quote it" in notices[0][0]
    assert "`attended session`" in notices[0][0]


@pytest.mark.parametrize("batch", (False, True))
@pytest.mark.parametrize("container", (
    "<blockquote>{}</blockquote>", '<div class="gmail_quote">{}</div>',
    "<blockquote><blockquote>{}</blockquote></blockquote>",
))
def test_unproven_payment_quote_holds_all_items(
        monkeypatch, tmp_path, batch, container):
    handlers = [_Handler("session", label="Synthetic", amount=60)]
    if batch:
        handlers.append(_Handler("invoice", label="Invoice", amount=25,
                                 calendar=False))
    items = consent_email.build_pending_items(
        [(h, [{}]) for h in handlers], "2026-09-22")
    token = items[0].token
    approval = ("<p>ATTENDED</p>" if not batch else
                "".join(f"<p>{'ATTENDED' if item.is_calendar else 'APPROVE'} "
                        f"{subject_marker(item.token)}</p>" for item in items))
    quoted = f"<p>{subject_marker(token)}</p><p>I withdraw</p>"
    _arm_email(monkeypatch)
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        token, auth_by_id={"m1": [_GOOGLE_PASS]},
        body_by_id={"m1": {"body_html": approval + container.format(quoted)}}))
    result = consent_email.request_and_collect(
        [(h, [{}]) for h in handlers], "2026-09-22",
        state_path=tmp_path / "consent.json")
    assert result.approved == set() and result.blocked
    assert result.reason_code == "consent_reply_read_failed"


def test_plain_payment_quote_outside_sent_request_holds(monkeypatch, tmp_path):
    handler = _Handler("session", label="Example", amount=60)
    item = consent_email.build_pending_items([(handler, [{}])], "2026-09-22")[0]
    _arm_email(monkeypatch)
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        item.token, auth_by_id={"m1": [_GOOGLE_PASS]},
        body_by_id={"m1": {"body_text": (
            f"ATTENDED\n> Different notice\n> {subject_marker(item.token)}")}}))
    result = consent_email.request_and_collect(
        [(handler, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_read_failed"


def test_consent_unmarked_html_request_is_unreadable(monkeypatch, tmp_path):
    h = _Handler("oneoff", label="Example", amount=60)
    items = consent_email.build_pending_items([(h, [{}])], "2026-09-22")
    tok = items[0].token
    _arm_email(monkeypatch)
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok, auth_by_id={"m1": [_GOOGLE_PASS]}, body_by_id={"m1": {
            "body_html": f"<p>Reply APPROVE to pay</p><p>{subject_marker(tok)}</p>"
        }}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_read_failed"


@pytest.mark.parametrize("wrapper", (
    "<head>{text}</head>", "<title>{text}</title>",
    "<script>{text}</script>", "<style>{text}</style>",
    "<template>{text}</template>", "<noscript>{text}</noscript>",
    "<object>{text}</object>", "<iframe>{text}</iframe>",
    "<svg>{text}</svg>", "<math>{text}</math>",
    "<canvas>{text}</canvas>", "<audio>{text}</audio>",
    "<video>{text}</video>", "<select>{text}</select>",
    "<option>{text}</option>", "<datalist>{text}</datalist>",
    "<textarea>{text}</textarea>",
))
def test_consent_nonrendered_html_affirmative_holds(monkeypatch, tmp_path, wrapper):
    h = _Handler("therapy", label="Studio Example", amount=60)
    tok = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0].token
    _arm_email(monkeypatch)
    html = f"<body>{wrapper.format(text='ATTENDED')}<blockquote>{subject_marker(tok)}</blockquote></body>"
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok, auth_by_id={"m1": [_GOOGLE_PASS]}, body_by_id={"m1": {"body_html": html}}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_read_failed"


@pytest.mark.parametrize("attr", (
    "hidden", 'aria-hidden="true"', 'style="display:none"',
    'style="visibility:hidden"', 'style="opacity:0"',
    'style="font-size:0"', 'style="font-size:0px"', 'style="font-size:0pt"',
    'style="max-height:0"', 'style="max-width:0"',
    'style="height:0;overflow:hidden"', 'style="width:0;overflow:hidden"',
    'style="mso-hide:all"', 'style="position:absolute;left:-9999px"',
    'style="text-indent:-9999px"',
    'style="color:#fff;background-color:#fff"',
    'style="color:white;background:white"',
    'style="clip:rect(0,0,0,0)"', 'style="clip-path:inset(100%)"',
    'style="color:transparent"',
    'style="-webkit-text-fill-color:transparent"',
    'style="line-height:0"', 'style="transform:translateX(9999px)"',
))
def test_consent_hidden_html_affirmative_holds(monkeypatch, tmp_path, attr):
    h = _Handler("therapy", label="Studio Example", amount=60)
    tok = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0].token
    _arm_email(monkeypatch)
    html = f"<body><p {attr}>ATTENDED</p><blockquote>{subject_marker(tok)}</blockquote></body>"
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok, auth_by_id={"m1": [_GOOGLE_PASS]}, body_by_id={"m1": {"body_html": html}}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_read_failed"


def test_consent_class_hidden_by_stylesheet_holds(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    tok = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0].token
    _arm_email(monkeypatch)
    html = ("<html><head><style>.hidden{display:none}</style></head>"
            f"<body><p class='hidden'>ATTENDED</p><blockquote>{subject_marker(tok)}</blockquote></body></html>")
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok, auth_by_id={"m1": [_GOOGLE_PASS]}, body_by_id={"m1": {"body_html": html}}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_read_failed"


@pytest.mark.parametrize("colour", (
    "#0000", "#fff0", "hsla(0,0%,0%,0)", "hwb(0 0% 0% / 0)",
    "lab(0 0 0 / 0)", "lch(0 0 0 / 0)", "color(srgb 0 0 0 / 0)",
    "hsl(0,0%,0%)", "rgb(0,0,0)",
))
@pytest.mark.parametrize("property_name", ("color", "background"))
def test_consent_unsafe_html_colour_holds(monkeypatch, tmp_path, colour, property_name):
    h = _Handler("therapy", label="Studio Example", amount=60)
    tok = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0].token
    _arm_email(monkeypatch)
    html = (f"<body><p style='{property_name}:{colour}'>ATTENDED</p>"
            f"<blockquote>{subject_marker(tok)}</blockquote></body>")
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok, auth_by_id={"m1": [_GOOGLE_PASS]}, body_by_id={"m1": {"body_html": html}}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    if property_name == "color" and colour == "rgb(0,0,0)":
        assert result.approved == {consent_email.match_key(
            consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0])}
    else:
        assert result.approved == set()
        assert result.reason_code == "consent_reply_read_failed"


@pytest.mark.parametrize("style", (
    "color:#222;background:rgb(34,34,34)",
    "color:black;background:rgb(0,0,0)",
    "color:white;background:#fff",
    "color:hsl(0,0%,0%);background:black",
))
def test_consent_equal_or_dark_background_holds(monkeypatch, tmp_path, style):
    h = _Handler("therapy", label="Studio Example", amount=60)
    tok = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0].token
    _arm_email(monkeypatch)
    html = (f"<body><p style='{style}'>ATTENDED</p>"
            f"<blockquote>{subject_marker(tok)}</blockquote></body>")
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok, auth_by_id={"m1": [_GOOGLE_PASS]}, body_by_id={"m1": {"body_html": html}}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_read_failed"


def test_consent_normal_styled_html_approves(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Studio Example", amount=60)
    item = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0]
    _arm_email(monkeypatch)
    html = ("<body style='background:white'><p style='color:rgb(34,34,34);"
            "font-family:Arial;font-size:14px;margin:0'>ATTENDED</p>"
            f"<blockquote>{subject_marker(item.token)}</blockquote></body>")
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        item.token, auth_by_id={"m1": [_GOOGLE_PASS]}, body_by_id={"m1": {"body_html": html}}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == {consent_email.match_key(item)}


@pytest.mark.parametrize("calendar", (True, False))
@pytest.mark.parametrize("wrapper", (
    "<span style='font-size:50%'>" * 20 + "{verb}" + "</span>" * 20,
    "<span style='font-size:.5em'>" * 20 + "{verb}" + "</span>" * 20,
    "<span style='font-size:smaller'>" * 20 + "{verb}" + "</span>" * 20,
    *[f"<p style='{side}:99999px'>{{verb}}</p>" for side in (
        "margin", "padding", "margin-left", "margin-right", "margin-top",
        "margin-bottom", "padding-left", "padding-right", "padding-top", "padding-bottom")],
    "<p style='margin:0 0 0 99999px'>{verb}</p>",
    "<p style='padding:0 99999px'>{verb}</p>",
    "<p style='width:99999px'>{verb}</p>",
    "<details><p>{verb}</p></details>",
    "<dialog>{verb}</dialog>",
    '<p style="font-size:14px" style="font-size:0">{verb}</p>',
    '<p class="ordinary" class="gmail_quote">{verb}</p>',
    '<p hidden="" hidden="false">{verb}</p>',
    '<p id="ordinary" id="divRplyFwdMsg">{verb}</p>',
))
def test_ambiguous_html_holds_calendar_and_oneoff_payment(
        monkeypatch, tmp_path, calendar, wrapper):
    h = _Handler("therapy" if calendar else "oneoff", label="Example", amount=60,
                 calendar=calendar)
    item = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0]
    _arm_email(monkeypatch)
    html = (f"<body>{wrapper.format(verb='ATTENDED' if calendar else 'APPROVE')}"
            f"<blockquote>{subject_marker(item.token)}</blockquote></body>")
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        item.token, auth_by_id={"m1": [_GOOGLE_PASS]},
        body_by_id={"m1": {"body_html": html}}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_read_failed"


@pytest.mark.parametrize("calendar", (True, False))
@pytest.mark.parametrize("tail", (
    "I withdraw", "Name\nTitle\nLocation\nPhone\nExtra",
    "Please approve later", "Review PR #7", "Version v1.2.3",
    "From: old request\n> quoted\nI changed my mind",
))
def test_ambiguous_plain_tail_holds_payment(monkeypatch, tmp_path, calendar, tail):
    h = _Handler("therapy" if calendar else "oneoff", label="Example", amount=60,
                 calendar=calendar)
    item = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0]
    _arm_email(monkeypatch)
    separator = "\n" if tail.startswith("From:") else "\n-- \n"
    body = ("ATTENDED" if calendar else "APPROVE") + separator + tail
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        item.token, auth_by_id={"m1": [_GOOGLE_PASS]},
        body_by_id={"m1": {"body_text": body}}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code in {"consent_reply_read_failed", "consent_reply_unrecognized"}


@pytest.mark.parametrize("calendar", (True, False))
def test_benign_signature_does_not_approve_payment(monkeypatch, tmp_path, calendar):
    h = _Handler("therapy" if calendar else "oneoff", label="Example", amount=60,
                 calendar=calendar)
    item = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0]
    _arm_email(monkeypatch)
    body = ("ATTENDED" if calendar else "APPROVE") + "\n-- \nExample Name\nEngineer"
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        item.token, auth_by_id={"m1": [_GOOGLE_PASS]},
        body_by_id={"m1": {"body_text": body}}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_unrecognized"


@pytest.mark.parametrize("calendar", (True, False))
@pytest.mark.parametrize("tail", (
    "Subject: I rescind my consent", "From: I rescind my consent",
    "Date: I rescind my consent", "-- \nI rescind my consent",
    "--\nI rescind my consent",
    "-----Original Message-----\nI rescind my consent",
    "From: Example\nSent: Tuesday\nTo: Operator\nSubject: Request\nI rescind my consent",
))
def test_authored_withdrawal_holds_payment(monkeypatch, tmp_path, calendar, tail):
    h = _Handler("therapy" if calendar else "oneoff", label="Example", amount=60,
                 calendar=calendar)
    item = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0]
    _arm_email(monkeypatch)
    body = ("ATTENDED" if calendar else "APPROVE") + "\n" + tail
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        item.token, auth_by_id={"m1": [_GOOGLE_PASS]},
        body_by_id={"m1": {"body_text": body}}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_unrecognized"


@pytest.mark.parametrize("calendar", (True, False))
@pytest.mark.parametrize("quote", (
    "On Tuesday Example wrote:\n> Pending payment(s) — reply to approve or skip. No payment moves until you reply.",
    "-----Original Message-----\nFrom: Example\nSent: Tuesday\nTo: Operator\nSubject: Request\n> Pending payment(s) — reply to approve or skip. No payment moves until you reply.",
))
def test_proven_bottom_quote_can_approve_payment(monkeypatch, tmp_path, calendar, quote):
    h = _Handler("therapy" if calendar else "oneoff", label="Example", amount=60,
                 calendar=calendar)
    item = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0]
    _arm_email(monkeypatch)
    body = ("ATTENDED" if calendar else "APPROVE") + "\n" + quote
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        item.token, auth_by_id={"m1": [_GOOGLE_PASS]},
        body_by_id={"m1": {"body_text": body}}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == {consent_email.match_key(item)}


@pytest.mark.parametrize("batch", (False, True))
@pytest.mark.parametrize("shape,approved,unreadable", (
    ("unquoted_tail", False, True),
    ("quoted_tail", True, False),
    ("unseparated_headers", False, False),
    ("plain_approves", False, True),
    ("html_approves", False, True),
    ("matching", True, False),
    ("plain_only", True, False),
    ("html_only", True, False),
))
def test_consent_parts_and_quote_boundaries(
        monkeypatch, tmp_path, batch, shape, approved, unreadable):
    handlers = [_Handler("session", label="Example", amount=60)]
    if batch:
        handlers.append(_Handler("invoice", label="Invoice", amount=25, calendar=False))
    triggered = [(handler, [{}]) for handler in handlers]
    items = consent_email.build_pending_items(triggered, "2026-09-22")
    lines = [
        f"{'ATTENDED' if item.is_calendar else 'APPROVE'} {subject_marker(item.token)}"
        for item in items
    ]
    affirmative = "\n".join(lines)
    headers = "From: Example\nSent: Tuesday\nTo: Operator\nSubject: Request"
    payload = {"body_text": affirmative}
    if shape == "unquoted_tail":
        payload["body_text"] += f"\n-----Original Message-----\n{headers}\nI withdraw"
    elif shape == "quoted_tail":
        payload["body_text"] += (f"\n-----Original Message-----\n{headers}\n"
                                 "> Pending payment(s) — reply to approve or skip. No payment moves until you reply.")
    elif shape == "unseparated_headers":
        payload["body_text"] += (f"\n{headers}\n"
                                 "> Pending payment(s) — reply to approve or skip. No payment moves until you reply.")
    elif shape == "plain_approves":
        payload["body_html"] = "<p>hold</p>"
    elif shape == "html_approves":
        payload["body_text"] = "hold"
        payload["body_html"] = "<p>" + "</p><p>".join(lines) + "</p>"
    elif shape in {"matching", "html_only"}:
        payload["body_html"] = "<p>" + "</p><p>".join(lines) + "</p>"
        if shape == "html_only":
            del payload["body_text"]
    sends = []
    _arm_email(monkeypatch)
    monkeypatch.setattr(consent_email, "send_request", lambda *args: sends.append(args) or True)
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        items[0].token, auth_by_id={"m1": [_GOOGLE_PASS]}, body_by_id={"m1": payload}))
    result = consent_email.request_and_collect(
        triggered, "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == ({consent_email.match_key(item) for item in items} if approved else set())
    if unreadable:
        assert result.reason_code == "consent_reply_read_failed"
        assert len(sends) == 2
        assert "No payment was made" in sends[1][1]
        assert "Only a reply to this new request" in sends[1][1]
        fresh = consent_email.build_pending_items(triggered, "2026-09-22", 2)
        for item in fresh:
            if len(fresh) > 1:
                assert consent_email.accepted_reply_form(item) in sends[1][1]
            else:
                assert subject_marker(item.token) in sends[1][0]
    elif shape == "unseparated_headers":
        assert result.reason_code == "consent_reply_unrecognized"


@pytest.mark.parametrize("batch", (False, True))
@pytest.mark.parametrize("shape", (
    "before", "after_body", "after_html", "between",
    "empty_html", "whitespace_html", "markup_empty_html",
    "body_background", "table_background", "td_background", "unknown_class",
))
def test_unaccounted_or_empty_html_holds_payment_consent(monkeypatch, tmp_path, batch, shape):
    handlers = [_Handler("session", label="Example", amount=60)]
    if batch:
        handlers.append(_Handler("invoice", label="Invoice", amount=25, calendar=False))
    triggered = [(handler, [{}]) for handler in handlers]
    items = consent_email.build_pending_items(triggered, "2026-09-22")
    affirmative = "\n".join(
        f"{'ATTENDED' if item.is_calendar else 'APPROVE'} {subject_marker(item.token)}"
        for item in items)
    html_lines = "".join(f"<p>{line}</p>" for line in affirmative.splitlines())
    token = subject_marker(items[0].token)
    html = {
        "before": f"I withdraw<html><body>{html_lines}</body></html>",
        "after_body": f"<html><body>{html_lines}</body>I withdraw</html>",
        "after_html": f"<html><body>{html_lines}</body></html>I withdraw",
        "between": f"<p>{affirmative.splitlines()[0]}</p>I withdraw<blockquote>{token}</blockquote>",
        "empty_html": "",
        "whitespace_html": " \n ",
        "markup_empty_html": "<p></p>",
        "body_background": f"<body background='image.png'>{html_lines}</body>",
        "table_background": f"<table background='image.png'><tr><td>{html_lines}</td></tr></table>",
        "td_background": f"<table><tr><td background='image.png'>{html_lines}</td></tr></table>",
        "unknown_class": f"<p class='ordinary'>{affirmative}</p>",
    }[shape]
    payload = {"body_text": affirmative, "body_html": html}
    _arm_email(monkeypatch)
    sends = []
    monkeypatch.setattr(consent_email, "send_request", lambda *args: sends.append(args) or True)
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        items[0].token, auth_by_id={"m1": [_GOOGLE_PASS]}, body_by_id={"m1": payload}))
    result = consent_email.request_and_collect(
        triggered, "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_read_failed"
    assert all(result.states[consent_email.match_key(item)] == "blocked" for item in items)
    assert len(sends) == 2
    assert "Only a reply to this new request" in sends[1][1]


@pytest.mark.parametrize("calendar", (True, False))
@pytest.mark.parametrize("markup", (
    "<p style='font-family:Wingdings'>{verb}</p>",
    "<font face='Symbol'>{verb}</font>",
    "<p style='font:14px Webdings'>{verb}</p>",
    "<p style='font-variant:small-caps'>{verb}</p>",
    "<p style='font-feature-settings:normal'>{verb}</p>",
    "<p style='font-synthesis:none'>{verb}</p>",
    "<p style='unicode-range:U+0-FFFF'>{verb}</p>",
    "<p style='text-transform:uppercase'>{verb}</p>",
    "<p style='direction:rtl'>{verb}</p>",
    "<p style='unicode-bidi:bidi-override'>{verb}</p>",
    "<p style='writing-mode:vertical-rl'>{verb}</p>",
    "<p style='text-orientation:sideways'>{verb}</p>",
))
def test_unsafe_font_holds_payment(monkeypatch, tmp_path, calendar, markup):
    h = _Handler("therapy" if calendar else "oneoff", label="Example", amount=60,
                 calendar=calendar)
    item = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0]
    _arm_email(monkeypatch)
    verb = "ATTENDED" if calendar else "APPROVE"
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        item.token, auth_by_id={"m1": [_GOOGLE_PASS]},
        body_by_id={"m1": {"body_html": f"<body>{markup.format(verb=verb)}</body>"}}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_read_failed"


def test_unreadable_payment_reply_gets_fresh_request_and_no_payment(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Example", amount=60)
    item = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0]
    _arm_email(monkeypatch)
    sent = []
    monkeypatch.setattr(consent_email, "send_request", lambda *args: sent.append(args) or True)
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        item.token, auth_by_id={"m1": [_GOOGLE_PASS]},
        body_by_id={"m1": {"body_text": "ATTENDED", "body_html": "<p>SKIP</p>"}}))
    for _ in range(2):
        result = consent_email.request_and_collect(
            [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
        assert result.approved == set()
    assert len(sent) == 2  # the second poll reads only the fresh token
    assert "previous consent request could not be read" in sent[1][1]
    assert "Only a reply to this new request" in sent[1][1]


def test_unreadable_reply_holds_old_sweep_and_fresh_token_can_approve(monkeypatch, tmp_path):
    h = _Handler("therapy", label="Example", amount=60)
    triggered = [(h, [{}])]
    old = consent_email.build_pending_items(triggered, "2026-09-22", 1)[0]
    fresh = consent_email.build_pending_items(triggered, "2026-09-22", 2)[0]
    _arm_email(monkeypatch)
    sends = []
    monkeypatch.setattr(consent_email, "send_request", lambda *a: sends.append(a) or True)
    monkeypatch.setattr(_ec, "_fetch_auth_results", lambda _mid: [_GOOGLE_PASS])

    def fake_gws(args, timeout=45):
        if "+triage" in args:
            query = args[args.index("--query") + 1]
            mids = ["old-unreadable", "old-valid"] if old.token in query else ["fresh-valid"]
            return {"complete": True, "messages": [
                {"id": mid, "subject": f"RE: [ATELES] {subject_marker(old.token if mid.startswith('old') else fresh.token)}",
                 "from": "Op <op@example.com>"} for mid in mids]}
        if "+read" in args:
            mid = args[args.index("--id") + 1]
            if mid == "old-unreadable":
                return {"body_text": "ATTENDED", "body_html": "<p>SKIP</p>"}
            item = old if mid == "old-valid" else fresh
            return {"body_text": f"ATTENDED {subject_marker(item.token)}"}
        raise AssertionError(args)

    monkeypatch.setattr(_ec, "gws_json", fake_gws)
    first = consent_email.request_and_collect(triggered, "2026-09-22",
                                               state_path=tmp_path / "m.json")
    assert first.reason_code == "consent_reply_read_failed"
    assert first.approved == set()
    assert h.execute_calls == []
    assert len(sends) == 2
    assert "request remains held" in sends[1][1]
    assert "Only a reply to this new request" in sends[1][1]
    assert subject_marker(fresh.token) in sends[1][0]

    second = consent_email.request_and_collect(triggered, "2026-09-22",
                                                state_path=tmp_path / "m.json")
    assert second.approved == {fresh.token}
    assert old.token not in second.approved
    assert len(sends) == 2


@pytest.mark.parametrize("format", ("plain", "html"))
def test_consent_bottom_posted_reply_holds(monkeypatch, tmp_path, format):
    h = _Handler("therapy", label="Studio Example", amount=60)
    tok = consent_email.build_pending_items([(h, [{}])], "2026-09-22")[0].token
    _arm_email(monkeypatch)
    body = ({"body_text": f"> {subject_marker(tok)}\nATTENDED"} if format == "plain" else
            {"body_html": f"<body><blockquote>{subject_marker(tok)}</blockquote><p>ATTENDED</p></body>"})
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok, auth_by_id={"m1": [_GOOGLE_PASS]}, body_by_id={"m1": body}))
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_read_failed"


@pytest.mark.parametrize("calendar", (True, False))
@pytest.mark.parametrize("invalid_first", (True, False))
def test_consent_reply_invalid_verb_holds_whole_reply(calendar, invalid_first):
    h = _Handler("payment", label="Example", amount=60, calendar=calendar)
    items = consent_email.build_pending_items([(h, [{}])], "2026-09-22")
    tok = items[0].token
    valid = "ATTENDED" if calendar else "APPROVE"
    invalid = "APPROVE" if calendar else "ATTENDED"
    lines = [invalid, valid] if invalid_first else [valid, invalid]
    text = f"Re: Consent {subject_marker(tok)}\n" + "\n".join(lines)
    states, flagged = consent_email.overlay_verdicts(items, [text])
    assert flagged is True
    assert states[consent_email.match_key(items[0])] == "awaiting_approval"


@pytest.mark.parametrize("batch,invalid", (
    (False, "HOLD"), (False, "I withdraw approval"), (False, "SKIP"),
    (True, "HOLD"), (True, "I withdraw approval"),
))
@pytest.mark.parametrize("invalid_first", (False, True))
def test_earlier_approval_cannot_survive_later_hold_or_withdrawal(
        monkeypatch, tmp_path, batch, invalid, invalid_first):
    handlers = [_Handler("session", label="Example", amount=60)]
    if batch:
        handlers.append(_Handler("invoice", label="Invoice", amount=25, calendar=False))
    _email_env(monkeypatch)
    _install(monkeypatch, handlers, tmp_path)
    _quiet_main(monkeypatch)
    monkeypatch.setattr(consent_email, "send_request", lambda *args: True)

    def read(tokens, **kwargs):
        approvals = [
            f"RE: {subject_marker(token)}\n"
            f"{'ATTENDED' if index == 0 else 'APPROVE'} {subject_marker(token)}"
            for index, token in enumerate(tokens)
        ]
        other = f"RE: {subject_marker(tokens[0])}\n{invalid} {subject_marker(tokens[0])}"
        texts = ([other, *approvals] if invalid_first else [*approvals, other])
        return ReadRepliesOutcome(kind="ok", texts=texts)

    monkeypatch.setattr(consent_email, "read_replies_with_status", read)
    for _ in range(2):
        monedula.main()
        assert handlers[0].execute_calls == []
    if batch:
        assert len(handlers[1].execute_calls) == 1


@pytest.mark.parametrize("invalid_first", (True, False))
@pytest.mark.parametrize("named", ("one", "several", "zero"))
def test_malformed_line_holds_every_named_obligation_before_execution(
        monkeypatch, tmp_path, invalid_first, named):
    handlers = [_Handler("invoice_a", label="A", amount=25, calendar=False),
                _Handler("invoice_b", label="B", amount=30, calendar=False)]
    _email_env(monkeypatch)
    _install(monkeypatch, handlers, tmp_path)
    _quiet_main(monkeypatch)
    monkeypatch.setattr(consent_email, "send_request", lambda *args: True)

    def read(tokens, **kwargs):
        affected = tokens[:1] if named == "one" else tokens
        approvals = [f"RE: {subject_marker(tok)}\nAPPROVE {subject_marker(tok)}"
                     for tok in affected]
        markers = " ".join(subject_marker(tok) for tok in
                           (tokens[:1] if named == "one" else
                            tokens if named == "several" else []))
        invalid = f"RE: {subject_marker(tokens[0])}\nHOLD {markers}".strip()
        texts = [invalid, *approvals] if invalid_first else [*approvals, invalid]
        return ReadRepliesOutcome(kind="ok", texts=texts)

    monkeypatch.setattr(consent_email, "read_replies_with_status", read)
    result = consent_email.request_and_collect(
        [(h, h.matches([{}])) for h in handlers], "2026-09-22",
        state_path=tmp_path / "probe.json")
    assert result.approved == set()
    monedula.main()
    assert all(h.execute_calls == [] for h in handlers)


@pytest.mark.parametrize("fault", ("continuation", "invalid_envelope", "invalid_row",
                                   "empty_plain"))
def test_real_reader_bad_collection_or_part_never_executes(monkeypatch, tmp_path, fault):
    h = _Handler("session", label="Example", amount=60)
    _email_env(monkeypatch)
    _install(monkeypatch, [h], tmp_path)
    _quiet_main(monkeypatch)
    monkeypatch.setattr(consent_email, "send_request", lambda *args: True)
    monkeypatch.setattr(_ec, "_gws", lambda: "/synthetic/gws")

    def fake(args, timeout=45):
        if "+triage" in args:
            tok = args[args.index("--query") + 1].split()[-1]
            row = {"id": "m1", "subject": f"RE: {subject_marker(tok)}",
                   "from": "operator@example.com"}
            if fault == "invalid_envelope":
                return {"wrong": []}
            if fault == "invalid_row":
                row["id"] = []
            result = {"complete": True, "messages": [row]}
            if fault == "continuation":
                result["nextPageToken"] = "more"
            return result
        if args[:4] == ["gmail", "users", "messages", "get"]:
            return {"payload": {"headers": [
                {"name": "Authentication-Results", "value": _GOOGLE_PASS}]}}
        if "+read" in args:
            return ({"body_text": "", "body_html": "<p>ATTENDED</p>"}
                    if fault == "empty_plain" else {"body_text": "ATTENDED"})
        raise AssertionError(args)

    monkeypatch.setattr(_ec, "gws_json", fake)
    result = consent_email.request_and_collect(
        [(h, h.matches([{}]))], "2026-09-22", state_path=tmp_path / "probe.json")
    assert result.approved == set()
    assert result.reason_code == "consent_reply_read_failed"
    monedula.main()
    assert h.execute_calls == []


@pytest.mark.parametrize("batch", (False, True))
@pytest.mark.parametrize("shape", ("real_object", "array", "results"))
@pytest.mark.parametrize("fault", ("none", "full_page", "invalid_row"))
def test_cli_triage_contract_controls_payment_execution(
        monkeypatch, tmp_path, batch, shape, fault):
    handlers = [_Handler("session_a", label="Synthetic A", amount=60)]
    if batch:
        handlers.append(_Handler("session_b", label="Synthetic B", amount=40))
    _email_env(monkeypatch)
    _install(monkeypatch, handlers, tmp_path)
    _quiet_main(monkeypatch)
    monkeypatch.setattr(consent_email, "send_request", lambda *args: True)
    monkeypatch.setattr(_ec, "_gws", lambda: "/synthetic/gws")
    monkeypatch.setattr(monedula, "_yesterday", lambda: date(2026, 9, 22))

    def fake(args, timeout=45):
        if "+triage" in args:
            row = {"date": "2026-11-18T10:00:00Z", "from": "Op <operator@example.com>",
                   "id": "synthetic-reply", "subject": "RE: synthetic consent"}
            return triage_contract_response(shape, fault, row, 40)
        if args[:4] == ["gmail", "users", "messages", "get"]:
            return {"id": "synthetic-reply", "payload": {"headers": [
                {"name": "Authentication-Results", "value": _GOOGLE_PASS}]}}
        if "+read" in args:
            items = consent_email.build_pending_items(
                [(h, h.matches([{}])) for h in handlers], "2026-09-22", 1)
            return {"body_text": "\n".join(
                f"ATTENDED {subject_marker(item.token)}" for item in items)}
        raise AssertionError(args)

    monkeypatch.setattr(_ec, "gws_json", fake)
    triggered = [(h, h.matches([{}])) for h in handlers]
    result = consent_email.request_and_collect(
        triggered, "2026-09-22", state_path=tmp_path / "consent.json")
    assert len(result.approved) == (len(handlers) if fault == "none" else 0)
    if fault != "none":
        assert result.reason_code == "consent_reply_read_failed"
    monedula.main()
    assert all(len(h.execute_calls) == (1 if fault == "none" else 0) for h in handlers)


def test_consent_multi_item_reply_is_atomic_and_valid_reply_approves_both():
    calendar = _Handler("session", label="Studio", amount=60)
    oneoff = _Handler("invoice", label="Vendor", amount=40, calendar=False)
    items = consent_email.build_pending_items(
        [(calendar, [{}]), (oneoff, [{}])], "2026-09-22")
    first, second = items
    good = (f"ATTENDED {subject_marker(first.token)}\n"
            f"APPROVE {subject_marker(second.token)}")
    states, flagged = consent_email.overlay_verdicts(items, [good])
    assert not flagged
    assert all(state == "approved" for state in states.values())
    bad = (f"ATTENDED {subject_marker(first.token)}\n"
           f"ATTENDED {subject_marker(second.token)}")
    states, flagged = consent_email.overlay_verdicts(items, [bad])
    assert flagged
    assert all(state == "awaiting_approval" for state in states.values())


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


@pytest.mark.parametrize("failure", ("metadata", "body_missing", "body_empty",
                                     "snippet_only", "preview_only", "html_empty"))
def test_real_read_partial_failure_holds_every_payment(monkeypatch, tmp_path, failure):
    """m1 (authenticated ATTENDED) loads; m2 fails. Monedula must hold, not act
    on the half it could read."""
    h = _Handler("therapy", label="Studio Example", amount=60)
    items = consent_email.build_pending_items([(h, [{}])], "2026-09-22")
    tok = items[0].token
    key = consent_email.match_key(items[0])
    _arm_email(monkeypatch)
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok,
        auth_by_id={"m1": [_GOOGLE_PASS],
                    "m2": None if failure == "metadata" else [_GOOGLE_PASS]},
        body_by_id={"m1": "ATTENDED",
                    "m2": ({"snippet": "ATTENDED", "subject": "ATTENDED"}
                           if failure == "snippet_only" else
                           {"preview": "ATTENDED"} if failure == "preview_only" else
                           {"body_html": "<style>ATTENDED</style>"}
                           if failure == "html_empty" else
                           {} if failure == "body_empty" else None)}))
    outcome = _ec.read_replies_with_status([tok])
    assert outcome.kind == "transport_error"
    assert outcome.texts == []
    assert [reply.message_id for reply in outcome.replies] == ["m1"]
    assert [r.message_id for r in outcome.unreadable_replies] == ["m2"]
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


# ── ux review (PR #1202): swarm-mailbox dependency + unauthenticated replies ─


@pytest.mark.parametrize(
    "setup",
    ["unset", "same_as_operator", "no_config_dir"],
)
def test_no_separate_swarm_mailbox_holds_with_explicit_blocker_and_sends_nothing(
    monkeypatch, tmp_path, setup
):
    """Request and replies in the operator's own mailbox can never produce an
    authenticated reply, so no request is sent and consent is held with the
    explicit blocker — not a request pretending to be actionable."""
    if setup == "unset":
        monkeypatch.delenv("ATELES_SWARM_EMAIL", raising=False)
        monkeypatch.delenv("ATELES_SWARM_GWS_CONFIG_DIR", raising=False)
    elif setup == "same_as_operator":
        monkeypatch.setenv("ATELES_SWARM_EMAIL", "Op <OPERATOR@example.com>")
    else:
        monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path / "missing"))
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
    sends, reads = [], []
    monkeypatch.setattr(
        consent_email, "send_request", lambda *a, **k: sends.append(a) or True
    )
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: reads.append(a) or ReadRepliesOutcome(kind="ok", texts=[]),
    )
    h = _Handler("therapy", label="Studio Example", amount=60)
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    tok = _key_for(h, {}, "2026-09-22")
    assert result.reason_code == consent_email.REASON_NEEDS_SWARM_MAILBOX
    assert result.states[tok] == "blocked"
    assert result.approved == set() and result.awaiting == set()
    assert result.channel_ok is False
    assert sends == [] and reads == []
    assert not (tmp_path / "m.json").exists()


def test_no_swarm_mailbox_main_surfaces_one_deduped_blocker_and_pays_nothing(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("ATELES_SWARM_EMAIL", raising=False)
    monkeypatch.delenv("ATELES_SWARM_GWS_CONFIG_DIR", raising=False)
    _email_env(monkeypatch)
    h = _Handler("therapy", label="Studio Example", amount=60)
    _install(monkeypatch, [h], tmp_path)
    sends = []
    monkeypatch.setattr(
        consent_email, "send_request", lambda *a, **k: sends.append(a) or True
    )
    notify_calls = _quiet_main(monkeypatch)
    for _ in range(2):
        assert monedula.main() is False
    assert h.execute_calls == []
    assert sends == []
    blockers = [c for c in notify_calls if c[1] == "blocker"]
    assert blockers, "the blocker must reach the operator"
    keys = {c[2].get("dedupe_key") for c in blockers}
    assert keys == {"monedula:consent_email_needs_swarm_mailbox"}
    assert all("ateles#1221" in c[0] for c in blockers)
    assert all("separate swarm mailbox" in c[0] for c in blockers)


def _unauth_read(calls: list):
    """An operator-address reply that fails authentication, on every sweep."""

    def fake_read(tokens, on_sender_rejected=None,
                  on_unauthenticated_operator_reply=None, **k):
        calls.append(1)
        assert on_unauthenticated_operator_reply is not None, (
            "consent must distinguish an unauthenticated operator reply from "
            "a non-operator sender"
        )
        on_unauthenticated_operator_reply()
        return ReadRepliesOutcome(kind="ok", texts=[])

    return fake_read


def test_unauthenticated_operator_reply_notifies_once_per_request_and_holds(
    monkeypatch, tmp_path
):
    h = _Handler("therapy", label="Studio Example", amount=60)
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(consent_email, "read_replies_with_status", _unauth_read([]))
    notices: list = []
    for _ in range(3):
        result = consent_email.request_and_collect(
            [(h, [{}])],
            "2026-09-22",
            state_path=tmp_path / "m.json",
            on_unauthenticated_reply=lambda msg, key: notices.append((msg, key)),
        )
        tok = _key_for(h, {}, "2026-09-22")
        assert result.approved == set()
        assert result.states[tok] == "awaiting_approval"
        assert result.reason_code == consent_email.REASON_REPLY_UNAUTHENTICATED
        assert result.unauthenticated_replies == 1
    assert len(notices) == 1, "deduped: one notice per consent request"
    msg, key = notices[0]
    assert "could not be authenticated" in msg
    assert "no payment was made" in msg
    assert "ATELES_SWARM_GWS_CONFIG_DIR" in msg
    assert "operator@example.com" not in msg and "60" not in msg
    assert key.startswith("monedula:consent_reply_unauthenticated:")


def test_unauthenticated_reply_notifies_again_for_a_new_request(monkeypatch, tmp_path):
    h1 = _Handler("therapy", label="Studio Example", amount=60)
    h2 = _Handler("yoga", label="Yoga Example", amount=60)
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(consent_email, "read_replies_with_status", _unauth_read([]))
    notices: list = []
    cb = lambda msg, key: notices.append(key)  # noqa: E731
    path = tmp_path / "m.json"
    consent_email.request_and_collect([(h1, [{}])], "2026-09-22", state_path=path,
                                      on_unauthenticated_reply=cb)
    consent_email.request_and_collect([(h1, [{}]), (h2, [{}])], "2026-09-22",
                                      state_path=path, on_unauthenticated_reply=cb)
    assert len(notices) == 2 and notices[0] != notices[1]


def test_unauthenticated_reply_main_notifies_once_records_and_pays_nothing(
    monkeypatch, tmp_path
):
    _email_env(monkeypatch)
    h = _Handler("therapy", label="Studio Example", amount=60)
    _install(monkeypatch, [h], tmp_path)
    monkeypatch.setattr(consent_email, "send_request", lambda *a, **k: True)
    monkeypatch.setattr(consent_email, "read_replies_with_status", _unauth_read([]))
    notify_calls = _quiet_main(monkeypatch)
    escalations: list = []
    monkeypatch.setattr(
        monedula,
        "_post_escalation_entity",
        lambda entity, key: escalations.append((entity, key)) or True,
    )
    for _ in range(3):
        monedula.main()
    assert h.execute_calls == []
    unauth = [c for c in notify_calls if "could not be authenticated" in c[0]]
    assert len(unauth) == 1
    assert unauth[0][1] == "blocker"
    assert unauth[0][2]["dedupe_key"].startswith(
        "monedula:consent_reply_unauthenticated:"
    )
    recorded = [e for e, _ in escalations
                if "consent_reply_unauthenticated" in e.get("tags", [])]
    assert len(recorded) == 1


def test_real_read_unauthenticated_operator_reply_fires_distinct_signal(
    monkeypatch, tmp_path
):
    """End to end through lib.approval: an operator-address reply without
    authentication evidence is reported as unauthenticated, not as a
    non-operator sender, and still approves nothing."""
    h = _Handler("therapy", label="Studio Example", amount=60)
    items = consent_email.build_pending_items([(h, [{}])], "2026-09-22")
    tok = items[0].token
    key = consent_email.match_key(items[0])
    _arm_email(monkeypatch)
    monkeypatch.setattr(_ec, "gws_json", _fake_gws(
        tok, auth_by_id={"m1": []}, body_by_id={"m1": "ATTENDED"}))
    notices: list = []
    result = consent_email.request_and_collect(
        [(h, [{}])], "2026-09-22", state_path=tmp_path / "m.json",
        on_unauthenticated_reply=lambda msg, k: notices.append(k))
    assert key not in result.approved
    assert result.reason_code == consent_email.REASON_REPLY_UNAUTHENTICATED
    assert len(notices) == 1


# ── ux review (PR #1202 @ 33039cb2): correction email copy ───────────────────


def test_correction_email_quotes_line_says_nothing_paid_and_gives_per_item_forms(
    monkeypatch, tmp_path
):
    cal = _Handler("therapy", label="Studio Example", amount=60, calendar=True)
    one = _Handler("invoice", label="Invoice Example", amount=25, calendar=False)
    sends: list = []
    monkeypatch.setattr(
        consent_email, "send_request", lambda *a, **k: sends.append(a) or True
    )
    monkeypatch.setattr(
        consent_email,
        "read_replies_with_status",
        lambda *a, **k: ReadRepliesOutcome(
            kind="ok", texts=["RE: consent\nATTENDED both please\n"]
        ),
    )
    result = consent_email.request_and_collect(
        [(cal, [{}]), (one, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert result.reason_code == "consent_reply_unrecognized"
    assert result.approved == set()
    corrections = [s for s in sends if "correction" in s[0]]
    assert len(corrections) == 1
    subject, body = corrections[0][0], corrections[0][1]
    assert "not understood" in subject
    # Operator-addressed, complete sentence — not an internal fragment.
    assert body.startswith("Your reply to the Monedula payment consent request")
    assert "no payment was made" in body
    assert "> ATTENDED both please" in body
    by_name = {i.handler_name: i for i in result.pending_items}
    cal_marker = subject_marker(by_name["therapy"].token)
    one_marker = subject_marker(by_name["invoice"].token)
    assert f"ATTENDED {cal_marker}" in body
    assert f"SKIP {cal_marker}" in body
    assert f"APPROVE {one_marker}" in body
    assert f"SKIP {one_marker}" in body
    assert f"APPROVE {cal_marker}" not in body, "calendar items need ATTENDED"
    assert "identify the unrecognized line" not in body

    # Deduped: a repeat sweep of the same request sends no second correction.
    consent_email.request_and_collect(
        [(cal, [{}]), (one, [{}])], "2026-09-22", state_path=tmp_path / "m.json"
    )
    assert len([s for s in sends if "correction" in s[0]]) == 1


# ── Swarm mailbox routing (ateles#1221 consent half, folded into PR #1202) ───


def test_calendar_read_keeps_the_operator_default_gws_config(monkeypatch, tmp_path):
    """Only consent runs as the swarm mailbox; the calendar is the operator's."""
    import os as _os
    import subprocess as _sp

    swarm_dir = _os.environ["ATELES_SWARM_GWS_CONFIG_DIR"]
    monkeypatch.delenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/gws")
    seen: list = []

    def fake_run(cmd, **kw):
        seen.append((list(cmd), kw.get("env")))
        return _sp.CompletedProcess(cmd, 0, stdout='{"items": []}', stderr="")

    monkeypatch.setattr(monedula.subprocess, "run", fake_run)
    assert monedula.fetch_yesterday_events() == []
    assert seen
    assert seen[0][0][1:4] == ["calendar", "calendarList", "list"]
    assert any(c[1:4] == ["calendar", "events", "list"] for c, _ in seen)
    for _argv, run_env in seen:
        assert (run_env or {}).get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR") != swarm_dir
        assert "GOOGLE_WORKSPACE_CLI_CONFIG_DIR" not in (run_env or {})


def test_consent_round_trip_through_the_swarm_mailbox_pays_once(monkeypatch, tmp_path):
    """Effect test at the gws subprocess boundary: the request goes From the
    swarm To the operator as the swarm mailbox; the operator's authenticated
    reply arrives in the swarm mailbox; that reply is what pays — once."""
    import json as _j
    import os as _os
    import subprocess as _sp

    swarm_dir = _os.environ["ATELES_SWARM_GWS_CONFIG_DIR"]
    _email_env(monkeypatch)
    h = _Handler("therapy", label="Studio Example", amount=60, calendar=True)
    match = {"trigger": "calendar", "handler": "therapy"}
    # Calendar-gated: triggers only while yesterday's events are fetched.
    h._match_fn = lambda events: [match] if events else []
    _install(monkeypatch, [h], tmp_path)
    _quiet_main(monkeypatch)
    # Resolve only gws, so nothing else on the host (activity reporter) runs.
    monkeypatch.setattr(
        _ec.shutil, "which", lambda name: "/bin/gws" if name == "gws" else None
    )

    calls: list = []
    sent_subjects: list = []
    auth = ("mx.google.com; dkim=pass header.i=@example.com header.s=s; "
            "dmarc=pass (p=NONE) header.from=example.com")

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw.get("env")))
        args = list(cmd[1:])
        out: object = {}
        if args[:2] == ["gmail", "+" + "send"]:
            sent_subjects.append(args[args.index("--subject") + 1])
        elif "+triage" in args:
            out = {"complete": True, "messages": [
                {"id": "r1", "subject": f"RE: {s}", "from": "Op <operator@example.com>"}
                for s in sent_subjects
            ]}
        elif args[:4] == ["gmail", "users", "messages", "get"]:
            out = {"id": "r1", "payload": {"headers": [
                {"name": "Authentication-Results", "value": auth}]}}
        elif "+read" in args:
            out = {"body_text": "ATTENDED"}
        return _sp.CompletedProcess(cmd, 0, stdout=_j.dumps(out), stderr="")

    monkeypatch.setattr(_ec.subprocess, "run", fake_run)

    for _ in range(3):
        monedula.main()

    assert h.execute_calls == [match]
    sends = [(a, e) for a, e in calls if a[1:3] == ["gmail", "+" + "send"]]
    assert len(sends) == 1
    argv, env = sends[0]
    assert argv[argv.index("--from") + 1] == "swarm@example.net"
    assert argv[argv.index("--to") + 1] == "operator@example.com"
    assert calls, "consent made gws calls"
    assert all(e["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == swarm_dir for _, e in calls)


def test_unauthenticated_reply_in_swarm_mailbox_does_not_pay(monkeypatch, tmp_path):
    import json as _j
    import subprocess as _sp

    _email_env(monkeypatch)
    h = _Handler("therapy", label="Studio Example", amount=60, calendar=True)
    _install(monkeypatch, [h], tmp_path)
    _quiet_main(monkeypatch)
    # Resolve only gws, so nothing else on the host (activity reporter) runs.
    monkeypatch.setattr(
        _ec.shutil, "which", lambda name: "/bin/gws" if name == "gws" else None
    )
    sent: list = []

    def fake_run(cmd, **kw):
        args = list(cmd[1:])
        out: object = {}
        if args[:2] == ["gmail", "+" + "send"]:
            sent.append(args[args.index("--subject") + 1])
        elif "+triage" in args:
            out = {"complete": True, "messages": [{"id": "r1", "subject": f"RE: {s}",
                                 "from": "operator@example.com"} for s in sent]}
        elif args[:4] == ["gmail", "users", "messages", "get"]:
            out = {"id": "r1", "payload": {"headers": []}}
        elif "+read" in args:
            out = {"body_text": "ATTENDED"}
        return _sp.CompletedProcess(cmd, 0, stdout=_j.dumps(out), stderr="")

    monkeypatch.setattr(_ec.subprocess, "run", fake_run)
    for _ in range(2):
        monedula.main()
    assert h.execute_calls == []


def test_needs_swarm_mailbox_hint_names_the_missing_variable(monkeypatch):
    monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
    monkeypatch.delenv("ATELES_SWARM_GWS_CONFIG_DIR")
    hint = consent_email.needs_swarm_mailbox_hint()
    assert "ateles#1221" in hint
    assert "ATELES_SWARM_GWS_CONFIG_DIR" in hint
    assert "ATELES_SWARM_EMAIL" not in hint
    assert "payments held" in hint


# ── ux review (PR #1202 @ b20b4e3b): per-item verbs in the multi-item request ─


def test_multi_item_request_gives_each_item_its_accepted_form():
    """Mixed set (calendar + one-off): each item shows exactly its own form,
    identical to accepted_reply_form (so request and correction cannot drift),
    and a calendar item is never offered an APPROVE form."""
    cal = _Handler("therapy", label="Studio Example", amount=60, calendar=True)
    one = _Handler("invoice", label="Invoice Example", amount=25, calendar=False)
    items = consent_email.build_pending_items(
        [(cal, [{}]), (one, [{}])], "2026-09-22"
    )
    by_name = {i.handler_name: i for i in items}
    cal_item, one_item = by_name["therapy"], by_name["invoice"]
    cal_marker = subject_marker(cal_item.token)
    one_marker = subject_marker(one_item.token)

    _subj, body = consent_email.build_request_body(items, superseded=False)

    cal_form = consent_email.accepted_reply_form(cal_item)
    one_form = consent_email.accepted_reply_form(one_item)
    assert cal_form == f"ATTENDED {cal_marker}   or   SKIP {cal_marker}"
    assert one_form == f"APPROVE {one_marker}   or   SKIP {one_marker}"
    lines = body.splitlines()
    assert lines[lines.index("Studio Example (EUR 60):") + 1].strip() == cal_form
    assert lines[lines.index("Invoice Example (EUR 25):") + 1].strip() == one_form
    assert f"APPROVE {cal_marker}" not in body
    assert f"ATTENDED {one_marker}" not in body
    assert "calendar need ATTENDED" in body and "one-off payments need APPROVE" in body


@pytest.mark.parametrize("recovered", (False, True))
@pytest.mark.parametrize("format", ("plain", "html"))
def test_full_sent_payment_request_quote_approves(monkeypatch, tmp_path, recovered, format):
    handler = _Handler("session", label="Example", amount=60)
    triggered = [(handler, [{}])]
    _arm_email(monkeypatch)
    sent = []
    monkeypatch.setattr(consent_email, "send_request",
                        lambda subject, body: sent.append((subject, body)) or True)
    item = consent_email.build_pending_items(triggered, "2026-09-22")[0]
    calls = {"n": 0}

    def fake(args, timeout=45):
        if "+triage" in args:
            query = args[args.index("--query") + 1]
            active = item.token in query or (recovered and calls["n"] > 0 and
                                             "[Ateles] Monedula consent" in query)
            return {"complete": True, "messages": [{
                "id": "reply", "subject": f"RE: {sent[-1][0]}",
                "from": "Op <op@example.com>"}] if active else []}
        if "+read" in args:
            if recovered and calls["n"] == 0:
                calls["n"] += 1
                return {"body_text": "ATTENDED", "body_html": "<p>SKIP</p>"}
            original = sent[-1][1]
            marker = subject_marker(item.token)
            authored = f"ATTENDED {marker}" if recovered else "ATTENDED"
            if format == "plain":
                return {"body_text": authored + "\n" + "\n".join(
                    f"> {line}" for line in original.splitlines())}
            return {"body_html": (f"<p>{authored}</p><blockquote>" +
                                  "<br>".join(html.escape(line) for line in original.splitlines()) +
                                  "</blockquote>")}
        raise AssertionError(args)

    monkeypatch.setattr(_ec, "gws_json", fake)
    monkeypatch.setattr(_ec, "_fetch_auth_results", lambda _mid: [_GOOGLE_PASS])
    path = tmp_path / "m.json"
    if recovered:
        first = consent_email.request_and_collect(triggered, "2026-09-22", state_path=path)
        assert first.approved == set()
        assert len(sent) == 2
        assert "previous consent request" in sent[-1][1]
        item = consent_email.build_pending_items(triggered, "2026-09-22", 2)[0]
    result = consent_email.request_and_collect(triggered, "2026-09-22", state_path=path)
    assert result.approved == {item.token}


@pytest.mark.parametrize("batch", (False, True))
@pytest.mark.parametrize("correction,authenticated", (
    ("do not pay", True), ("ATTENDED", True), ("do not pay", False)))
def test_tokenless_payment_reply_holds_entire_request(
        monkeypatch, tmp_path, batch, correction, authenticated):
    handlers = [_Handler("session", label="Example", amount=60)]
    if batch:
        handlers.append(_Handler("invoice", label="Invoice", amount=25, calendar=False))
    triggered = [(handler, [{}]) for handler in handlers]
    items = consent_email.build_pending_items(triggered, "2026-09-22")
    _arm_email(monkeypatch)
    sent = []
    monkeypatch.setattr(consent_email, "send_request",
                        lambda subject, body: sent.append((subject, body)) or True)

    def fake(args, timeout=45):
        if "+triage" in args:
            query = args[args.index("--query") + 1]
            mids = (["m1"] if "subject:" not in query and items[0].token in query else
                    ["m1", "m2"] if "subject:" in query else [])
            return {"complete": True, "messages": [{
                "id": mid, "subject": f"RE: [Ateles] Monedula consent 2026-09-22"
                                      + (f" {subject_marker(items[0].token)}" if mid == "m1" else ""),
                "from": "op@example.com"} for mid in mids]}
        if "+read" in args:
            mid = args[args.index("--id") + 1]
            if mid == "m2":
                return {"body_text": correction}
            body = ("\n".join(f"ATTENDED {subject_marker(i.token)}" if i.is_calendar
                              else f"APPROVE {subject_marker(i.token)}" for i in items)
                    if batch else "ATTENDED")
            return {"body_text": body}
        raise AssertionError(args)

    monkeypatch.setattr(_ec, "gws_json", fake)
    monkeypatch.setattr(_ec, "_fetch_auth_results", lambda mid:
                        [_GOOGLE_PASS] if mid == "m1" or authenticated else [])
    notices = []
    result = consent_email.request_and_collect(
        triggered, "2026-09-22", state_path=tmp_path / "m.json",
        on_missing_token_reply=lambda msg, key: notices.append(msg))
    assert len(result.approved) == (0 if authenticated else len(items))
    assert not any("no payment was approved" in note for note in notices)
    if authenticated:
        assert len(result.blocked) == len(items)
        assert any("is held" in note for note in notices)


@pytest.mark.parametrize("batch", (False, True))
def test_new_bound_reply_can_follow_notified_tokenless_reply(monkeypatch, tmp_path, batch):
    handlers = [_Handler("session", label="Example", amount=60)]
    if batch:
        handlers.append(_Handler("invoice", label="Invoice", amount=25, calendar=False))
    triggered = [(handler, [{}]) for handler in handlers]
    items = consent_email.build_pending_items(triggered, "2026-09-22")
    _arm_email(monkeypatch)
    monkeypatch.setattr(consent_email, "send_request", lambda *_: True)
    sweep = {"number": 1}

    def fake(args, timeout=45):
        if "+triage" in args:
            query = args[args.index("--query") + 1]
            if "in:sent" in query:
                return {"complete": True, "messages": []}
            mids = (["old"] if "subject:" not in query else ["correction", "old"])
            if sweep["number"] == 2:
                mids.append("new")
            return {"complete": True, "messages": [{
                "id": mid, "subject": "RE: [Ateles] Monedula consent 2026-09-22",
                "from": "op@example.com"} for mid in mids]}
        if "+read" in args:
            mid = args[args.index("--id") + 1]
            if mid == "correction":
                return {"body_text": "hold"}
            return {"body_text": "\n".join(
                f"{'ATTENDED' if item.is_calendar else 'APPROVE'} {subject_marker(item.token)}"
                for item in items)}
        raise AssertionError(args)

    monkeypatch.setattr(_ec, "gws_json", fake)
    monkeypatch.setattr(_ec, "_fetch_auth_results", lambda _: [_GOOGLE_PASS])
    notices = []
    path = tmp_path / "m.json"
    first = consent_email.request_and_collect(
        triggered, "2026-09-22", state_path=path,
        on_missing_token_reply=lambda msg, key: notices.append(msg) or True)
    assert first.approved == set()
    assert len(first.blocked) == len(items)
    sweep["number"] = 2
    second = consent_email.request_and_collect(
        triggered, "2026-09-22", state_path=path,
        on_missing_token_reply=lambda msg, key: notices.append(msg) or True)
    assert second.approved == {item.token for item in items}
    assert len(notices) == 1
