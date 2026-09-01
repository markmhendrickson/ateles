"""Focused tests for quota-aware bundled-plan harness selection."""

from __future__ import annotations

import sys
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
    monkeypatch.delenv("APIS_STAGE_MIN_TIER", raising=False)
    for _provider in harness_router.PROVIDERS:
        monkeypatch.delenv(f"APIS_HARNESS_MODELS_{_provider.upper()}", raising=False)
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
        harness_router.provider_candidates(_available(), now=100.0)[0]
        for _ in range(3)
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


def test_capacity_cooldown_removes_provider_until_expiry(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_COOLDOWN_SECONDS", "30")
    harness_router.cool_down("claude", now=100.0)
    assert "claude" not in harness_router.provider_candidates(
        _available(), now=129.9
    )
    assert "claude" in harness_router.provider_candidates(
        _available(), now=130.0
    )


def test_missing_binary_is_not_eligible() -> None:
    available = _available()
    available["codex"] = None
    assert "codex" not in harness_router.provider_candidates(available, now=100.0)


def test_operator_order_is_respected_and_deduplicated(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_PROVIDERS", "cursor,claude,cursor,unknown"
    )
    assert harness_router.configured_providers() == ["cursor", "claude"]
    assert harness_router.provider_candidates(_available(), now=100.0)[0] == "cursor"


def test_invalid_headroom_json_fails_open_to_equal_weights(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", "not-json")
    assert harness_router.configured_headroom() == {
        "claude": 1.0,
        "codex": 1.0,
        "cursor": 1.0,
    }


def test_headroom_file_can_be_refreshed_without_restart(
    monkeypatch, tmp_path
) -> None:
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


# ── Per-model fallback within a provider (2026-09-01 outage) ──────────────────
# On that day Cursor's third-party model bucket read 100% while its native
# bucket read 24%. Verified by hand in the same minute: composer-2.5 and
# cursor-grok-4.6-low answered normally while claude-opus-5-thinking-high
# returned "You've hit your usage limit for Opus". The router must therefore
# retire a MODEL, not a plan.


def _cursor_only(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "cursor")


def test_exhausted_model_does_not_retire_the_whole_provider(monkeypatch) -> None:
    _cursor_only(monkeypatch)
    monkeypatch.setenv(
        "APIS_HARNESS_MODELS_CURSOR", "opus:strong,grok:mid,composer:basic"
    )
    harness_router.cool_down("cursor", model="opus", now=100.0)

    pairs = harness_router.candidate_pairs(_available(), now=101.0)

    assert ("cursor", "opus") not in pairs
    assert pairs == [("cursor", "grok"), ("cursor", "composer")]
    # The provider itself is still usable — the regression that caused the outage
    # was reporting it as entirely cooling.
    assert "cursor" not in harness_router.cooling_providers(now=101.0)


def test_provider_reports_cooling_only_when_every_model_is_out(monkeypatch) -> None:
    _cursor_only(monkeypatch)
    monkeypatch.setenv("APIS_HARNESS_MODELS_CURSOR", "opus:strong,grok:mid")
    harness_router.cool_down("cursor", model="opus", now=100.0)
    harness_router.cool_down("cursor", model="grok", now=100.0)

    assert harness_router.candidate_pairs(_available(), now=101.0) == []
    assert "cursor" in harness_router.cooling_providers(now=101.0)


def test_models_are_exhausted_within_provider_before_crossing(monkeypatch) -> None:
    """Provider loyalty: a weaker model on the chosen plan outranks a hop."""
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "cursor,codex")
    monkeypatch.setenv("APIS_HARNESS_MODELS_CURSOR", "opus:strong,composer:basic")
    monkeypatch.setenv("APIS_HARNESS_MODELS_CODEX", "")

    pairs = harness_router.candidate_pairs(
        _available(), preferred="cursor", now=100.0
    )

    assert pairs == [("cursor", "opus"), ("cursor", "composer")]


def test_ambient_default_provider_yields_one_flagless_candidate(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex")
    monkeypatch.setenv("APIS_HARNESS_MODELS_CODEX", "")

    assert harness_router.candidate_pairs(_available(), now=100.0) == [("codex", "")]


# ── Per-stage capability floors ───────────────────────────────────────────────


def test_floor_excludes_models_beneath_the_stage_minimum(monkeypatch) -> None:
    _cursor_only(monkeypatch)
    monkeypatch.setenv(
        "APIS_HARNESS_MODELS_CURSOR", "opus:strong,grok:mid,composer:basic"
    )

    strong = harness_router.candidate_pairs(
        _available(), min_tier=harness_router.TIER_STRONG, now=100.0
    )

    assert strong == [("cursor", "opus")]


def test_stage_with_unmeetable_floor_returns_nothing_rather_than_downgrading(
    monkeypatch,
) -> None:
    """The safety property: refuse, never quietly review on a weak model."""
    _cursor_only(monkeypatch)
    monkeypatch.setenv("APIS_HARNESS_MODELS_CURSOR", "grok:mid,composer:basic")

    assert (
        harness_router.candidate_pairs(
            _available(), min_tier=harness_router.TIER_STRONG, now=100.0
        )
        == []
    )
    # ... while a cheap stage still runs on exactly the same capacity.
    assert harness_router.candidate_pairs(
        _available(), min_tier=harness_router.TIER_BASIC, now=100.0
    )


def test_stage_floor_is_operator_overridable_without_a_redeploy(monkeypatch) -> None:
    monkeypatch.setenv("APIS_STAGE_MIN_TIER", '{"routing": "strong"}')
    assert harness_router.stage_floor("routing") == harness_router.TIER_STRONG
    # Unconfigured stages keep their built-in default.
    assert harness_router.stage_floor("security") == harness_router.TIER_STRONG


def test_malformed_stage_floor_config_falls_back_to_defaults(monkeypatch) -> None:
    monkeypatch.setenv("APIS_STAGE_MIN_TIER", "not-json")
    assert harness_router.stage_floor("routing") == harness_router.TIER_BASIC


# ── The ratchet ───────────────────────────────────────────────────────────────


def test_completing_agent_may_raise_the_next_stages_floor() -> None:
    assert (
        harness_router.effective_floor("routing", "strong")
        == harness_router.TIER_STRONG
    )


def test_completing_agent_may_not_lower_the_floor() -> None:
    """An agent must not be able to cheapen the review of its own work."""
    assert (
        harness_router.effective_floor("security", "basic")
        == harness_router.TIER_STRONG
    )


@pytest.mark.parametrize("junk", [None, "", "nonsense", True, 2.5, {"a": 1}])
def test_unparseable_requested_tier_leaves_the_static_floor_intact(junk) -> None:
    assert (
        harness_router.effective_floor("security", junk)
        == harness_router.TIER_STRONG
    )


def test_unknown_model_is_not_assumed_review_grade(monkeypatch) -> None:
    """A bare name defaults to mid, so it cannot satisfy a strong floor."""
    _cursor_only(monkeypatch)
    monkeypatch.setenv("APIS_HARNESS_MODELS_CURSOR", "some-new-model")

    assert (
        harness_router.candidate_pairs(
            _available(), min_tier=harness_router.TIER_STRONG, now=100.0
        )
        == []
    )
