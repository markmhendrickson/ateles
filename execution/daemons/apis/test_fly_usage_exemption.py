"""A dispatch that runs on Fly is not gated by the Mac's usage reading.

Operator ruling 2026-10-15: the weekly usage ceiling, the pace line and the
refusals derived from the Mac's usage probe (a missing, stale or failed
reading) describe the Mac's own provider logins. A Fly runner uses its own,
separately held login, so none of them applies to a Fly-bound dispatch, for
any provider. Cooling windows and every Fly-side cap still apply, and a
dispatch that is exempt for Fly but then falls back to the Mac is checked
against the local gate again. Local dispatches are gated exactly as before.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import fly_runner  # noqa: E402
import harness_router as hr  # noqa: E402
import skill_runner  # noqa: E402

PROVIDERS = ("claude", "codex", "cursor")
FLY = frozenset(hr.FRONTIER_PROVIDERS)
BINARIES = {"claude": "/bin/claude", "codex": "/bin/codex", "cursor": "/bin/cursor-agent"}


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_USAGE_GATE", "on")
    monkeypatch.setenv("APIS_USAGE_PROBE", "off")
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "no-headroom.json"))
    # Claude is not enabled for local dispatch; the gate covers all three.
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex,cursor")
    monkeypatch.setenv("APIS_USAGE_GATED_PROVIDERS", "claude,codex,cursor")
    for name in ("APIS_HARNESS_HEADROOM", "APIS_USAGE_STALE_SECONDS",
                 "APIS_USAGE_WEEKLY_CEILING_PERCENT", "APIS_USAGE_PACE_BURST_PERCENT",
                 "APIS_HARNESS_MIN_HEADROOM", "APIS_FLY_RUNNER_CLASSES",
                 "APIS_FLY_CLAUDE_MACHINE_ID"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    hr.reset_state()
    yield
    hr.reset_state()


def _reading(provider: str, condition: str) -> None:
    """Put the Mac's usage reading for *provider* into *condition*."""
    now = time.time()
    if condition == "over-ceiling":
        hr.record_usage(provider, [
            {"name": "five_hour", "used_percent": 10.0,
             "resets_at": hr._iso_from_wall(now + 3600)},
            {"name": "weekly_all", "used_percent": 95.0,
             "resets_at": hr._iso_from_wall(now + 3 * 86400)},
        ], observed_at=now)  # fmt: skip
    elif condition == "stale":
        hr.record_usage(provider, [
            {"name": "weekly_all", "used_percent": 5.0,
             "resets_at": hr._iso_from_wall(now + 3 * 86400)},
        ], observed_at=now - 3 * 3600)  # fmt: skip
    elif condition == "failed-probe":
        hr.record_probe_failure(provider, "not logged in", observed_at=now)
    else:
        raise AssertionError(condition)


CONDITIONS = ("over-ceiling", "stale", "failed-probe")


@pytest.mark.parametrize("condition", CONDITIONS)
@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_fly_bound_dispatch_is_not_refused_by_the_macs_reading(provider, condition):
    _reading(provider, condition)
    # Bound for the Mac: refused, exactly as today.
    assert hr.provider_exclusion_reason(provider, BINARIES) is not None
    assert hr.provider_candidates(BINARIES, preferred=provider) == []
    # Bound for Fly: the Mac's reading does not apply.
    assert hr.provider_exclusion_reason(provider, BINARIES, fly_bound=FLY) is None
    assert hr.provider_candidates(BINARIES, preferred=provider, fly_bound=FLY) == [provider]


@pytest.mark.parametrize("condition", CONDITIONS)
def test_an_unpinned_fly_bound_dispatch_is_not_refused_by_the_macs_reading(condition):
    for provider in ("codex", "cursor"):
        _reading(provider, condition)
    assert hr.provider_candidates(BINARIES) == []
    assert hr.usage_gate_refusals_all(BINARIES)  # the local refusal, as today
    assert set(hr.provider_candidates(BINARIES, fly_bound=FLY)) == {"codex", "cursor"}
    assert hr.usage_gate_refusals_all(BINARIES, fly_bound=FLY) is None


def test_a_cooling_window_still_applies_to_a_fly_bound_dispatch():
    hr.record_cooling("codex", time.time() + 3600, reason="rate limit")
    assert hr.provider_candidates(BINARIES, preferred="codex", fly_bound=FLY) == []


def test_mac_read_headroom_does_not_apply_but_an_operator_override_does(monkeypatch):
    hr.record_exhausted("codex", time.time() + 3600)  # the Mac's login is exhausted
    assert hr.provider_candidates(BINARIES, preferred="codex") == []
    assert hr.provider_candidates(BINARIES, preferred="codex", fly_bound=FLY) == ["codex"]
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"cursor": 0.0}')  # the operator's choice
    assert hr.provider_candidates(BINARIES, preferred="cursor", fly_bound=FLY) == []


# ── Claude: selectable for Fly only, the local list unchanged ───────────────


