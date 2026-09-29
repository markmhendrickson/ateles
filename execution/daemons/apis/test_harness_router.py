"""Focused tests for quota-aware bundled-plan harness selection."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import harness_router  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_router(monkeypatch, tmp_path):
    monkeypatch.delenv("APIS_HARNESS_PROVIDERS", raising=False)
    monkeypatch.delenv("APIS_HARNESS_HEADROOM", raising=False)
    monkeypatch.delenv("APIS_HARNESS_MIN_HEADROOM", raising=False)
    monkeypatch.delenv("APIS_HARNESS_COOLDOWN_SECONDS", raising=False)
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "missing-headroom.json")
    )
    harness_router.reset_state()
    yield
    harness_router.reset_state()


def _available() -> dict[str, str]:
    return {
        "claude": "/bin/claude",
        "codex": "/bin/codex",
        "cursor": "/bin/cursor-agent",
    }


def test_equal_headroom_round_robins_across_three_providers() -> None:
    first_choices = [
        harness_router.provider_candidates(_available(), now=100.0)[0] for _ in range(3)
    ]
    assert first_choices == ["claude", "codex", "cursor"]


def test_highest_headroom_receives_first_dispatch(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        '{"claude": 0.1, "codex": 0.9, "cursor": 0.4}',
    )
    candidates = harness_router.provider_candidates(_available(), now=100.0)
    assert candidates == ["codex", "cursor", "claude"]


def test_provider_at_or_below_minimum_is_held_out(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        '{"claude": 0.05, "codex": 0.8, "cursor": 0.0}',
    )
    assert harness_router.provider_candidates(_available(), now=100.0) == ["codex"]


def test_usable_names_and_candidates_share_eligibility(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        '{"claude": 0.05, "codex": 0.8, "cursor": 0.0}',
    )

    assert harness_router.usable_provider_names(_available(), now=100.0) == {"codex"}
    assert harness_router.provider_candidates(_available(), now=100.0) == ["codex"]


def test_provider_exclusion_reason_names_headroom_floor(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        '{"claude": 1.0, "codex": 0.0, "cursor": 1.0}',
    )

    assert (
        harness_router.provider_exclusion_reason("codex", _available(), now=100.0)
        == "headroom=0.000 is at or below minimum=0.050"
    )


def test_provider_exclusion_reason_names_missing_binary() -> None:
    available = _available()
    available["codex"] = None

    assert (
        harness_router.provider_exclusion_reason("codex", available, now=100.0)
        == "binary unavailable"
    )


def test_provider_exclusion_reason_names_cooldown(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_COOLDOWN_SECONDS", "30")
    harness_router.cool_down("codex", now=100.0)

    assert (
        harness_router.provider_exclusion_reason("codex", _available(), now=101.0)
        == "cooling down"
    )


def test_capacity_cooldown_removes_provider_until_expiry(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_COOLDOWN_SECONDS", "30")
    harness_router.cool_down("claude", now=100.0)
    assert "claude" not in harness_router.provider_candidates(_available(), now=129.9)
    assert "claude" in harness_router.provider_candidates(_available(), now=130.0)


def test_missing_binary_is_not_eligible() -> None:
    available = _available()
    available["codex"] = None
    assert "codex" not in harness_router.provider_candidates(available, now=100.0)


def test_operator_order_is_respected_and_deduplicated(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "cursor,claude,cursor,unknown")
    assert harness_router.configured_providers() == ["cursor", "claude"]
    assert harness_router.provider_candidates(_available(), now=100.0)[0] == "cursor"


def test_invalid_headroom_json_fails_open_to_equal_weights(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", "not-json")
    assert harness_router.configured_headroom() == {
        "claude": 1.0,
        "codex": 1.0,
        "cursor": 1.0,
        "claude-local": 1.0,
    }


def test_headroom_file_can_be_refreshed_without_restart(monkeypatch, tmp_path) -> None:
    headroom_file = tmp_path / "headroom.json"
    headroom_file.write_text(
        '{"claude": 0.1, "codex": 0.9, "cursor": 0.4}',
        encoding="utf-8",
    )
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
    assert harness_router.provider_candidates(_available(), now=100.0)[0] == "codex"

    headroom_file.write_text(
        '{"claude": 0.9, "codex": 0.1, "cursor": 0.4}',
        encoding="utf-8",
    )
    harness_router.reset_state()
    assert harness_router.provider_candidates(_available(), now=101.0)[0] == "claude"


def test_malformed_headroom_file_falls_back_to_env(monkeypatch, tmp_path) -> None:
    headroom_file = tmp_path / "headroom.json"
    headroom_file.write_text("not-json", encoding="utf-8")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(headroom_file))
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        '{"claude": 0.1, "codex": 0.9, "cursor": 0.4}',
    )
    assert harness_router.provider_candidates(_available(), now=100.0)[0] == "codex"


# ── Live plan usage and expiring overrides (ent_0c213a308b74c1216b241a6e) ──


def _iso(wall: float) -> str:
    return harness_router._iso_from_wall(wall)


def _headroom_file(tmp_path, monkeypatch, content: dict, *, mtime: float) -> None:
    path = tmp_path / "harness-headroom.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(path))


def _usage_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "harness-usage.json"
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(path))
    return path


def test_live_usage_after_reset_lifts_a_stale_hand_set_zero(
    tmp_path, monkeypatch
) -> None:
    """The 2026-09-28 incident: claude=0.0 hand-set at exhaustion kept every
    Claude dispatch refused after the weekly reset, while real usage was
    weekly 3% / 5-hour 8%. A usage observation made after the file was written
    must win."""
    now = time.time()
    _headroom_file(
        tmp_path, monkeypatch, {"claude": 0.0, "codex": 0.9}, mtime=now - 86400
    )
    _usage_file(tmp_path, monkeypatch)
    harness_router.record_usage(
        "claude",
        [
            {"name": "weekly", "used_percent": 3, "resets_at": _iso(now + 6 * 86400)},
            {"name": "five_hour", "used_percent": 8, "resets_at": _iso(now + 3600)},
        ],
        observed_at=now - 60,
    )

    assert harness_router.configured_headroom(now_wall=now)["claude"] == pytest.approx(
        0.92
    )
    assert harness_router.provider_exclusion_reason("claude", _available()) is None
    assert "claude" in harness_router.provider_candidates(_available(), now=100.0)


def test_recorded_exhaustion_holds_out_a_provider_the_file_calls_roomy(
    tmp_path, monkeypatch
) -> None:
    """The opposite incident: the file said codex=0.91 while Codex was
    exhausted, so selection routed to a provider with no quota."""
    now = time.time()
    _headroom_file(
        tmp_path, monkeypatch, {"claude": 0.86, "codex": 0.91}, mtime=now - 3600
    )
    _usage_file(tmp_path, monkeypatch)
    harness_router.record_exhausted("codex", now + 4 * 86400, observed_at=now - 60)

    reason = harness_router.provider_exclusion_reason("codex", _available())
    assert reason is not None and "headroom=0.000" in reason
    assert "codex" not in harness_router.provider_candidates(_available(), now=100.0)


def test_expired_exhaustion_entry_no_longer_blocks_selection(
    tmp_path, monkeypatch
) -> None:
    """An exhaustion record whose reset has passed restores the provider with
    no edit, even over an older hand-set zero."""
    now = time.time()
    _headroom_file(tmp_path, monkeypatch, {"claude": 0.0}, mtime=now - 7 * 86400)
    _usage_file(tmp_path, monkeypatch)
    harness_router.record_exhausted("claude", now - 60, observed_at=now - 86400)

    assert harness_router.configured_headroom(now_wall=now)["claude"] == 1.0
    assert harness_router.provider_exclusion_reason("claude", _available()) is None


def test_override_with_future_cooldown_until_blocks_and_expires(
    tmp_path, monkeypatch
) -> None:
    now = time.time()
    _headroom_file(
        tmp_path,
        monkeypatch,
        {"claude": {"headroom": 0.0, "cooldown_until": _iso(now + 3600)}},
        mtime=now,
    )
    assert harness_router.configured_headroom(now_wall=now)["claude"] == 0.0
    assert harness_router.provider_exclusion_reason("claude", _available()) is not None
    assert harness_router.configured_headroom(now_wall=now + 7200)["claude"] == 1.0


def test_dated_override_beats_live_usage_until_it_expires(
    tmp_path, monkeypatch
) -> None:
    now = time.time()
    _headroom_file(
        tmp_path,
        monkeypatch,
        {"codex": {"headroom": 0.0, "cooldown_until": _iso(now + 3600)}},
        mtime=now - 600,
    )
    _usage_file(tmp_path, monkeypatch)
    harness_router.record_usage(
        "codex", [{"name": "weekly", "used_percent": 20}], observed_at=now - 60
    )
    assert harness_router.configured_headroom(now_wall=now)["codex"] == 0.0
    assert harness_router.configured_headroom(now_wall=now + 1800)["codex"] == 0.0
    later = now + 7200
    monkeypatch.setenv("APIS_HARNESS_USAGE_MAX_AGE_SECONDS", "86400")
    assert harness_router.configured_headroom(now_wall=later)["codex"] == pytest.approx(
        0.8
    )


def test_manual_override_never_expires_and_beats_live_usage(
    tmp_path, monkeypatch
) -> None:
    now = time.time()
    _headroom_file(
        tmp_path,
        monkeypatch,
        {
            "cursor": {
                "headroom": 0.0,
                "cooldown_until": _iso(now - 3600),
                "cooldown_reason": "manual",
            }
        },
        mtime=now - 7200,
    )
    _usage_file(tmp_path, monkeypatch)
    harness_router.record_usage(
        "cursor", [{"name": "monthly", "used_percent": 1}], observed_at=now
    )
    assert harness_router.configured_headroom(now_wall=now)["cursor"] == 0.0


def test_undated_override_written_after_the_observation_still_wins(
    tmp_path, monkeypatch
) -> None:
    now = time.time()
    _usage_file(tmp_path, monkeypatch)
    harness_router.record_usage(
        "claude", [{"name": "weekly", "used_percent": 3}], observed_at=now - 3600
    )
    _headroom_file(tmp_path, monkeypatch, {"claude": 0.2}, mtime=now - 60)
    assert harness_router.configured_headroom(now_wall=now)["claude"] == 0.2


def test_stale_unreset_observation_carries_no_opinion(tmp_path, monkeypatch) -> None:
    now = time.time()
    _usage_file(tmp_path, monkeypatch)
    harness_router.record_usage(
        "claude",
        [{"name": "weekly", "used_percent": 97, "resets_at": _iso(now + 86400)}],
        observed_at=now - 2 * 86400,
    )
    assert harness_router.live_headroom("claude", now_wall=now) is None
    assert harness_router.configured_headroom(now_wall=now)["claude"] == 1.0


def test_window_past_its_reset_counts_as_unused(tmp_path, monkeypatch) -> None:
    now = time.time()
    _usage_file(tmp_path, monkeypatch)
    harness_router.record_usage(
        "claude",
        [
            {"name": "weekly", "used_percent": 100, "resets_at": _iso(now - 60)},
            {"name": "five_hour", "used_percent": 10, "resets_at": _iso(now + 600)},
        ],
        observed_at=now - 3600,
    )
    assert harness_router.live_headroom("claude", now_wall=now) == pytest.approx(0.9)


def test_legacy_bare_headroom_without_usage_is_unchanged(tmp_path, monkeypatch) -> None:
    now = time.time()
    _headroom_file(tmp_path, monkeypatch, {"cursor": 0.0, "codex": 0.5}, mtime=now)
    values = harness_router.configured_headroom(now_wall=now)
    assert values["cursor"] == 0.0
    assert values["codex"] == 0.5
    assert values["claude"] == 1.0


def test_record_usage_preserves_other_providers(tmp_path, monkeypatch) -> None:
    path = _usage_file(tmp_path, monkeypatch)
    now = time.time()
    harness_router.record_exhausted("codex", now + 3600)
    harness_router.record_usage("claude", [{"name": "weekly", "used_percent": 5}])
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert set(stored) == {"claude", "codex"}
    assert stored["codex"]["exhausted_until"] is not None
    assert not list(tmp_path.glob(".harness-usage.json.*.tmp"))


def test_record_usage_rejects_unknown_provider_and_bad_window(
    tmp_path, monkeypatch
) -> None:
    _usage_file(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        harness_router.record_usage("gemini", [{"name": "w", "used_percent": 1}])
    with pytest.raises(ValueError):
        harness_router.record_usage("claude", [{"name": "w", "used_percent": "lots"}])


def test_live_exhaustion_beats_undated_override_even_after_a_later_file_edit(
    tmp_path, monkeypatch
) -> None:
    """Editing another provider's line bumps the file time; that must not
    resurrect an exhausted provider's stale roomy value."""
    now = time.time()
    _usage_file(tmp_path, monkeypatch)
    harness_router.record_exhausted("codex", now + 4 * 86400, observed_at=now - 3600)
    _headroom_file(tmp_path, monkeypatch, {"codex": 0.8, "claude": 0.5}, mtime=now - 60)
    assert harness_router.configured_headroom(now_wall=now)["codex"] == 0.0


