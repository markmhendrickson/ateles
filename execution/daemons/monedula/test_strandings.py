"""
Tests for stranded-profile detection and escalation (ateles#553).

The defect these lock down: Monedula skipped any ACTIVE payment_profile it
could not act on with a bare WARNING and a `continue`, then exited 0. Over 17
days that produced 2,748 `is UNREACHABLE` warnings, 1,753 fetch failures, and
**zero** escalations — so the operator was never told a payment had not
happened. The pre-existing suite asserted the loader's happy paths and never
the rejection branches, which is why the same class of defect recurred.

These tests assert the *effect* — an escalation entity is written, the run
reports non-clean — not that a particular log string was emitted. A log-string
assertion would have passed against the broken code, since the broken code
logged perfectly good warnings that nobody read.

Run with: pytest execution/daemons/monedula/test_strandings.py -v
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from handlers.payment_profile import load_profiles_from_neotoma
from strandings import (
    REASON_BAD_AMOUNT,
    REASON_BAD_PAYMENT_TYPE,
    REASON_FETCH_FAILED,
    REASON_UNREACHABLE,
    Stranding,
    build_escalation_entity,
    escalate,
    select_new_strandings,
)

HOSTED = "https://neotoma.example.invalid"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NEOTOMA_BASE_URL", HOSTED)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "tok-placeholder")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _serve(monkeypatch, payload: dict):
    """Patch urlopen to answer the profile query with `payload`."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _Resp(json.dumps(payload).encode()),
    )


def _profile(**snapshot) -> dict:
    """A minimally-valid active profile entity, overridable per test."""
    snap = {
        "label": "Test profile",
        "prefix": "TEST",
        "status": "active",
        "payment_type": "wise",
        "amount_eur": 10,
        "calendar_keywords": ["test"],
    }
    snap.update(snapshot)
    return {"entity_id": "ent_test0000000000000000000", "snapshot": snap}


# ---------------------------------------------------------------------------
# Detection: each rejection branch produces a stranding
# ---------------------------------------------------------------------------


def test_unreachable_profile_is_recorded_as_stranded(monkeypatch):
    """The #553 headline case: no calendar_keywords and no due_date.

    This is the branch that fired 2,748 times and escalated nothing.
    """
    _serve(monkeypatch, {"entities": [_profile(calendar_keywords=[], due_date="")]})
    strandings: list = []
    profiles = load_profiles_from_neotoma(strandings)

    assert profiles == [], "an unreachable profile must not become a handler"
    assert len(strandings) == 1
    assert strandings[0].reason == REASON_UNREACHABLE
    assert strandings[0].entity_id == "ent_test0000000000000000000"


def test_bad_payment_type_is_recorded_as_stranded(monkeypatch):
    _serve(monkeypatch, {"entities": [_profile(payment_type="paypal")]})
    strandings: list = []
    load_profiles_from_neotoma(strandings)
    assert [s.reason for s in strandings] == [REASON_BAD_PAYMENT_TYPE]


def test_non_positive_amount_is_recorded_as_stranded(monkeypatch):
    _serve(monkeypatch, {"entities": [_profile(amount_eur=0)]})
    strandings: list = []
    load_profiles_from_neotoma(strandings)
    assert [s.reason for s in strandings] == [REASON_BAD_AMOUNT]


def test_failed_fetch_strands_every_profile(monkeypatch):
    """A transport failure is not 'no profiles configured'.

    This branch logged 1,753 WARNINGs. During those windows *every* profile
    was unpayable at once, which is strictly worse than one bad profile.
    """

    def boom(req, timeout=None):
        raise urllib.error.URLError("nodename nor servname provided")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    strandings: list = []
    assert load_profiles_from_neotoma(strandings) == []
    assert [s.reason for s in strandings] == [REASON_FETCH_FAILED]