@pytest.fixture
def claude_on_fly(monkeypatch):
    assert "claude" in fly_runner.LOGIN_PROVIDERS  # Claude runs on a Fly login machine
    monkeypatch.setenv("APIS_FLY_CLAUDE_MACHINE_ID", "m-claude")
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")


def test_claude_is_selectable_for_fly_but_not_locally(claude_on_fly):
    assert hr.configured_providers() == ["codex", "cursor"]  # unchanged
    assert skill_runner._fly_routing(None, None) == (frozenset(), ())
    bound, extra = skill_runner._fly_routing("fly", None)
    assert extra == ("claude",) and bound == FLY
    assert skill_runner._fly_routing(None, "carry_forward_check")[1] == ("claude",)
    assert "claude" not in hr.provider_candidates(BINARIES)
    assert "claude" in hr.provider_candidates(BINARIES, fly_bound=bound, fly_extra=extra)
    assert hr.configured_providers() == ["codex", "cursor"]


def test_claude_is_not_selectable_for_fly_without_its_login_machine(claude_on_fly, monkeypatch):
    monkeypatch.delenv("APIS_FLY_CLAUDE_MACHINE_ID")
    assert skill_runner._fly_routing("fly", None)[1] == ()


def test_claude_is_not_selectable_when_fly_does_not_run_it_on_a_login_machine(monkeypatch):
    monkeypatch.setattr(fly_runner, "LOGIN_PROVIDERS", frozenset({"codex", "cursor"}))
    monkeypatch.setenv("APIS_FLY_CLAUDE_MACHINE_ID", "m-claude")
    assert skill_runner._fly_routing("fly", None)[1] == ()


# ── The fallback to the Mac is gated locally again ──────────────────────────


@pytest.mark.parametrize("condition", CONDITIONS)
def test_a_fallback_to_the_mac_is_checked_against_the_local_gate(monkeypatch, condition):
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: dict(BINARIES))
    _reading("codex", condition)
    assert skill_runner._local_fallback_refusal("codex", pinned=True) is not None


def test_a_fallback_for_a_fly_only_provider_is_refused_on_the_mac(monkeypatch):
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: dict(BINARIES))
    hr.record_usage("claude", [{"name": "weekly_all", "used_percent": 1.0,
                                "resets_at": hr._iso_from_wall(time.time() + 86400)}])
    reason = skill_runner._local_fallback_refusal("claude", pinned=False)
    assert reason and "not enabled for local dispatch" in reason


def test_a_fallback_with_a_good_local_reading_is_allowed(monkeypatch):
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: dict(BINARIES))
    hr.record_usage("codex", [{"name": "weekly_all", "used_percent": 1.0,
                               "resets_at": hr._iso_from_wall(time.time() + 86400)}])
    assert skill_runner._local_fallback_refusal("codex", pinned=False) is None


# ── run_skill: the exemption end to end, with the attempt stubbed ───────────


def _stub_attempts(monkeypatch, *, on_fly: bool):
    calls = []

    async def once(skill, prompt, *, provider, **kw):
        calls.append((provider, kw))
        return skill_runner.SkillResult(
            skill, True, 0, "ok", "", provider=provider,
            remote_run_id="fly-abc" if on_fly else "",
        )  # fmt: skip

    monkeypatch.setattr(skill_runner, "_run_skill_once", once)
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: dict(BINARIES))
    return calls


@pytest.mark.parametrize("provider", PROVIDERS)
def test_run_skill_sends_a_gated_provider_to_fly_and_reports_the_exemption(monkeypatch,
                                                                          provider):  # fmt: skip
    calls = _stub_attempts(monkeypatch, on_fly=True)
    _reading(provider, "over-ceiling")
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius",
                                                provider=provider, runner="fly"))  # fmt: skip
    assert result.ok and [c[0] for c in calls] == [provider]
    assert calls[0][1]["local_fallback_check"] is not None  # a fallback re-checks
    assert "usage gate paced" in result.usage_gate_exempted


@pytest.mark.parametrize("provider", ("codex", "cursor"))
def test_run_skill_still_refuses_the_same_dispatch_bound_for_the_mac(monkeypatch, provider):
    calls = _stub_attempts(monkeypatch, on_fly=False)
    _reading(provider, "over-ceiling")
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius",
                                                provider=provider, runner="local"))  # fmt: skip
    assert not result.ok and calls == []


def test_run_skill_bound_for_the_mac_gets_no_fallback_exemption(monkeypatch):
    calls = _stub_attempts(monkeypatch, on_fly=False)
    hr.record_usage("codex", [{"name": "weekly_all", "used_percent": 1.0,
                               "resets_at": hr._iso_from_wall(time.time() + 86400)}])
    result = asyncio.run(skill_runner.run_skill("lanius", "task", role="lanius",
                                                provider="codex"))  # fmt: skip
    assert result.ok and calls[0][1]["local_fallback_check"] is None
    assert result.usage_gate_exempted == ""