def test_concurrent_recorders_do_not_drop_entries(tmp_path, monkeypatch) -> None:
    import threading

    path = _usage_file(tmp_path, monkeypatch)
    providers = ["claude", "codex", "cursor"] * 10
    threads = [
        threading.Thread(
            target=harness_router.record_usage,
            args=(provider, [{"name": "weekly", "used_percent": 1}]),
        )
        for provider in providers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {
        "claude",
        "codex",
        "cursor",
    }


def test_headroom_resolution_names_the_winning_source(tmp_path, monkeypatch) -> None:
    """``headroom_resolution`` labels which precedence tier produced each value,
    and agrees with ``configured_headroom`` on the value itself."""
    now = time.time()
    _usage_file(tmp_path, monkeypatch)
    harness_router.record_exhausted("codex", now + 3600, observed_at=now - 60)
    _headroom_file(
        tmp_path,
        monkeypatch,
        {
            "claude": {"headroom": 0.0, "cooldown_reason": "manual"},
            "codex": 1.0,
            "cursor": {"headroom": 0.0, "cooldown_until": _iso_after(now, 3600)},
        },
        mtime=now - 10,
    )
    resolved = harness_router.headroom_resolution(now_wall=now)
    assert resolved["claude"] == (0.0, harness_router.HEADROOM_SOURCE_MANUAL_OVERRIDE)
    # A live 0.0 is never outvoted by a hand-set 1.0.
    assert resolved["codex"] == (0.0, harness_router.HEADROOM_SOURCE_LIVE_USAGE)
    assert resolved["cursor"] == (0.0, harness_router.HEADROOM_SOURCE_DATED_OVERRIDE)
    assert harness_router.configured_headroom(now_wall=now) == {
        provider: value for provider, (value, _s) in resolved.items()
    }


def test_headroom_resolution_labels_undated_and_default(
    tmp_path, monkeypatch
) -> None:
    now = time.time()
    _headroom_file(tmp_path, monkeypatch, {"codex": 0.4}, mtime=now)
    resolved = harness_router.headroom_resolution(now_wall=now)
    assert resolved["codex"] == (0.4, harness_router.HEADROOM_SOURCE_UNDATED_OVERRIDE)
    assert resolved["claude"] == (1.0, harness_router.HEADROOM_SOURCE_DEFAULT)
    assert harness_router.headroom_override_origin() == "file"


def _iso_after(now: float, seconds: float) -> str:
    return harness_router._iso_from_wall(now + seconds)
