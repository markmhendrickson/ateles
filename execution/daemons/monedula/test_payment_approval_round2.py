# ruff: noqa: F401,F811  (shared fixtures are imported, not redefined)
"""UX round 2: outcome alerts, held-payment alerts, idempotent re-raise.

Each behaviour here was seen red against 2983749e before the fix. Synthetic
data only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import consent_email  # noqa: E402
import payment_approval  # noqa: E402
import payment_journal  # noqa: E402
from test_payment_approval import FakeNeotoma, env  # noqa: E402
from test_payment_approval_authority import _approve, _kind, alerts  # noqa: E402
from test_session_payments import _event  # noqa: E402

IBAN_LIKE = "".join(["XX00", "TEST", "0000", "0000", "0000"])


def _paid_with(w, neo, result):
    w.handler.execute = lambda match: dict(result, handler="yoga")
    w.tick()
    _approve(neo)
    w.tick(minutes=15)


# ── Finding 1: the operator is told what happened to the money ──────────────


def test_a_sent_payment_is_confirmed_as_info(env, alerts):
    w, neo = env
    _paid_with(w, neo, {"status": "sent"})
    sent = _kind(alerts, "payment_outcome") or [a for a in alerts if "payment_outcome" in (a["key"] or "")]
    assert len(sent) == 1 and sent[0]["priority"] == "warn"
    assert "SENT" in sent[0]["message"] and "yoga" in sent[0]["message"]
    assert neo.only["payment_key"][:12] in sent[0]["message"]


@pytest.mark.parametrize(
    "result",
    [
        {"status": "manual_required", "error": "no token", "iban": IBAN_LIKE},
        {"status": "failed", "error": "rail error"},
        {},
    ],
)
def test_manual_required_or_failed_is_a_blocker_that_never_reads_as_paid(env, alerts, result):
    w, neo = env
    _paid_with(w, neo, result)
    out = [a for a in alerts if "payment_outcome" in (a["key"] or "")]
    assert len(out) == 1 and out[0]["priority"] == "blocker"
    text = out[0]["message"]
    assert "SENT" not in text and "paid" not in text.lower().replace("not paid", "")
    assert "NOT" in text
    assert IBAN_LIKE not in text, "no payee account detail in an alert"
    assert out[0]["key"].endswith(neo.only["payment_key"])


def test_a_payment_outcome_alert_is_not_repeated_on_later_ticks(env, alerts):
    w, neo = env
    _paid_with(w, neo, {"status": "sent"})
    w.tick(minutes=15)
    w.tick(minutes=15)
    assert len([a for a in alerts if "payment_outcome" in (a["key"] or "")]) == 1


# ── Finding 2: every hold alerts ─────────────────────────────────────────────


def test_a_failed_record_raise_alerts_per_payment_not_only_when_all_fail(env, alerts):
    w, neo = env
    w.calendars["primary"] = [_event("evt-1"), _event("evt-2")]
    k1 = consent_email.obligation_key(w.handler, "2026-11-11", "evt-1")
    neo.fail_store_for = {k1}
    w.tick()
    unavailable = _kind(alerts, "unavailable")
    assert len(unavailable) == 1
    assert unavailable[0]["priority"] == "blocker"
    assert unavailable[0]["key"].endswith(k1)
    assert k1[:12] in unavailable[0]["message"] and "yoga" in unavailable[0]["message"]
    # the other payment's record was raised normally
    assert len(_kind(alerts, "raised")) == 1


def test_a_terms_change_at_execute_time_alerts(env, alerts, monkeypatch):
    w, neo = env
    w.tick()
    _approve(neo)
    real = payment_approval.collect

    def collect_then_change_terms(*a, **k):
        result = real(*a, **k)
        w.handler.profile.amount_eur = 11  # live terms move after approval
        return result

    monkeypatch.setattr(payment_approval, "collect", collect_then_change_terms)
    w.tick(minutes=15)
    assert w.handler.execute_calls == []
    changed = [a for a in alerts if "payment_terms_changed" in (a["key"] or "")]
    assert len(changed) == 1 and changed[0]["priority"] == "blocker"
    assert "yoga" in changed[0]["message"]


def test_a_bad_env_wise_recipient_id_is_stranded_not_log_only(monkeypatch):
    from handlers import payment_profile
    from strandings import REASON_BAD_RECIPIENT_ID

    monkeypatch.setenv("MONEDULA_PROFILES", "TESTP")
    monkeypatch.setenv("TESTP_CALENDAR_KEYWORDS", "yoga")
    monkeypatch.setenv("TESTP_AMOUNT_EUR", "10")
    monkeypatch.setenv("TESTP_WISE_RECIPIENT_ID", "abc")
    strandings: list = []
    assert payment_profile.load_profiles(strandings) == []
    assert [s.reason for s in strandings] == [REASON_BAD_RECIPIENT_ID]


def test_an_undeliverable_alert_is_logged_at_error(monkeypatch, caplog):
    import monedula

    monkeypatch.setattr(monedula, "_notifier", None)
    with caplog.at_level("ERROR"):
        monedula._notify("monedula: something held", priority="blocker")
    assert any("could not be delivered" in r.getMessage() for r in caplog.records)


# ── Re-raise after a first write that landed but timed out ──────────────────


def test_a_timed_out_first_write_that_landed_is_not_a_held_payment(env, alerts):
    w, neo = env
    key = consent_email.obligation_key(w.handler, "2026-11-11", "evt-1")
    neo.timeout_after_store_for = {key}
    w.tick()
    # The record landed; the read-back finds it, so this is a raised request,
    # not an unavailable one.
    assert key in neo.rows
    assert not _kind(alerts, "unavailable")
    assert len(_kind(alerts, "raised")) == 1


def test_a_retry_after_an_invisible_landed_write_reads_back_not_conflicts(env, alerts):
    """The write landed and timed out, and the next lookup does not see it yet:
    the retry reuses the idempotency key with a new raised_at, which the server
    refuses. The retry must read back the record that is there, not hold."""
    w, neo = env
    key = consent_email.obligation_key(w.handler, "2026-11-11", "evt-1")
    neo.timeout_after_store_for = {key}
    neo.hide_queries = 3  # tick 1: lookup + read-back miss; tick 2: lookup misses
    w.tick()
    first_tick_unavailable = len(_kind(alerts, "unavailable"))
    neo.timeout_after_store_for = set()
    w.tick(minutes=15)  # re-raises with a new raised_at; the server answers 409
    assert key in neo.rows
    assert len(_kind(alerts, "unavailable")) == first_tick_unavailable, (
        "the idempotency conflict was treated as a held payment"
    )
    _approve(neo)
    w.tick(minutes=15)
    assert len(w.handler.execute_calls) == 1


# ── Cheap ux minors ──────────────────────────────────────────────────────────


def test_the_raised_alert_says_exactly_what_to_answer(env, alerts):
    w, neo = env
    w.tick()
    msg = _kind(alerts, "raised")[0]["message"]
    assert "attended yoga" in msg and "yes yoga" in msg


def test_a_stale_alert_repeats_after_a_further_interval(env, alerts, monkeypatch):
    from datetime import timedelta

    w, neo = env
    w.tick()
    real = payment_approval._now
    monkeypatch.setattr(payment_approval, "_now", lambda: real() + timedelta(hours=25))
    w.tick(minutes=15)
    monkeypatch.setattr(payment_approval, "_now", lambda: real() + timedelta(hours=49))
    w.tick(minutes=15)
    keys = {a["key"] for a in _kind(alerts, "stale")}
    assert len(keys) == 2
