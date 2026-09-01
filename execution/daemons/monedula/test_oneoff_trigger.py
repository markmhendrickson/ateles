"""
Unit tests for one-off (due-date-triggered) payment profiles.

Monedula was built for attendance-gated recurring payments: a calendar event
fires the handler. A one-off invoice has no session and no event, so profiles
without calendar_keywords were silently skipped at load time and could never
be paid by the daemon.

These tests lock the contract for the due-date trigger:

  * a one-off matches on or after its due date, never before
  * a malformed due date never fires a payment
  * the two trigger kinds do not bleed into each other — a recurring profile
    never matches on a date, a one-off never matches on a calendar event
  * a profile with neither trigger is rejected at load as unreachable
  * the payee can come from the profile itself (one-off payees are not
    standing contacts)

Run with: pytest execution/daemons/monedula/test_oneoff_trigger.py -v
"""

from __future__ import annotations

from datetime import date, timedelta

from handlers.payment_profile import PaymentProfile
from handlers.wise_transfer import WiseTransferHandler, _load_contact

TODAY = date.today()


def _oneoff(due: date | str, **kw) -> PaymentProfile:
    return PaymentProfile(
        prefix="INVOICE",
        label="Invoice",
        calendar_keywords=[],
        payment_type="wise",
        amount_eur=100,
        due_date=due if isinstance(due, str) else due.isoformat(),
        one_off=True,
        **kw,
    )


def _recurring(**kw) -> PaymentProfile:
    return PaymentProfile(
        prefix="THERAPY",
        label="Therapy",
        calendar_keywords=["therapy", "terapia"],
        payment_type="wise",
        amount_eur=60,
        **kw,
    )


# ── The due-date trigger fires on or after the date, never before ────────────


def test_matches_on_due_date() -> None:
    h = WiseTransferHandler(_oneoff(TODAY))
    matches = h.matches([])
    assert len(matches) == 1
    assert matches[0]["trigger"] == "due_date"
    assert matches[0]["overdue_days"] == 0


def test_matches_when_overdue() -> None:
    h = WiseTransferHandler(_oneoff(TODAY - timedelta(days=9)))
    matches = h.matches([])
    assert len(matches) == 1
    assert matches[0]["overdue_days"] == 9


def test_does_not_match_before_due() -> None:
    h = WiseTransferHandler(_oneoff(TODAY + timedelta(days=1)))
    assert h.matches([]) == []


# ── A malformed date must never fire a payment ───────────────────────────────


def test_malformed_due_date_does_not_match() -> None:
    for bad in ("31/07/2026", "soon", "2026-13-01", ""):
        h = WiseTransferHandler(_oneoff(bad))
        assert h.matches([]) == [], f"{bad!r} must not trigger a payment"


# ── The two trigger kinds stay separate ──────────────────────────────────────


def test_oneoff_ignores_calendar_events() -> None:
    """A due one-off must not multiply its match per calendar event."""
    h = WiseTransferHandler(_oneoff(TODAY))
    events = [{"summary": "Invoice something"}, {"summary": "Therapy"}]
    assert len(h.matches(events)) == 1


def test_recurring_still_matches_events() -> None:
    h = WiseTransferHandler(_recurring())
    assert len(h.matches([{"summary": "Therapy session"}])) == 1


def test_recurring_does_not_match_without_events() -> None:
    """No calendar event means no recurring payment — the attendance gate."""
    h = WiseTransferHandler(_recurring())
    assert h.matches([]) == []


def test_recurring_unaffected_by_a_due_date() -> None:
    """A recurring profile is event-driven even if a due_date is present."""
    h = WiseTransferHandler(_recurring(due_date=TODAY.isoformat()))
    assert h.matches([]) == []
    assert len(h.matches([{"summary": "terapia"}])) == 1


# ── Payee resolution comes from the profile for one-offs ─────────────────────


def test_payee_from_profile(monkeypatch) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    profile = _oneoff(TODAY, wise_iban="XX00 1111", wise_recipient_name="ACME, S.L.")
    contact = _load_contact(profile)
    assert contact == {"name": "ACME, S.L.", "iban": "XX00 1111"}


def test_partial_payee_falls_back(monkeypatch) -> None:
    """Half a payee is not a payee — don't silently pay with a missing name."""
    monkeypatch.delenv("DATA_DIR", raising=False)
    profile = _oneoff(TODAY, wise_iban="XX00 1111")
    assert _load_contact(profile) is None


# ── Preview reflects the trigger kind ────────────────────────────────────────


def test_preview_shows_due_date(monkeypatch) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    profile = _oneoff(TODAY, wise_iban="XX00 1111", wise_recipient_name="ACME, S.L.")
    h = WiseTransferHandler(profile)
    text = h.preview(h.matches([])[0])
    assert "one-off invoice" in text
    assert TODAY.isoformat() in text


# ── Anti-replay: sent one-offs close task + archive profile ───────────────────


def _ok_run(*_a, **_k):
    class _R:
        returncode = 0
        stderr = ""
        stdout = "{}"

    return _R()