def test_http_rejection_strands_every_profile(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(HOSTED, 403, "Forbidden", {}, io.BytesIO(b"1010"))

    monkeypatch.setattr("urllib.request.urlopen", boom)
    strandings: list = []
    assert load_profiles_from_neotoma(strandings) == []
    assert [s.reason for s in strandings] == [REASON_FETCH_FAILED]


# ---------------------------------------------------------------------------
# The boundary that keeps the alarm credible
# ---------------------------------------------------------------------------


def test_reachable_profile_is_not_stranded(monkeypatch):
    """A healthy profile must never escalate — 'nothing due' is normal."""
    _serve(monkeypatch, {"entities": [_profile()]})
    strandings: list = []
    profiles = load_profiles_from_neotoma(strandings)
    assert len(profiles) == 1
    assert strandings == []


def test_one_off_with_due_date_and_no_keywords_is_reachable(monkeypatch):
    """A one-off invoice triggers on its date; it is not stranded."""
    _serve(
        monkeypatch,
        {"entities": [_profile(calendar_keywords=[], due_date="2026-09-01")]},
    )
    strandings: list = []
    assert len(load_profiles_from_neotoma(strandings)) == 1
    assert strandings == []


def test_inactive_profile_is_not_stranded(monkeypatch):
    """Archived/paused profiles are deliberately inert, not defects.

    Escalating these would alarm on every archived invoice the operator has
    ever paid — the fastest way to make the channel ignorable.
    """
    _serve(
        monkeypatch,
        {
            "entities": [
                _profile(status="archived", calendar_keywords=[], due_date=""),
                _profile(status="superseded", calendar_keywords=[], due_date=""),
            ]
        },
    )
    strandings: list = []
    assert load_profiles_from_neotoma(strandings) == []
    assert strandings == [], "inactive profiles must never escalate"


def test_collector_is_optional(monkeypatch):
    """Omitting the collector preserves the historical loader behaviour."""
    _serve(monkeypatch, {"entities": [_profile(calendar_keywords=[], due_date="")]})
    assert load_profiles_from_neotoma() == []


# ---------------------------------------------------------------------------
# Deduplication — 2,748 escalations would be its own incident
# ---------------------------------------------------------------------------


def _s(reason=REASON_UNREACHABLE, entity_id="ent_a", label="A") -> Stranding:
    return Stranding(entity_id=entity_id, label=label, reason=reason, detail="d")


def test_first_observation_escalates():
    fresh, state = select_new_strandings([_s()], state={}, now=1000.0)
    assert len(fresh) == 1
    assert state[_s().key]["escalated_at"] == 1000.0


def test_same_condition_does_not_re_escalate_next_tick():
    """The daemon wakes every ~15 minutes; the condition escalates once."""
    _, state = select_new_strandings([_s()], state={}, now=1000.0)
    fresh, _ = select_new_strandings([_s()], state=state, now=1000.0 + 900)
    assert fresh == [], "an unchanged stranding must stay quiet"


def test_unchanged_condition_re_escalates_after_the_interval():
    """A stranding must not fade out of view entirely."""
    _, state = select_new_strandings([_s()], state={}, now=1000.0)
    fresh, _ = select_new_strandings(
        [_s()], state=state, now=1000.0 + 25 * 3600
    )
    assert len(fresh) == 1


def test_suppression_interval_measures_from_first_report():
    """Ticking must not reset the clock and suppress a stranding forever."""
    _, state = select_new_strandings([_s()], state={}, now=0.0)
    for tick in range(1, 20):  # ~5 hours of 15-minute ticks
        fresh, state = select_new_strandings([_s()], state=state, now=tick * 900.0)
        assert fresh == []
    fresh, _ = select_new_strandings([_s()], state=state, now=25 * 3600.0)
    assert len(fresh) == 1, "the interval must elapse from the first report"


def test_new_reason_for_same_profile_escalates_again():
    """Fixing one defect and uncovering another is new information."""
    _, state = select_new_strandings([_s(REASON_UNREACHABLE)], state={}, now=1000.0)
    fresh, _ = select_new_strandings(
        [_s(REASON_BAD_AMOUNT)], state=state, now=1000.0 + 60
    )
    assert len(fresh) == 1
    assert fresh[0].reason == REASON_BAD_AMOUNT


def test_distinct_profiles_escalate_separately():
    fresh, _ = select_new_strandings(
        [_s(entity_id="ent_a", label="A"), _s(entity_id="ent_b", label="B")],
        state={},
        now=1000.0,
    )
    assert len(fresh) == 2


def test_cleared_condition_that_returns_is_reported_as_new():
    """A recurrence is news; stale state must not suppress it."""
    _, state = select_new_strandings([_s()], state={}, now=1000.0)
    _, state = select_new_strandings([], state=state, now=2000.0)
    fresh, _ = select_new_strandings([_s()], state=state, now=3000.0)
    assert len(fresh) == 1


def test_dedup_volume_against_the_real_incident():
    """17 days of 15-minute ticks on 5 profiles: 5 escalations/day, not 2,748."""
    state: dict = {}
    profiles = [_s(entity_id=f"ent_{i}", label=f"P{i}") for i in range(5)]
    total = 0
    ticks = 17 * 24 * 4  # 15-minute ticks over 17 days
    for tick in range(ticks):
        fresh, state = select_new_strandings(profiles, state=state, now=tick * 900.0)
        total += len(fresh)
    assert total == 5 * 17, f"expected one per profile per day, got {total}"
    assert total < 100, "escalation volume must not become its own incident"


# ---------------------------------------------------------------------------
# Escalation effect: a durable entity is written, and the run is non-clean
# ---------------------------------------------------------------------------


def test_escalation_entity_matches_the_schema():
    entity = build_escalation_entity(_s(), observed_at="2026-08-31T12:00:00Z")
    assert entity["entity_type"] == "escalation"
    # Fields declared on the registered escalation schema (v1.0).
    for f in ("title", "body", "severity", "source_agent", "status", "tags"):
        assert f in entity
    assert entity["severity"] == "error"
    assert entity["status"] == "open"
    assert entity["source_entity_type"] == "payment_profile"
    assert entity["source_entity_id"] == "ent_a"


def test_escalation_entity_sends_no_undeclared_fields():
    """The entity carries ONLY fields the registered escalation schema declares.

    The original assertion checked that each declared field was PRESENT, which
    an undeclared extra passes trivially — `observed_at` shipped that way
    (ateles#599 arch lens, confirmed live via describe_entity_type). An
    undeclared field is an unknown_fields defect, not tolerated pass-through,
    so the check has to be exact rather than one-directional.

    The observation time still reaches the operator: it is stated in the body.
    """
    declared = {
        "entity_type",
        "title",
        "body",
        "severity",
        "source_agent",
        "source_entity_id",
        "source_entity_type",
        "status",
        "tags",
    }
    entity = build_escalation_entity(_s(), observed_at="2026-08-31T12:00:00Z")

    assert set(entity) == declared, f"undeclared: {sorted(set(entity) - declared)}"
    assert "observed_at" not in entity
    assert "2026-08-31T12:00:00Z" in entity["body"], "the timestamp must not be lost"


def test_escalate_writes_an_entity_and_notifies(monkeypatch, tmp_path):
    """The effect assertion #553 asked for: an escalation is actually filed."""
    posted: list = []
    monkeypatch.setattr(
        "strandings._post_escalation",
        lambda entity, key: posted.append((entity, key)) or True,
    )
    notes: list = []
    fresh = escalate(
        [_s()],
        notify=lambda msg, priority="info": notes.append((msg, priority)),
        state_file=tmp_path / "state.json",
        now=1000.0,
    )
    assert len(fresh) == 1
    assert len(posted) == 1, "a durable escalation entity must be written"
    assert posted[0][0]["entity_type"] == "escalation"
    assert len(notes) == 1
    assert notes[0][1] == "blocker", "a stranded payment is not an INFO digest item"


def test_escalation_is_written_even_when_notification_fails(monkeypatch, tmp_path):
    """Notification is best-effort; the durable record is not.

    Monedula's log shows both notifier legs failing for long stretches (gws
    +send rc=2/rc=4 during DNS outages). An escalation that lived only in the
    notifier would have been silent for exactly those windows.
    """
    posted: list = []
    monkeypatch.setattr(
        "strandings._post_escalation",
        lambda entity, key: posted.append(entity) or True,
    )

    def broken_notify(msg, priority="info"):
        raise RuntimeError("transport down")

    fresh = escalate(
        [_s()], notify=broken_notify, state_file=tmp_path / "s.json", now=1000.0
    )
    assert len(fresh) == 1
    assert len(posted) == 1


def test_escalate_is_a_noop_when_nothing_is_stranded(monkeypatch, tmp_path):
    posted: list = []
    monkeypatch.setattr(
        "strandings._post_escalation", lambda e, k: posted.append(e) or True
    )
    assert escalate([], notify=lambda *a, **k: None, state_file=tmp_path / "s.json") == []
    assert posted == []


def test_idempotency_key_is_stable_within_a_day(monkeypatch, tmp_path):
    """A retry inside one window must not create duplicate escalations."""
    keys: list = []
    monkeypatch.setattr(
        "strandings._post_escalation", lambda e, k: keys.append(k) or True
    )
    escalate([_s()], state_file=tmp_path / "a.json", now=1000.0)
    escalate([_s()], state_file=tmp_path / "b.json", now=1000.0 + 3600)
    assert len(keys) == 2
    assert keys[0] == keys[1], "same condition, same day — same idempotency key"


def test_corrupt_state_file_escalates_rather_than_suppresses(monkeypatch, tmp_path):
    """Unreadable state must fail toward noise, never toward silence."""
    monkeypatch.setattr("strandings._post_escalation", lambda e, k: True)
    bad = tmp_path / "corrupt.json"
    bad.write_text("{not json")
    assert len(escalate([_s()], state_file=bad, now=1000.0)) == 1


# ---------------------------------------------------------------------------
# End to end: a stranded profile makes the whole run non-clean
# ---------------------------------------------------------------------------


def test_run_with_a_stranded_profile_is_not_clean(monkeypatch, tmp_path):
    """`main()` must not report success when a payment could not fire.

    This is the assertion that would have broken the sixteen consecutive
    "clean" exits in #553. The daemon had nothing else to do that run — the
    'nothing to do' early return is exactly the path these strandings took.
    """
    import sys
    import types

    import monedula

    monkeypatch.setattr(monedula, "STATE_FILE", tmp_path / ".last_run")
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(monedula, "telegram_send", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        monedula, "fetch_due_payment_tasks", lambda *a, **k: [], raising=False
    )
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [])

    filed: list = []
    monkeypatch.setattr(
        monedula, "escalate_strandings", lambda s: filed.extend(s) or list(s)
    )

    fake = types.ModuleType("handlers")

    def _load(strandings=None):
        if strandings is not None:
            strandings.append(_s())
        return []

    fake.load_handlers = _load
    monkeypatch.setitem(sys.modules, "handlers", fake)

    assert monedula.main() is False, "a stranded profile must not exit clean"
    assert len(filed) == 1, "the stranding must be escalated before the early return"


def test_run_with_no_strandings_is_clean(monkeypatch, tmp_path):
    """The converse: normal operation must still report success."""
    import sys
    import types

    import monedula

    monkeypatch.setattr(monedula, "STATE_FILE", tmp_path / ".last_run")
    monkeypatch.setattr(monedula, "_notify", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(monedula, "telegram_send", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        monedula, "fetch_due_payment_tasks", lambda *a, **k: [], raising=False
    )
    monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [])

    fake = types.ModuleType("handlers")
    fake.load_handlers = lambda strandings=None: []
    monkeypatch.setitem(sys.modules, "handlers", fake)

    assert monedula.main() is True
