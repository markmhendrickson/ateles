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


def test_routing_state_reports_every_provider_with_its_headroom(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        '{"claude": 0.15, "codex": 0.9, "cursor": 0.4}',
    )
    described = harness_router.describe_routing_state(now=100.0)
    assert "claude=0.15" in described
    assert "codex=0.9" in described
    assert "cursor=0.4" in described


def test_routing_state_names_a_cooling_provider(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_COOLDOWN_SECONDS", "30")
    harness_router.cool_down("claude", now=100.0)
    assert "cooling: claude" in harness_router.describe_routing_state(now=110.0)


def test_routing_state_renders_no_cooldown_explicitly() -> None:
    assert "cooling: none" in harness_router.describe_routing_state(now=100.0)


def test_routing_state_reflects_operator_provider_order(monkeypatch) -> None:
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "cursor,claude")
    assert "providers: cursor,claude" in harness_router.describe_routing_state(
        now=100.0
    )
