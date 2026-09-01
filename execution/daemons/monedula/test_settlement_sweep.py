"""
Settlement-sweep tests (ateles#575).

`awaiting_settlement` is only defensible because something drives its exit. A
hold state nothing transitions out of is a task parked forever, which is worse
than the false `done` it replaces. The sweep is that consumer: it re-reads each
parked transfer from Wise and drives one of three exits — settled, failed, or
still in flight (reported as suspect once it has waited too long).

The exits are OBSERVED. The sweep believes Wise's own transfer record, never
the daemon's memory of what it submitted, so a stuck transfer stays visible in
the digest instead of quietly resolving itself.

**Money-safety invariant asserted throughout: the sweep never POSTs to Wise.**
A POST from here is a second payment.

Run with: pytest execution/daemons/monedula/test_settlement_sweep.py -v
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

import settlement
from handlers.payment_profile import PaymentProfile

TODAY = date(2026, 8, 31)


def _parked(**kw) -> PaymentProfile:
    base = dict(
        prefix="INVOICE",
        label="Invoice",
        calendar_keywords=[],
        payment_type="wise",
        amount_eur=Decimal("100.00"),
        due_date="2026-08-01",
        one_off=True,
        entity_id="prof_x",
        wise_reference="ref-1",
        pending_transfer_id="7",
        pending_transfer_at=TODAY.isoformat(),
    )
    base.update(kw)
    return PaymentProfile(**base)  # type: ignore[arg-type]


class _Ok:
    returncode = 0
    stderr = ""
    stdout = "{}"


@pytest.fixture
def calls(monkeypatch) -> list[list[str]]:
    import subprocess as _sp

    captured: list[list[str]] = []
    monkeypatch.setattr(
        _sp, "run", lambda cmd, *a, **k: (captured.append(list(cmd)), _Ok())[1]
    )
    monkeypatch.setattr(settlement, "shutil", _WhichStub())
    monkeypatch.setattr(settlement, "find_task_id", lambda _p: "task_x")
    monkeypatch.setattr(settlement, "note_task", _real_note_task)
    return captured


class _WhichStub:
    @staticmethod
    def which(_name):
        return "/usr/bin/neotoma"


def _real_note_task(profile, task_id, neotoma, text):
    from handlers.wise_transfer import note_task

    return note_task(profile, task_id, neotoma, text)


@pytest.fixture
def no_wise_post(monkeypatch):
    """The money-safety invariant: a POST during a sweep is a double payment."""
    posted: list[str] = []
    monkeypatch.setattr(
        "handlers.wise_transfer._wise_post",
        lambda t, path, body: posted.append(path),
    )
    return posted


def _wire(monkeypatch, profiles, transfer):
    monkeypatch.setattr(
        settlement, "load_profiles_awaiting_settlement", lambda: list(profiles)
    )
    monkeypatch.setattr(settlement, "fetch_transfer", lambda _t, _id: transfer)


def status_updates(calls) -> list[tuple[str, str]]:
    out = []
    for c in calls:
        if "entities" in c and "update" in c and "--status" in c:
            out.append((c[c.index("update") + 1], c[c.index("--status") + 1]))
    return out


def corrections(calls) -> list[tuple[str, str, str]]:
    return [
        (
            c[c.index("--entity-id") + 1],
            c[c.index("--field-name") + 1],
            c[c.index("--corrected-value") + 1],
        )
        for c in calls
        if "corrections" in c and "create" in c
    ]


def notes(calls) -> list[str]:
    return [c[c.index("--notes") + 1] for c in calls if "--notes" in c]


def due_dates(calls) -> list[str]:
    return [c[c.index("--due-date") + 1] for c in calls if "--due-date" in c]


# ── settled ──────────────────────────────────────────────────────────────────


def test_settled_one_off_closes_task_and_archives(
    monkeypatch, calls, no_wise_post
) -> None:
    """This is the ONLY path that may mark a payment task done."""
    _wire(monkeypatch, [_parked()], {"status": "outgoing_payment_sent"})

    records = settlement.sweep_pending_settlements("tok", today=TODAY)

    assert records[0]["outcome"] == "settled"
    assert ("task_x", "done") in status_updates(calls)
    assert ("prof_x", "archived") in status_updates(calls)
    assert any(
        "Payment settled" in n and "outgoing_payment_sent" in n for n in notes(calls)
    )
    assert no_wise_post == []


def test_settled_recurring_restores_active_and_rolls_due_date(
    monkeypatch, calls, no_wise_post
) -> None:
    profile = _parked(
        prefix="THERAPY",
        label="Therapy",
        calendar_keywords=["therapy"],
        due_date="",
        one_off=False,
        entity_id="prof_r",
    )
    _wire(monkeypatch, [profile], {"status": "outgoing_payment_sent"})
    monkeypatch.setattr(settlement, "find_next_event_due_date", lambda _p: "2026-09-30")

    settlement.sweep_pending_settlements("tok", today=TODAY)

    got = {(f, v) for _e, f, v in corrections(calls)}
    assert ("status", "active") in got
    assert ("pending_transfer_id", "") in got
    assert due_dates(calls) == ["2026-09-30"]
    assert ("task_x", "done") not in status_updates(calls)
    assert no_wise_post == []


def test_settled_recurring_with_no_next_event_still_unparks(
    monkeypatch, calls, no_wise_post
) -> None:
    """Without this the profile is stranded and the payment stops recurring."""
    profile = _parked(
        calendar_keywords=["therapy"], due_date="", one_off=False, entity_id="prof_r"
    )
    _wire(monkeypatch, [profile], {"status": "outgoing_payment_sent"})
    monkeypatch.setattr(settlement, "find_next_event_due_date", lambda _p: None)

    settlement.sweep_pending_settlements("tok", today=TODAY)

    got = {(f, v) for _e, f, v in corrections(calls)}
    assert ("status", "active") in got
    assert ("pending_transfer_id", "") in got


# ── failed ───────────────────────────────────────────────────────────────────


def test_failed_transfer_leaves_task_open_and_does_not_rearm(
    monkeypatch, calls, no_wise_post
) -> None:
    """A failed payment is never re-armed automatically.

    Returning the profile to `active` would let the next tick propose the same
    payment again, on a transfer whose money may or may not have come back.
    Re-payment is an operator decision.
    """
    # Second observation: the first already latched bounced_back onto the
    # profile, so this read is the confirming one.
    _wire(
        monkeypatch,
        [_parked(pending_failed_status="bounced_back")],
        {"status": "bounced_back"},
    )

    records = settlement.sweep_pending_settlements("tok", today=TODAY)

    assert records[0]["outcome"] == "failed"
    got = {(f, v) for _e, f, v in corrections(calls)}
    assert ("status", "payment_failed") in got
    assert ("status", "active") not in got
    assert status_updates(calls) == []  # no done, no archived
    assert any("FAILED" in n for n in notes(calls))
    assert no_wise_post == []


# ── the two-observation failure guard (ateles#604 review) ───────────────────
#
# The module documented this guard and did not implement it: `_resolve_one`
# acted on the FIRST failed read, wrote payment_failed and cleared
# pending_transfer_id — after which nothing re-read the transfer, so a
# transient was permanent. The operator's own ledger has bounced_back appearing
# ~20s after funding and resolving back to processing on the next poll, which
# is exactly the case that would have been declared dead.


def test_a_single_failed_read_records_no_terminal_outcome(
    monkeypatch, calls, no_wise_post
) -> None:
    """First observation latches the status and writes nothing terminal."""
    _wire(monkeypatch, [_parked()], {"status": "bounced_back"})

    records = settlement.sweep_pending_settlements("tok", today=TODAY)

    assert records[0]["outcome"] == "failed_pending_confirmation"
    got = {(f, v) for _e, f, v in corrections(calls)}
    assert ("pending_failed_status", "bounced_back") in got, "the read must be latched"
    assert ("status", "payment_failed") not in got, "one read is not a verdict"
    assert not any(f == "pending_transfer_id" for _e, f, _v in corrections(calls)), (
        "clearing the transfer id on one read makes the error permanent"
    )
    assert status_updates(calls) == []
    assert no_wise_post == []


def test_a_transient_failure_that_recovers_clears_the_latch(
    monkeypatch, calls, no_wise_post
) -> None:
    """bounced_back -> processing must not leave the profile pre-confirmed.

    This is the transient the guard exists for. Without clearing, an unrelated
    failure months later would be treated as already-confirmed on its first
    read — the same one-read verdict by another route.
    """
    _wire(
        monkeypatch,
        [_parked(pending_failed_status="bounced_back")],
        {"status": "processing"},
    )

    records = settlement.sweep_pending_settlements("tok", today=TODAY)

    assert records[0]["outcome"] == "in_flight"
    assert ("pending_failed_status", "") in {
        (f, v) for _e, f, v in corrections(calls)
    }, "a recovered transfer must clear the latch"
    assert no_wise_post == []


def test_two_different_failed_statuses_do_not_confirm_each_other(
    monkeypatch, calls, no_wise_post
) -> None:
    """The guard requires the SAME status twice, not merely two failures."""
    _wire(
        monkeypatch,
        [_parked(pending_failed_status="bounced_back")],
        {"status": "charged_back"},
    )

    records = settlement.sweep_pending_settlements("tok", today=TODAY)

    assert records[0]["outcome"] == "failed_pending_confirmation"
    got = {(f, v) for _e, f, v in corrections(calls)}
    assert ("pending_failed_status", "charged_back") in got
    assert ("status", "payment_failed") not in got
    assert no_wise_post == []


# ── in flight ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "transfer", [{"status": "processing"}, {"status": "unknown"}, None, {}]
)
def test_in_flight_and_unreadable_write_nothing(
    transfer, monkeypatch, calls, no_wise_post
) -> None:
    _wire(monkeypatch, [_parked()], transfer)
    records = settlement.sweep_pending_settlements("tok", today=TODAY)
    assert records[0]["outcome"] == "in_flight"
    assert calls == []
    assert no_wise_post == []


@pytest.mark.parametrize(
    "age,expected", [(4, "in_flight"), (5, "suspect"), (6, "suspect")]
)
def test_age_threshold_boundary(age, expected, monkeypatch, calls) -> None:
    """Pinned as `>=`: at exactly the threshold, it is suspect."""
    monkeypatch.setenv("MONEDULA_SETTLEMENT_ALERT_DAYS", "5")
    profile = _parked(pending_transfer_at=(TODAY - timedelta(days=age)).isoformat())
    _wire(monkeypatch, [profile], {"status": "processing"})

    records = settlement.sweep_pending_settlements("tok", today=TODAY)
    assert records[0]["outcome"] == expected
    assert calls == [], "a suspect record still writes nothing — it only reports"


@pytest.mark.parametrize(
    "bad", ["", "soon", "31/07/2026", (TODAY + timedelta(days=3)).isoformat()]
)
def test_malformed_pending_transfer_at_is_suspect_not_silent(
    bad, monkeypatch, calls
) -> None:
    """A wait that cannot be measured must surface, not read as brand new."""
    _wire(monkeypatch, [_parked(pending_transfer_at=bad)], {"status": "processing"})
    records = settlement.sweep_pending_settlements("tok", today=TODAY)
    assert records[0]["outcome"] == "suspect"


@pytest.mark.parametrize(
    "raw,expected", [(None, 5), ("", 5), ("abc", 5), ("-1", 5), ("0", 0), ("9", 9)]
)
def test_alert_days_env_parsing(raw, expected, monkeypatch) -> None:
    from handlers.wise_transfer import settlement_alert_days

    if raw is None:
        monkeypatch.delenv("MONEDULA_SETTLEMENT_ALERT_DAYS", raising=False)
    else:
        monkeypatch.setenv("MONEDULA_SETTLEMENT_ALERT_DAYS", raw)
    assert settlement_alert_days() == expected


def test_zero_threshold_makes_everything_suspect_immediately(
    monkeypatch, calls
) -> None:
    monkeypatch.setenv("MONEDULA_SETTLEMENT_ALERT_DAYS", "0")
    _wire(monkeypatch, [_parked()], {"status": "processing"})
    records = settlement.sweep_pending_settlements("tok", today=TODAY)
    assert records[0]["outcome"] == "suspect"


# ── unresolvable transfer ids ────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "   ", "abc", "0", "-1"])
def test_unresolvable_pending_transfer_id_is_skipped_with_warning(
    bad, monkeypatch, caplog
) -> None:
    """Validity, not str.isdigit(): "0" and "-1" are not Wise transfer ids."""
    import logging

    import handlers.payment_profile as pp

    monkeypatch.setattr(
        pp, "load_profiles_from_neotoma", lambda **k: [_parked(pending_transfer_id=bad)]
    )
    with caplog.at_level(logging.WARNING):
        assert pp.load_profiles_awaiting_settlement() == []
    assert any("pending_transfer_id" in r.message for r in caplog.records)


# ── resilience ───────────────────────────────────────────────────────────────


def test_one_profile_failure_does_not_abort_the_sweep(monkeypatch, calls) -> None:
    """Profiles left unswept are transfers nobody is watching."""
    profiles = [
        _parked(entity_id="prof_1", pending_transfer_id="1"),
        _parked(entity_id="prof_2", pending_transfer_id="2"),
        _parked(entity_id="prof_3", pending_transfer_id="3"),
    ]

    def _fetch(_token, transfer_id):
        if transfer_id == 2:
            raise RuntimeError("wise unreachable")
        return {"status": "processing"}

    monkeypatch.setattr(
        settlement, "load_profiles_awaiting_settlement", lambda: profiles
    )
    monkeypatch.setattr(settlement, "fetch_transfer", _fetch)

    records = settlement.sweep_pending_settlements("tok", today=TODAY)
    assert len(records) == 3
    assert records[1]["outcome"] == "suspect"
    assert records[0]["outcome"] == "in_flight"
    assert records[2]["outcome"] == "in_flight"


def test_sweep_is_idempotent(monkeypatch, calls) -> None:
    """Once resolved, a profile is no longer parked, so a re-run writes nothing."""
    state = {"parked": [_parked()]}
    monkeypatch.setattr(
        settlement, "load_profiles_awaiting_settlement", lambda: state["parked"]
    )
    monkeypatch.setattr(
        settlement, "fetch_transfer", lambda *a: {"status": "outgoing_payment_sent"}
    )

    settlement.sweep_pending_settlements("tok", today=TODAY)
    assert calls, "first pass writes"

    state["parked"] = []  # the correction removed it from awaiting_settlement
    calls.clear()
    assert settlement.sweep_pending_settlements("tok", today=TODAY) == []
    assert calls == []


def test_sweep_without_neotoma_cli_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(settlement.shutil, "which", lambda _n: None)
    assert settlement.sweep_pending_settlements("tok", today=TODAY) == []


# ── digest formatting ────────────────────────────────────────────────────────


def test_format_settlement_lines() -> None:
    lines = settlement.format_settlement_lines(
        [
            {
                "profile": "quiet-profile",
                "transfer_id": "1",
                "outcome": "in_flight",
                "wise_status": "processing",
                "age_days": 1,
            },
            {
                "profile": "settled-profile",
                "transfer_id": "2",
                "outcome": "settled",
                "wise_status": "outgoing_payment_sent",
                "age_days": 2,
            },
            {
                "profile": "failed-profile",
                "transfer_id": "3",
                "outcome": "failed",
                "wise_status": "bounced_back",
                "age_days": 3,
            },
            {
                "profile": "stuck-profile",
                "transfer_id": "4",
                "outcome": "suspect",
                "wise_status": "processing",
                "age_days": 9,
            },
        ]
    )
    joined = "\n".join(lines)
    assert "quiet-profile" not in joined, "a healthy in-flight transfer is not reported"
    assert "settled-profile" in joined and "settled" in joined
    assert "failed-profile" in joined and "NOT re-armed" in joined
    assert "stuck-profile" in joined and "9d" in joined


def test_format_settlement_lines_handles_unknown_age() -> None:
    (line,) = settlement.format_settlement_lines(
        [
            {
                "profile": "A",
                "transfer_id": "1",
                "outcome": "suspect",
                "wise_status": "processing",
                "age_days": None,
            }
        ]
    )
    assert "age unknown" in line


# ── Daemon wiring: the sweep must run where AC-9 can actually hold ───────────


def test_sweep_call_precedes_the_early_return() -> None:
    """Placement is load-bearing, so pin it structurally.

    Behind the "nothing to do" early return, a quiet day would never resolve an
    in-flight transfer and awaiting_settlement would be a state nothing exits.
    Importing monedula.py runs a Notifier bootstrap against the operator's real
    config, so this reads the source rather than executing main().
    """
    import pathlib

    src = (pathlib.Path(__file__).parent / "monedula.py").read_text()

    # Anchor on the executable statements, not on prose that mentions them.
    sweep_at = src.index("settlement_records = sweep_pending_settlements(")
    early_return_at = src.index(
        "if not triggered and not due_tasks and not settlement_lines:"
    )
    assert sweep_at < early_return_at, (
        "the settlement sweep must run BEFORE the early return"
    )


def test_early_return_accounts_for_settlement_records() -> None:
    """A settlement-only tick must still send its digest."""
    import pathlib

    src = (pathlib.Path(__file__).parent / "monedula.py").read_text()
    assert "not triggered and not due_tasks and not settlement_lines" in src


def test_missing_wise_token_skips_sweep_not_the_run() -> None:
    """A sweep that cannot run must not take payment detection down."""
    import pathlib

    src = (pathlib.Path(__file__).parent / "monedula.py").read_text()
    guard = src[src.index("settlement_records: list[dict] = []") :]
    assert "WISE_API_TOKEN" in guard[:400]
    # The sweep is wrapped so a failure notifies and continues rather than raising.
    assert "except Exception" in guard[:1400]
    assert 'priority="blocker"' in guard[:1400]
