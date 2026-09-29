"""Usage snapshot trust, live feeder and weekly pacing.

Tasks ent_63b22ea0d13109238c0a00bf (validate + feed the snapshot) and
ent_1e50b009d88b08e47963f847 (pace dispatch to 60% of the weekly allowance).
The incident these pin: on 2026-09-29 over 60% of the weekly Claude allowance
went in under a day while the snapshot read 20%, observed seven hours earlier.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import harness_router as hr  # noqa: E402
import skill_runner  # noqa: E402
import usage_probe  # noqa: E402

WEEK = hr.WEEK_SECONDS
# Fixed "now": Tuesday 2026-09-29 16:00 UTC, 22h into a week that reset
# 2026-09-28 18:00 UTC and resets again 2026-10-05 18:00 UTC.
RESET = 1791223200.0  # 2026-10-05T18:00:00Z
START = RESET - WEEK
NOW = START + 22 * 3600.0


@pytest.fixture(autouse=True)
def _gate_env(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_USAGE_GATE", "on")
    monkeypatch.setenv("APIS_USAGE_PROBE", "on")
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "no-headroom.json"))
    for name in (
        "APIS_HARNESS_PROVIDERS", "APIS_HARNESS_HEADROOM", "APIS_USAGE_STALE_SECONDS",
        "APIS_USAGE_REFRESH_SECONDS", "APIS_USAGE_WEEKLY_CEILING_PERCENT",
        "APIS_USAGE_PACE_BURST_PERCENT", "APIS_USAGE_GATED_PROVIDERS",
        "APIS_HARNESS_MIN_HEADROOM",
    ):
        monkeypatch.delenv(name, raising=False)
    hr.reset_state()
    yield
    hr.reset_state()


def _record(weekly: float, *, observed: float = NOW, five_hour: float = 10.0,
            resets: float = RESET) -> None:
    hr.record_usage(
        "claude",
        [
            {"name": "five_hour", "used_percent": five_hour,
             "resets_at": hr._iso_from_wall(observed + 3600)},
            {"name": "weekly_all", "used_percent": weekly,
             "resets_at": hr._iso_from_wall(resets)},
        ],
        observed_at=observed,
    )


# --- record_usage rejects garbage; a malformed entry is unknown, not exhausted


@pytest.mark.parametrize(
    "window",
    [
        {"name": "weekly_all", "used_percent": float("inf")},
        {"name": "weekly_all", "used_percent": float("nan")},
        {"name": "weekly_all", "used_percent": 100.5},
        {"name": "weekly_all", "used_percent": -1},
        {"name": "", "used_percent": 20},
        {"name": "   ", "used_percent": 20},
        {"used_percent": 20},
        {"name": "weekly_all", "used_percent": True},
        {"name": "weekly_all", "used_percent": 20, "resets_at": "not-a-time"},
    ],
)
def test_record_usage_rejects_malformed_window(window) -> None:
    with pytest.raises(ValueError):
        hr.record_usage("claude", [window])
    assert hr._read_json_object(hr._usage_path()) == {}


def test_record_usage_rejects_no_windows() -> None:
    with pytest.raises(ValueError):
        hr.record_usage("claude", [])


def test_malformed_entry_is_unknown_never_exhausted() -> None:
    """The 07:13Z write: an empty-named Infinity window made Claude headroom 0.0."""
    hr._usage_path().write_text(
        json.dumps({"claude": {
            "observed_at": hr._iso_from_wall(NOW),
            "windows": [{"name": "", "used_percent": float("inf")}],
            "exhausted_until": None,
        }})
    )
    assert hr.live_headroom("claude", now_wall=NOW) is None
    assert hr.configured_headroom(now_wall=NOW)["claude"] == 1.0


def test_named_infinite_window_is_unknown_not_exhausted() -> None:
    """Infinity used to clamp to "fully used"; a named window must not do that either."""
    hr._usage_path().write_text(
        json.dumps({"claude": {
            "observed_at": hr._iso_from_wall(NOW),
            "windows": [{"name": "weekly_all", "used_percent": float("inf"),
                         "resets_at": hr._iso_from_wall(RESET)}],
        }})
    )
    assert hr.live_headroom("claude", now_wall=NOW) is None
    assert hr.configured_headroom(now_wall=NOW)["claude"] == 1.0


def test_malformed_entry_refuses_gated_provider_by_name() -> None:
    hr._usage_path().write_text(
        json.dumps({"claude": {
            "observed_at": hr._iso_from_wall(NOW),
            "windows": [{"name": "", "used_percent": float("inf")}],
        }})
    )
    gate = hr.usage_gate("claude", now_wall=NOW)
    assert gate is not None and not gate.allowed and gate.code == hr.GATE_MALFORMED
    assert "malformed" in gate.message


# --- staleness refusal


def test_missing_reading_refuses_frontier_dispatch() -> None:
    gate = hr.usage_gate("claude", now_wall=NOW)
    assert gate is not None and not gate.allowed and gate.code == hr.GATE_MISSING


def test_stale_reading_refuses_with_distinct_message() -> None:
    _record(20.0, observed=NOW - 7 * 3600)  # the incident: 20%, seven hours old
    gate = hr.usage_gate("claude", now_wall=NOW)
    assert gate is not None and not gate.allowed and gate.code == hr.GATE_STALE
    assert gate.message.startswith("usage reading stale since ")
    assert hr.render_wall(NOW - 7 * 3600) in gate.message


def test_reading_just_inside_the_bound_is_accepted() -> None:
    _record(5.0, observed=NOW - 29 * 60)
    gate = hr.usage_gate("claude", now_wall=NOW)
    assert gate is not None and gate.allowed


def test_stale_bound_is_configurable(monkeypatch) -> None:
    _record(5.0, observed=NOW - 29 * 60)
    monkeypatch.setenv("APIS_USAGE_STALE_SECONDS", "600")
    gate = hr.usage_gate("claude", now_wall=NOW)
    assert gate is not None and gate.code == hr.GATE_STALE


def test_reading_whose_weekly_window_already_reset_is_stale() -> None:
    _record(30.0, observed=RESET - 60, resets=RESET)
    gate = hr.usage_gate("claude", now_wall=RESET + 5)
    assert gate is not None and gate.code == hr.GATE_STALE


def test_gate_off_and_ungated_providers_are_untouched(monkeypatch) -> None:
    assert hr.usage_gate("codex", now_wall=NOW) is None
    assert hr.usage_gate("claude-local", now_wall=NOW) is None
    monkeypatch.setenv("APIS_USAGE_GATE", "off")
    assert hr.usage_gate("claude", now_wall=NOW) is None


# --- pacing to the ceiling


def test_pace_line_allows_up_to_ceiling_times_elapsed_plus_burst() -> None:
    now = START + WEEK * 0.5  # line = 60*0.5 + 10 = 40
    _record(39.0, observed=now)
    ok = hr.usage_gate("claude", now_wall=now)
    assert ok is not None and ok.allowed
    assert ok.pace_percent == pytest.approx(40.0)
    _record(40.0, observed=now)
    over = hr.usage_gate("claude", now_wall=now)
    assert over is not None and not over.allowed and over.code == hr.GATE_PACED


def test_incident_replay_66_percent_a_day_in_is_refused() -> None:
    _record(66.0)
    gate = hr.usage_gate("claude", now_wall=NOW)
    assert gate is not None and not gate.allowed and gate.code == hr.GATE_PACED
    assert gate.weekly_used_percent == 66.0
    # At or above the ceiling nothing returns before the weekly reset.
    assert gate.returns_at == RESET
    assert "returns at" in gate.message


def test_paced_refusal_says_when_capacity_returns() -> None:
    now = START + WEEK * 0.25  # line = 15 + 10 = 25
    _record(40.0, observed=now)
    gate = hr.usage_gate("claude", now_wall=now)
    assert gate is not None and not gate.allowed
    # pace(e) = 60e + 10 reaches 40 at e = 0.5, plus the one-minute slack.
    assert gate.returns_at == pytest.approx(START + WEEK * 0.5 + 60.0)
    assert hr.render_wall(gate.returns_at) in gate.message


def test_ceiling_caps_the_pace_line_late_in_the_week() -> None:
    now = START + WEEK * 0.99  # 60*0.99 + 10 = 69.4 would exceed the ceiling
    _record(61.0, observed=now)
    gate = hr.usage_gate("claude", now_wall=now)
    assert gate is not None and not gate.allowed
    assert gate.pace_percent == 60.0


def test_ceiling_and_burst_are_configurable(monkeypatch) -> None:
    _record(66.0)
    monkeypatch.setenv("APIS_USAGE_WEEKLY_CEILING_PERCENT", "100")
    monkeypatch.setenv("APIS_USAGE_PACE_BURST_PERCENT", "60")
    gate = hr.usage_gate("claude", now_wall=NOW)
    assert gate is not None and gate.allowed  # 100*0.131 + 60 = 73 > 66


# --- selection honours the gate; local work keeps running


def test_gate_excludes_claude_and_leaves_other_providers() -> None:
    _record(66.0)
    avail = {"claude": "/bin/claude", "codex": "/bin/codex", "cursor": None}
    assert hr.provider_candidates(avail, now_wall=NOW) == ["codex"]
    reason = hr.provider_exclusion_reason("claude", avail, now_wall=NOW)
    assert reason is not None and "pace line" in reason


def test_local_provider_is_offered_while_frontier_is_refused() -> None:
    avail = {"claude": "/bin/claude", "claude-local": "/bin/claude"}
    assert hr.provider_candidates(avail, local_first=True, now_wall=NOW) == ["claude-local"]


def test_refusals_all_reports_gate_when_it_is_the_only_blocker() -> None:
    _record(66.0)
    refusals = hr.usage_gate_refusals_all({"claude": "/bin/claude"}, now_wall=NOW)
    assert refusals is not None and refusals["claude"].code == hr.GATE_PACED
    _record(5.0)
    assert hr.usage_gate_refusals_all({"claude": "/bin/claude"}, now_wall=NOW) is None


# --- the feeder: parse the CLI's own rate_limit_event

REAL_SHAPE = json.dumps({
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed_warning", "resetsAt": 1791223200, "rateLimitType": "seven_day",
        "utilization": 0.66, "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.2, "resetsAt": 1790713800},
            "seven_day": {"utilization": 0.66, "resetsAt": 1791223200},
        },
    },
})


def test_parse_real_shaped_event() -> None:
    out = "\n".join(['{"type":"system"}', "not json", REAL_SHAPE, '{"type":"result"}'])
    windows = usage_probe.parse_rate_limit_windows(out)
    assert {w["name"]: w["used_percent"] for w in windows} == {
        "five_hour": 20.0, "weekly_all": 66.0,
    }
    weekly = next(w for w in windows if w["name"] == "weekly_all")
    assert hr._wall_from_iso(weekly["resets_at"]) == RESET


def test_parse_skips_malformed_windows_and_caps_overage() -> None:
    event = {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": float("nan"), "resetsAt": 1},
        "seven_day": {"utilization": 1.4, "resetsAt": 1791223200},
        "": {"utilization": 0.1, "resetsAt": 1},
        "seven_day_opus": {"utilization": "x", "resetsAt": 1},
    }}}
    windows = usage_probe.parse_rate_limit_windows(json.dumps(event))
    assert windows == [{
        "name": "weekly_all", "used_percent": 100.0,
        "resets_at": hr._iso_from_wall(RESET),
    }]


def test_parse_no_event_is_empty() -> None:
    assert usage_probe.parse_rate_limit_windows('{"type":"result"}\nplain text') == []


def _fake_run(stdout: str, calls: list, returncode: int = 0):
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")
    return run


def test_refresh_records_snapshot_and_reopens_the_gate() -> None:
    calls: list = []
    outcome = usage_probe.refresh_usage_if_stale(
        {"claude": "/bin/claude"}, env={"PATH": "/usr/bin"}, now_wall=NOW,
        run=_fake_run(REAL_SHAPE, calls),
    )
    assert outcome == {"claude": "refreshed"}
    cmd, kwargs = calls[0]
    assert cmd[0] == "/bin/claude" and "stream-json" in cmd
    assert kwargs["input"] == usage_probe.PROBE_PROMPT
    assert kwargs["env"] == {"PATH": "/usr/bin"}  # the caller's environment, nothing added
    assert Path(kwargs["cwd"]).name.startswith("usage-probe-")
    assert {w["name"] for w in hr.usage_windows("claude")} == {"five_hour", "weekly_all"}


def test_refresh_is_skipped_while_reading_is_fresh() -> None:
    _record(5.0, observed=time.time(), resets=time.time() + 3 * 86400)
    calls: list = []
    outcome = usage_probe.refresh_usage_if_stale(
        {"claude": "/bin/claude"}, env={}, run=_fake_run(REAL_SHAPE, calls),
    )
    assert outcome == {"claude": "fresh"} and calls == []


def test_refresh_replaces_an_aged_reading() -> None:
    _record(5.0, observed=time.time() - 3600, resets=time.time() + 3 * 86400)
    calls: list = []
    outcome = usage_probe.refresh_usage_if_stale(
        {"claude": "/bin/claude"}, env={}, run=_fake_run(REAL_SHAPE, calls),
    )
    assert outcome == {"claude": "refreshed"} and len(calls) == 1


def test_failed_probe_leaves_reading_to_age_out_and_gate_refuses() -> None:
    _record(5.0, observed=NOW - 3600)
    before = hr.usage_windows("claude")
    outcome = usage_probe.refresh_usage_if_stale(
        {"claude": "/bin/claude"}, env={}, now_wall=NOW,
        run=_fake_run("Not logged in", [], returncode=1),
    )
    assert outcome["claude"].startswith("probe failed")
    assert hr.usage_windows("claude") == before  # nothing recorded as a reading
    gate = hr.usage_gate("claude", now_wall=NOW)
    assert gate is not None and gate.code == hr.GATE_STALE


def test_probe_that_cannot_launch_does_not_raise() -> None:
    def boom(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])
    outcome = usage_probe.refresh_usage_if_stale(
        {"claude": "/nope"}, env={}, now_wall=NOW, run=boom
    )
    assert outcome["claude"].startswith("probe failed")


def test_probe_is_off_when_disabled_or_provider_cooled(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setenv("APIS_USAGE_PROBE", "off")
    assert usage_probe.refresh_usage_if_stale(
        {"claude": "/bin/claude"}, env={}, run=_fake_run(REAL_SHAPE, calls)) == {}
    monkeypatch.setenv("APIS_USAGE_PROBE", "on")
    hr.record_cooling("claude", time.time() + 3600, reason="session_limit")
    out = usage_probe.refresh_usage_if_stale(
        {"claude": "/bin/claude"}, env={}, run=_fake_run(REAL_SHAPE, calls))
    assert out["claude"].startswith("cooled") and calls == []


# --- the dispatcher: distinct refusal, refresh before selection


def _dispatch(binaries, attempts: list):
    async def attempt(provider: str, **_kw):
        attempts.append(provider)
        return skill_runner.SkillResult("pavo", True, 0, "done", "", provider=provider)
    return asyncio.run(
        skill_runner._run_provider_attempts("pavo", attempt, binaries=binaries)
    )


def _no_refresh(monkeypatch) -> None:
    monkeypatch.setattr(skill_runner, "refresh_usage_if_stale", lambda *a, **k: {})


def test_dispatch_refused_on_stale_reading_names_it(monkeypatch) -> None:
    _no_refresh(monkeypatch)
    _record(20.0, observed=time.time() - 7 * 3600, resets=time.time() + 3 * 86400)
    attempts: list = []
    result = _dispatch({"claude": "/bin/claude"}, attempts)
    assert attempts == [] and result.ok is False
    assert "usage reading stale since" in result.error
    assert "local/mechanical work is unaffected" in result.error
    assert result.cooled_until  # the panel defers and retries instead of paging


def test_dispatch_refused_on_pace_says_when_capacity_returns(monkeypatch) -> None:
    _no_refresh(monkeypatch)
    now = time.time()
    _record(66.0, observed=now, resets=now + 6 * 86400)
    attempts: list = []
    result = _dispatch({"claude": "/bin/claude"}, attempts)
    assert attempts == [] and result.ok is False
    assert "pace line" in result.error and "returns at" in result.error


def test_dispatch_refreshes_the_reading_before_selecting(monkeypatch) -> None:
    now = time.time()
    _record(66.0, observed=now - 7 * 3600, resets=now + 6 * 86400)

    def fake_refresh(binaries, **kwargs):
        hr.record_usage("claude", [
            {"name": "five_hour", "used_percent": 5.0},
            {"name": "weekly_all", "used_percent": 4.0,
             "resets_at": hr._iso_from_wall(now + 6 * 86400)},
        ])
        return {"claude": "refreshed"}

    monkeypatch.setattr(skill_runner, "refresh_usage_if_stale", fake_refresh)
    attempts: list = []
    result = _dispatch({"claude": "/bin/claude"}, attempts)
    assert attempts == ["claude"] and result.ok is True


# --- ordering: the refresh must precede EVERY read that feeds selection (arch, PR #1369)


def _stale_then_fresh(monkeypatch):
    """A stale reading that a fake refresh replaces with a healthy one; logs order."""
    now = time.time()
    _record(66.0, observed=now - 7 * 3600, resets=now + 6 * 86400)
    events: list[str] = []

    def fake_refresh(binaries, **kwargs):
        events.append("refresh")
        hr.record_usage("claude", [
            {"name": "five_hour", "used_percent": 5.0},
            {"name": "weekly_all", "used_percent": 4.0,
             "resets_at": hr._iso_from_wall(now + 6 * 86400)},
        ])
        return {"claude": "refreshed"}

    monkeypatch.setattr(skill_runner, "refresh_usage_if_stale", fake_refresh)
    return events


def test_run_skill_refreshes_before_the_tier_filter_reads_selection(monkeypatch) -> None:
    """`_tier_bound_binaries` calls `usable_provider_names`, which consults the gate;
    on a stale reading it must already have been refreshed, or claude looks
    excluded and the filter falls back to providers with no bound model."""
    events = _stale_then_fresh(monkeypatch)
    seen: dict = {}

    def spy_tier_filter(binaries, tier, pinned, **kw):
        gate = hr.usage_gate("claude")
        seen["claude_allowed_at_selection"] = gate is not None and gate.allowed
        events.append("select")
        return binaries

    async def stub_attempts(skill, attempt, **kw):
        return skill_runner.SkillResult(skill, True, 0, "done", "")

    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {"claude": "/bin/claude"})
    monkeypatch.setattr(skill_runner, "_tier_bound_binaries", spy_tier_filter)
    monkeypatch.setattr(skill_runner, "_run_provider_attempts", stub_attempts)
    asyncio.run(skill_runner.run_skill("pavo", "p"))
    assert events[:2] == ["refresh", "select"]
    assert seen["claude_allowed_at_selection"] is True


def test_usable_providers_refreshes_before_reading_the_gate(monkeypatch) -> None:
    """review_panel.resolve_lens_provider consumes this view."""
    _stale_then_fresh(monkeypatch)
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {"claude": "/bin/claude"})
    assert "claude" in skill_runner.usable_providers()


# --- non-blocking review items: say why a refresh failed, and how to fix a refusal


NOT_LOGGED_IN = json.dumps({
    "type": "result", "is_error": True, "result": "Not logged in \u00b7 Please run /login",
})


def test_probe_failure_keeps_the_clis_own_reason() -> None:
    outcome = usage_probe.refresh_usage_if_stale(
        {"claude": "/bin/claude"}, env={}, now_wall=NOW,
        run=_fake_run(NOT_LOGGED_IN, [], returncode=1),
    )
    assert "Not logged in" in outcome["claude"]


def test_probe_failure_reason_falls_back_to_stderr_and_masks_token_like_runs() -> None:
    secret = "sk-ant-" + "a1B2c3D4" * 6

    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, "", f"error: auth rejected for key {secret}\n")

    outcome = usage_probe.refresh_usage_if_stale(
        {"claude": "/bin/claude"}, env={}, now_wall=NOW, run=run)
    assert "auth rejected" in outcome["claude"]
    assert secret not in outcome["claude"] and "<masked>" in outcome["claude"]
    assert secret not in hr._usage_path().read_text()


def test_refusal_carries_why_the_refresh_failed_and_how_to_fix_it() -> None:
    _record(5.0, observed=NOW - 3600)
    usage_probe.refresh_usage_if_stale(
        {"claude": "/bin/claude"}, env={}, now_wall=NOW,
        run=_fake_run(NOT_LOGGED_IN, [], returncode=1),
    )
    gate = hr.usage_gate("claude", now_wall=NOW)
    assert gate is not None and gate.code == hr.GATE_STALE
    assert "Not logged in" in gate.message
    assert "harness_usage.py refresh" in gate.message
    assert "harness-headroom-restore.md" in gate.message
    assert gate.probe_failure and "Not logged in" in gate.probe_failure


@pytest.mark.parametrize("case", ["missing", "malformed", "stale"])
def test_every_refresh_class_refusal_points_at_the_fix(case) -> None:
    if case == "malformed":
        hr._usage_path().write_text(json.dumps({"claude": {
            "observed_at": hr._iso_from_wall(NOW), "windows": [{"name": "", "used_percent": 1}]}}))
    elif case == "stale":
        _record(5.0, observed=NOW - 3600)
    gate = hr.usage_gate("claude", now_wall=NOW)
    assert gate is not None and gate.code == case
    assert "harness_usage.py refresh" in gate.message


def test_success_clears_the_recorded_failure() -> None:
    hr.record_probe_failure("claude", "Not logged in")
    _record(5.0)
    assert "last_probe_failure" not in hr._read_json_object(hr._usage_path())["claude"]


def test_entry_holding_only_a_failure_or_cooling_is_missing_not_malformed() -> None:
    hr.record_probe_failure("claude", "Not logged in")
    gate = hr.usage_gate("claude", now_wall=NOW)
    assert gate is not None and gate.code == hr.GATE_MISSING
    assert "Not logged in" in gate.message


def test_account_without_a_weekly_window_names_the_valve() -> None:
    hr.record_usage("claude", [{"name": "five_hour", "used_percent": 5}], observed_at=NOW)
    gate = hr.usage_gate("claude", now_wall=NOW)
    assert gate is not None and gate.code == hr.GATE_MALFORMED
    assert "APIS_USAGE_GATE=off" in gate.message
