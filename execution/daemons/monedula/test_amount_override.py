"""
test_amount_override.py — Tests for Monedula's one-off per-session amount override.

A recurring obligation pays its profile's standing rate. A single session can
deviate (a group class, a double session) without editing that standing rate:
the task snapshot carries `amount_eur_override`, which the daemon copies onto
the match dict and the handler resolves via effective_amount_eur().

Covers the safety properties of that override:
  1. Resolution    — a valid override wins; absent/blank falls back to standing.
  2. Fail-safe     — invalid/zero/negative overrides fall back, never send €0.
  3. Non-mutation  — resolving an override NEVER edits the profile's standing
                     rate (the failure mode the override exists to prevent).
  4. Propagation   — the daemon puts the task's override on the match dict, so
                     the handler charges it.
  5. Visibility    — an override is disclosed in the approval email, so the
                     operator never approves a one-off amount unknowingly.

No real payment code runs: handlers are fakes that record calls.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import monedula  # noqa: E402
from handlers.payment_profile import PaymentProfile, effective_amount_eur  # noqa: E402

STANDING = 60
ONE_OFF = 70


def _profile(amount=STANDING):
    return PaymentProfile(
        prefix="YOGA", label="Yoga con Manel", calendar_keywords=["yoga"],
        payment_type="btc", amount_eur=amount, neotoma_task_id="ent_task",
    )


# --- 1. Resolution ---------------------------------------------------------

def test_valid_override_wins():
    assert effective_amount_eur(_profile(), {"amount_eur_override": ONE_OFF}) == ONE_OFF


def test_override_accepts_string_form():
    """Neotoma snapshots can stringify numerics — a string override must work."""
    assert effective_amount_eur(_profile(), {"amount_eur_override": str(ONE_OFF)}) == ONE_OFF


def test_no_match_falls_back_to_standing():
    assert effective_amount_eur(_profile(), None) == STANDING


def test_absent_or_blank_override_falls_back():
    for blank in ({}, {"amount_eur_override": None}, {"amount_eur_override": ""}):
        assert effective_amount_eur(_profile(), blank) == STANDING


# --- 2. Fail-safe ----------------------------------------------------------

def test_nonpositive_override_falls_back_never_sends_zero():
    """A €0 or negative override must never reach the wire."""
    for bad in (0, -5):
        assert effective_amount_eur(_profile(), {"amount_eur_override": bad}) == STANDING


def test_garbage_override_falls_back_without_raising():
    """A malformed override must not crash the daemon mid-payment."""
    for bad in ("abc", [], {}, object()):
        assert effective_amount_eur(_profile(), {"amount_eur_override": bad}) == STANDING


# --- 3. Non-mutation -------------------------------------------------------

def test_resolving_override_never_mutates_standing_rate():
    """The whole point: a one-off must not become the standing rate."""
    prof = _profile()
    effective_amount_eur(prof, {"amount_eur_override": ONE_OFF})
    assert prof.amount_eur == STANDING


# --- 4. Propagation --------------------------------------------------------

class _FakeProfile:
    def __init__(self, task_id, amount=STANDING):
        self.neotoma_task_id = task_id
        self.amount_eur = amount
        self.payment_type = "btc"
        self.label = "Yoga con Manel"
        self.btc_address = "bc1qexample"


class _FakeHandler:
    def __init__(self, name, task_id, amount=STANDING):
        self.name = name
        self.profile = _FakeProfile(task_id, amount)
        self.executed_with = []

    def execute(self, match):
        self.executed_with.append(match)
        return {"status": "sent", "handler": self.name, "txid": "deadbeef"}


def _task(tid, override=None, approved=True, due="2026-07-21"):
    snap = {
        "title": "Private yoga payment — Manel",
        "due_date": due,
        "payment_approved": approved,
        "status": "open",
        "payment_event_id": "",
    }
    if override is not None:
        snap["amount_eur_override"] = override
    return {"entity_id": tid, "snapshot": snap}


def test_daemon_puts_override_on_match(monkeypatch):
    """execute_approved_tasks must carry the task's override to the handler."""
    monkeypatch.setenv("MONEDULA_DRYRUN", "0")
    h = _FakeHandler("yoga", "ent_task")
    monedula.execute_approved_tasks([_task("ent_task", override=ONE_OFF)], [h])
    assert h.executed_with, "handler was never executed"
    assert h.executed_with[0].get("amount_eur_override") == ONE_OFF


def test_daemon_reports_override_amount_in_dry_run(monkeypatch):
    """Dry-run must quote the amount that WOULD be charged, not the standing rate."""
    monkeypatch.setenv("MONEDULA_DRYRUN", "1")
    h = _FakeHandler("yoga", "ent_task")
    results = monedula.execute_approved_tasks([_task("ent_task", override=ONE_OFF)], [h])
    assert results and results[0][1]["amount_eur"] == ONE_OFF
    assert not h.executed_with, "dry-run must not execute"


def test_task_without_override_charges_standing_rate(monkeypatch):
    monkeypatch.setenv("MONEDULA_DRYRUN", "1")
    h = _FakeHandler("yoga", "ent_task")
    results = monedula.execute_approved_tasks([_task("ent_task")], [h])
    assert results and results[0][1]["amount_eur"] == STANDING


# --- 5. Visibility ---------------------------------------------------------

def test_approval_email_discloses_one_off_amount():
    """The operator must see BOTH the one-off and the standing rate."""
    h = _FakeHandler("yoga", "ent_task")
    subject, body = monedula._build_approval_email(
        _task("ent_task", override=ONE_OFF, approved=False), h
    )
    assert f"€{ONE_OFF}" in subject, "subject must quote the charged amount"
    assert "ONE-OFF AMOUNT" in body
    assert f"standing rate is €{STANDING}" in body


def test_approval_email_has_no_override_note_for_normal_payment():
    """No override → no scary one-off warning."""
    h = _FakeHandler("yoga", "ent_task")
    subject, body = monedula._build_approval_email(_task("ent_task", approved=False), h)
    assert f"€{STANDING}" in subject
    assert "ONE-OFF AMOUNT" not in body
