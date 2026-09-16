"""Tests for the connector contract and the staleness verdict.

The staleness rules are the design, so they are tested hardest: a bug here
produces a record that confidently states the wrong thing, which is the exact
failure the package exists to prevent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lib.connectors.base import (
    INGESTION_MODES,
    MIN_STALE_AFTER_SECONDS,
    ConnectorResult,
    ConnectorStatus,
    Freshness,
    assess_freshness,
    stale_after_for,
)

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


# ── the threshold formula ───────────────────────────────────────────────────


def test_stale_after_is_three_poll_intervals():
    # 30min poll -> 90min, comfortably above the floor.
    assert stale_after_for(1800) == 5400


def test_stale_after_respects_the_floor():
    # A 60s poll would give 3min, which would call a brief blip "stale".
    assert stale_after_for(60) == MIN_STALE_AFTER_SECONDS


def test_fly_connector_threshold_is_45_minutes():
    """The documented number for a 15-minute poll — keep doc and code agreed."""
    assert stale_after_for(900) == 2700


# ── fresh / stale / unknown ─────────────────────────────────────────────────


def test_recent_observation_is_fresh():
    f = assess_freshness(_ago(minutes=5), stale_after_seconds=2700, now=NOW)
    assert f.state == "fresh"
    assert f.is_fresh and f.alarms_allowed


def test_old_observation_is_stale():
    f = assess_freshness(_ago(hours=3), stale_after_seconds=2700, now=NOW)
    assert f.state == "stale"
    assert not f.is_fresh


def test_exactly_at_threshold_is_still_fresh():
    """The boundary is inclusive; only PAST it is stale."""
    f = assess_freshness(_ago(seconds=2700), stale_after_seconds=2700, now=NOW)
    assert f.state == "fresh"


def test_missing_timestamp_is_unknown_not_stale():
    """'We could not tell' must never collapse into 'we can tell, and it is bad'."""
    for bad in (None, "", "not-a-date", 12345):
        assert assess_freshness(bad, stale_after_seconds=2700, now=NOW).state == "unknown"


def test_unknown_carries_no_age():
    f = assess_freshness(None, stale_after_seconds=2700, now=NOW)
    assert f.age_seconds is None
    assert "never observed" in f.summary()


def test_future_timestamp_does_not_produce_negative_age():
    """Clock skew must not invent a verdict."""
    ahead = (NOW + timedelta(hours=2)).isoformat()
    f = assess_freshness(ahead, stale_after_seconds=2700, now=NOW)
    assert f.age_seconds == 0.0
    assert f.state == "fresh"


def test_naive_and_z_suffixed_timestamps_both_parse():
    naive = (NOW - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    zed = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    assert assess_freshness(naive, stale_after_seconds=2700, now=NOW).state == "fresh"
    assert assess_freshness(zed, stale_after_seconds=2700, now=NOW).state == "fresh"


# ── alarm suppression ───────────────────────────────────────────────────────


def test_alarms_suppressed_on_stale_and_unknown():
    """An alarm from a stale reading asserts a present it cannot see."""
    assert not Freshness(state="stale", age_seconds=99999).alarms_allowed
    assert not Freshness(state="unknown").alarms_allowed
    assert Freshness(state="fresh", age_seconds=10).alarms_allowed


# ── the summary an operator reads ───────────────────────────────────────────


def test_summary_states_age_not_a_bare_value():
    fresh = assess_freshness(_ago(minutes=4), stale_after_seconds=2700, now=NOW)
    assert fresh.summary() == "observed 4m ago"

    stale = assess_freshness(_ago(hours=26), stale_after_seconds=2700, now=NOW)
    assert stale.summary().startswith("STALE")
    assert "1d2h" in stale.summary()


# ── ConnectorStatus ─────────────────────────────────────────────────────────


def test_status_freshness_uses_last_success_not_last_attempt():
    """A connector failing every minute has a recent attempt and stale data.

    This is the distinction the two fields exist to draw; judging freshness on
    attempts would make a broken connector look healthy.
    """
    s = ConnectorStatus(
        connector_name="fly",
        status="failing",
        last_attempt_at=_ago(seconds=30),
        last_success_at=_ago(hours=9),
        stale_after_seconds=2700,
    )
    assert s.freshness(now=NOW).state == "stale"


def test_status_never_succeeded_is_unknown():
    s = ConnectorStatus(
        connector_name="fly", last_attempt_at=_ago(seconds=10), stale_after_seconds=2700
    )
    assert s.freshness(now=NOW).state == "unknown"


def test_status_entity_fields_exclude_entity_id():
    """entity_id addresses the record; it is not part of the snapshot."""
    s = ConnectorStatus(connector_name="fly", entity_id="ent_abc")
    assert "entity_id" not in s.to_entity_fields()
    assert s.to_entity_fields()["connector_name"] == "fly"


# ── ConnectorResult ─────────────────────────────────────────────────────────


def test_failure_collapses_error_to_one_line():
    """Errors render in the app; a multi-line traceback there is unreadable."""
    r = ConnectorResult.failure("boom\n  at line 3\n  at line 4")
    assert "\n" not in r.error
    assert r.error == "boom at line 3 at line 4"
    assert not r.ok


def test_failure_truncates_a_huge_error():
    r = ConnectorResult.failure("x" * 5000)
    assert len(r.error) <= 300


def test_success_carries_count_and_detail():
    r = ConnectorResult.success(records_written=16, releases=16)
    assert r.ok and r.records_written == 16
    assert r.detail["releases"] == 16


def test_skipped_is_ok_with_skip_markers():
    r = ConnectorResult.skipped("set FLY_APP or DEPLOYMENT_CONFIGURATION_ID")
    assert r.ok
    assert r.error == ""
    assert r.records_written == 0
    assert r.detail["skipped"] is True
    assert "FLY_APP" in r.detail["skip_reason"]


# ── hybrid ingestion: push supplies latency, verify supplies liveness ───────


def test_push_does_not_refresh_freshness():
    """The load-bearing rule of the hybrid model.

    If a webhook delivery reset the staleness clock, a source that went quiet —
    because it genuinely had nothing to say, OR because deliveries silently
    stopped — would be indistinguishable from a healthy one. That is the 88-day
    SSE silence exactly.
    """
    s = ConnectorStatus(
        connector_name="github",
        ingestion_mode="hybrid",
        last_success_at=_ago(hours=9),   # last VERIFY, long ago
        last_push_at=_ago(seconds=30),   # a delivery arrived just now
        stale_after_seconds=2700,
    )
    assert s.freshness(now=NOW).state == "stale"


def test_verify_refreshes_even_when_push_is_silent():
    """A quiet webhook is not a problem when verification is current."""
    s = ConnectorStatus(
        connector_name="github",
        ingestion_mode="hybrid",
        last_success_at=_ago(minutes=5),
        last_push_at=_ago(days=7),
        stale_after_seconds=2700,
    )
    assert s.freshness(now=NOW).state == "fresh"


def test_hybrid_connector_still_declares_a_verify_interval():
    """A push source is never exempt: unfalsifiable freshness is not freshness."""
    # Hourly verify under hybrid — generous, because push supplies currency.
    assert stale_after_for(3600) == 10800


def test_ingestion_mode_defaults_to_poll():
    """Assume a connector cannot receive push until it says otherwise."""
    assert ConnectorStatus(connector_name="fly").ingestion_mode == "poll"


def test_push_only_is_not_an_offered_mode():
    """A mode nobody can use safely is a mode worth not offering."""
    assert "push" not in INGESTION_MODES
    assert set(INGESTION_MODES) == {"poll", "hybrid"}


def test_last_push_at_is_recorded_in_the_snapshot():
    """Recorded but never used for freshness — it makes a dead webhook visible."""
    fields = ConnectorStatus(
        connector_name="github", ingestion_mode="hybrid", last_push_at="2026-09-02T00:00:00+00:00"
    ).to_entity_fields()
    assert fields["last_push_at"] == "2026-09-02T00:00:00+00:00"
    assert fields["ingestion_mode"] == "hybrid"