def test_close_one_off_sent_archives_profile_and_marks_task_done(monkeypatch) -> None:
    """A DELIVERED one-off must mark the task done and archive the profile.

    Narrowed by ateles#575: the `sent` result driven here is now produced only
    when Wise's own transfer record says the payment was delivered, not merely
    when funding was accepted. The close-and-archive contract this test pins is
    unchanged — what changed is which transfers reach it. See the sibling
    test_awaiting_settlement_does_not_close_one_off for the other half.
    """
    import subprocess as _sp

    from handlers.wise_transfer import _close_one_off, _update_task

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return _ok_run()

    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/neotoma")
    monkeypatch.setattr(
        "handlers.wise_transfer._find_task_id", lambda _profile: "task_paid_1"
    )

    profile = _oneoff(TODAY, entity_id="prof_paid_1")
    _update_task(profile, {"status": "sent", "transfer_id": "tr_1"})

    status_corrections = [
        (entity_id, value)
        for entity_id, field, value in _corrections(calls)
        if field == "status"
    ]
    assert ("task_paid_1", "done") in status_corrections, (
        f"expected task done correction; got {status_corrections}"
    )
    assert ("prof_paid_1", "archived") in status_corrections, (
        f"expected profile archived correction; got {status_corrections}"
    )

    # Direct SUT path (same two status corrections, no notes call).
    calls.clear()
    _close_one_off(profile, "task_paid_1", "/usr/bin/neotoma")
    assert len(calls) == 2
    assert calls[0] == [
        "/usr/bin/neotoma",
        "--api-only",
        "corrections",
        "create",
        "--entity-id",
        "task_paid_1",
        "--field-name",
        "status",
        "--corrected-value",
        "done",
    ]
    assert calls[1] == [
        "/usr/bin/neotoma",
        "--api-only",
        "corrections",
        "create",
        "--entity-id",
        "prof_paid_1",
        "--field-name",
        "status",
        "--corrected-value",
        "archived",
    ]


def _corrections(calls: list[list[str]]) -> list[tuple[str, str, str]]:
    return [
        (
            c[c.index("--entity-id") + 1],
            c[c.index("--field-name") + 1],
            c[c.index("--corrected-value") + 1],
        )
        for c in calls
        if "corrections" in c and "create" in c
    ]


def test_awaiting_settlement_does_not_close_one_off(monkeypatch) -> None:
    """A submitted-but-undelivered transfer closes nothing (ateles#575).

    The sibling of the test above: same one-off profile, same code path, and
    the only difference is that Wise has not confirmed delivery. No --status
    argv of any kind may be emitted — not done, not archived.
    """
    import subprocess as _sp

    from handlers.wise_transfer import RESULT_AWAITING_SETTLEMENT, _update_task

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return _ok_run()

    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/neotoma")
    monkeypatch.setattr(
        "handlers.wise_transfer._find_task_id", lambda _profile: "task_flight_1"
    )

    profile = _oneoff(TODAY, entity_id="prof_flight_1")
    _update_task(
        profile,
        {"status": RESULT_AWAITING_SETTLEMENT, "transfer_id": "tr_1"},
    )

    status_corrections = [
        (entity_id, value)
        for entity_id, field, value in _corrections(calls)
        if field == "status" and value in ("done", "archived")
    ]
    assert status_corrections == [], (
        f"an unsettled transfer must not done/archive; got {status_corrections}"
    )


def test_update_task_manual_required_does_not_close_one_off(monkeypatch) -> None:
    """manual_required must leave the task open and the profile active."""
    import subprocess as _sp

    from handlers.wise_transfer import _update_task

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return _ok_run()

    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/neotoma")
    monkeypatch.setattr(
        "handlers.wise_transfer._find_task_id", lambda _profile: "task_manual_1"
    )

    profile = _oneoff(TODAY, entity_id="prof_manual_1")
    _update_task(profile, {"status": "manual_required"})

    status_corrections = [
        (entity_id, value)
        for entity_id, field, value in _corrections(calls)
        if field == "status" and value in ("done", "archived")
    ]
    assert status_corrections == [], (
        f"manual_required must not done/archive; got {status_corrections}"
    )


def test_close_one_off_empty_ids_warn_without_crash(monkeypatch, caplog) -> None:
    """Empty task_id / entity_id arms skip their update without raising."""
    import logging
    import subprocess as _sp

    from handlers.wise_transfer import _close_one_off

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return _ok_run()

    monkeypatch.setattr(_sp, "run", fake_run)

    profile = _oneoff(TODAY, entity_id="")
    with caplog.at_level(logging.WARNING):
        _close_one_off(profile, "", "/usr/bin/neotoma")

    assert calls == [], f"empty ids must not call neotoma correction; got {calls}"
    assert any("no entity id" in r.message for r in caplog.records)


def test_close_one_off_subprocess_failure_logged_not_raised(
    monkeypatch, caplog
) -> None:
    """Cleanup failures are logged; they must not propagate to the caller."""
    import logging
    import subprocess as _sp

    from handlers.wise_transfer import _close_one_off

    responses = [
        # task done: non-zero exit
        type("R", (), {"returncode": 1, "stderr": "boom", "stdout": ""})(),
        # profile archive: OSError
        OSError("neotoma unreachable"),
    ]

    def fake_run(cmd, *a, **k):
        next_resp = responses.pop(0)
        if isinstance(next_resp, BaseException):
            raise next_resp
        return next_resp

    monkeypatch.setattr(_sp, "run", fake_run)

    profile = _oneoff(TODAY, entity_id="prof_fail_1")
    with caplog.at_level(logging.ERROR):
        _close_one_off(profile, "task_fail_1", "/usr/bin/neotoma")  # must not raise

    assert any("ONE-OFF CLEANUP FAILED" in r.message for r in caplog.records)
    assert sum("ONE-OFF CLEANUP FAILED" in r.message for r in caplog.records) >= 2
