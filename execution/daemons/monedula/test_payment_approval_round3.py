# ruff: noqa: F401,F811  (shared fixtures are imported, not redefined)
"""Round 3: alerts carry fixed fields only; the SENT alert is actually delivered.

Each test here was seen red against 7ec90ca9 before the fix. Synthetic data only
(the IBAN-shaped values are fabricated placeholders).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import consent_email  # noqa: E402
import monedula  # noqa: E402
import payment_approval  # noqa: E402
from test_payment_approval import FakeNeotoma, env  # noqa: E402
from test_payment_approval_authority import _approve, _kind, alerts  # noqa: E402
from test_session_payments import _event  # noqa: E402

# Placeholder payee data in every shape a free-text error could carry it.
SPACED = " ".join(["ES91", "2100", "0418", "4502", "0005", "1332"])
LOWER = "es9121000418450200051332"
HOLDER = "Jane Q. Payee"
LEAK = f"Wise /v1/accounts returned no id: {{'name': '{HOLDER}', 'iban': '{SPACED}', 'alt': '{LOWER}'}}"
FRAGMENTS = ["jane", "payee", "2100", "0418", "9121", "es91", "wise /v1"]


def _blob(alerts) -> str:
    return " ".join(a["message"] for a in alerts).lower()


def _assert_clean(alerts):
    blob = _blob(alerts)
    for fragment in FRAGMENTS:
        assert fragment not in blob, f"{fragment!r} reached an operator alert"


# ── 1. Alerts never carry free text ──────────────────────────────────────────


@pytest.mark.parametrize("status", ["manual_required", "failed", HOLDER])
def test_outcome_alert_carries_a_fixed_category_not_the_error(env, alerts, status):
    w, neo = env
    w.handler.execute = lambda match: {"status": status, "error": LEAK}
    w.tick()
    _approve(neo)
    w.tick(minutes=15)
    out = [a for a in alerts if "payment_outcome" in (a["key"] or "")]
    assert len(out) == 1 and out[0]["priority"] == "blocker"
    _assert_clean(out)
    assert "daemon log" in out[0]["message"]
    assert "NOT" in out[0]["message"]


def test_the_error_text_stays_in_the_log(env, alerts, caplog):
    w, neo = env
    w.handler.execute = lambda match: {"status": "manual_required", "error": "token missing"}
    w.tick()
    _approve(neo)
    with caplog.at_level("INFO"):
        w.tick(minutes=15)
    assert any("token missing" in r.getMessage() for r in caplog.records)


def test_a_failed_record_raise_alert_has_no_exception_text(env, alerts):
    w, neo = env
    neo.fail_store_for = {consent_email.obligation_key(w.handler, "2026-11-11", "evt-1")}
    neo.store_error_message = f"HTTP 400: {LEAK}"
    w.tick()
    held = _kind(alerts, "unavailable")
    assert held
    _assert_clean(held)


def test_a_held_record_alert_has_no_status_free_text(env, alerts):
    w, neo = env
    w.tick()
    neo.rows[neo.only["payment_key"]]["status"] = f"{HOLDER} {SPACED}"
    w.tick(minutes=15)
    held = _kind(alerts, "not_accepted")
    assert held
    _assert_clean(held)


def test_a_preflight_failure_alert_has_no_exception_text(env, alerts):
    w, neo = env

    def preflight(match):
        raise RuntimeError(LEAK)

    w.handler.preflight = preflight
    w.tick()
    _approve(neo)
    w.tick(minutes=15)
    held = [a for a in alerts if "preflight" in (a["key"] or "")]
    assert held
    _assert_clean(held)


def test_a_recipient_id_stranding_detail_has_no_raw_value(monkeypatch):
    import io
    import json

    from handlers import payment_profile

    raw = f"{HOLDER} {SPACED}"

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    snap = {
        "label": "Test profile",
        "prefix": "TEST",
        "status": "active",
        "payment_type": "wise",
        "amount_eur": 10,
        "calendar_keywords": ["test"],
        "wise_recipient_id": raw,
    }
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.invalid")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "tok-placeholder")
    payload = {"entities": [{"entity_id": "ent_test0000000000000000000", "snapshot": snap}]}
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _Resp(json.dumps(payload).encode()),
    )
    strandings: list = []
    payment_profile.load_profiles_from_neotoma(strandings)
    assert strandings
    text = " ".join(s.detail for s in strandings).lower()
    for fragment in FRAGMENTS:
        assert fragment not in text

    monkeypatch.setenv("MONEDULA_PROFILES", "TESTP")
    monkeypatch.setenv("TESTP_CALENDAR_KEYWORDS", "yoga")
    monkeypatch.setenv("TESTP_AMOUNT_EUR", "10")
    monkeypatch.setenv("TESTP_WISE_RECIPIENT_ID", raw)
    env_strandings: list = []
    payment_profile.load_profiles(env_strandings)
    assert env_strandings
    text = " ".join(s.detail for s in env_strandings).lower()
    for fragment in FRAGMENTS:
        assert fragment not in text


# ── 2. The SENT alert reaches the operator through the real Notifier ────────


NO_SILENCE = {"silence_start": "", "silence_end": "", "timezone": "Europe/Madrid"}


def _real_notifier(monkeypatch, tmp_path):
    from lib.notify import Notifier
    from lib.notify import notifier as notifier_mod

    n = Notifier(rubric=NO_SILENCE)
    n._digest_path = tmp_path / "held.json"
    delivered: list[str] = []
    monkeypatch.setattr(
        n,
        "_deliver",
        lambda message, force=False, email_eligible=True: delivered.append(message) or True,
    )
    monkeypatch.setattr(notifier_mod.neotoma_sink, "record", lambda **k: None)
    monkeypatch.setattr(monedula, "_notifier", n)
    return n, delivered


def test_the_real_notifier_drops_info_but_delivers_the_sent_alert(env, monkeypatch, tmp_path):
    n, delivered = _real_notifier(monkeypatch, tmp_path)
    # Control: the real filter really does drop INFO, so a recording fake that
    # accepts everything could not have caught this.
    n.send("routine info", priority="info", handler="monedula")
    assert delivered == []

    w, neo = env
    w.tick()
    _approve(neo)
    w.tick(minutes=15)
    assert any("payment SENT" in m for m in delivered), delivered
    assert len(w.handler.execute_calls) == 1
