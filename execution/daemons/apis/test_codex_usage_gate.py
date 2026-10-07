"""The weekly pace gate covers Codex, from Codex's own plan windows.

Task ent_13bd6d4cb981cace028c5a25, Phase A3. Codex exposes numeric plan usage
only through ``codex app-server`` (``account/rateLimits/read``), verified
2026-10-06 against codex-cli 0.157.1; ``codex exec`` emits none. These tests
drive the real subprocess boundary against a stand-in ``codex`` executable that
speaks the same JSON-RPC, and pin that an unreadable reading is UNKNOWN: it
neither zeroes Codex's headroom nor lets a dispatch through.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import harness_router as hr  # noqa: E402
import usage_probe  # noqa: E402

WEEK = hr.WEEK_SECONDS
RESET = 1791223200.0  # a weekly reset
NOW = RESET - WEEK + 22 * 3600.0  # 22h into the week: the pace line is ~17.9%
ENV_MODE = "FAKE_CODEX_MODE"


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


def _snapshot(used: float, minutes: int = 10080, resets: float = RESET, **extra) -> dict:
    """The shape ``account/rateLimits/read`` returned from a real Pro account."""
    primary = {"usedPercent": used, "windowDurationMins": minutes, "resetsAt": int(resets)}
    limits = {"limitId": "codex", "planType": "pro", "primary": primary, "secondary": None}
    return {"rateLimits": limits, "rateLimitsByLimitId": {"codex": limits},
            "ordinaryUsageAllowed": True, **extra}


FAKE_CODEX = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    mode = os.environ.get("{env}", "")
    if mode == "crash":
        sys.exit(3)
    if mode == "hang":
        sys.stdin.read()
        sys.exit(0)
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") == 1:
            print(json.dumps({{"id": 1, "result": {{}}}}), flush=True)
        elif msg.get("id") == 2:
            if mode.startswith("error:"):
                reply = {{"id": 2, "error": {{"code": -32000, "message": mode[6:]}}}}
            else:
                reply = {{"id": 2, "result": json.loads(mode)}}
            print(json.dumps(reply), flush=True)
            sys.stdin.read()
            sys.exit(0)
    """
)


@pytest.fixture
def fake_codex(tmp_path):
    path = tmp_path / "codex"
    path.write_text(FAKE_CODEX.format(python=sys.executable, env=ENV_MODE))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _env(mode: str | dict) -> dict:
    return {"PATH": os.environ.get("PATH", ""),
            ENV_MODE: mode if isinstance(mode, str) else json.dumps(mode)}


def _refresh(fake_codex, mode, *, run=None, now=NOW):
    return usage_probe.refresh_usage_if_stale(
        {"codex": fake_codex}, env=_env(mode), now_wall=now, force=True,
        **({"run": run} if run else {}),
    )


def _native_probe(returncode: int, stdout: str = "", stderr: str = ""):
    calls: list = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


# ── parsing ─────────────────────────────────────────────────────────────────


def test_a_weekly_window_is_named_by_its_length() -> None:
    windows, problem = usage_probe.parse_codex_rate_limits(_snapshot(78))
    assert problem is None
    assert windows == [{"name": "weekly_all", "used_percent": 78.0,
                        "resets_at": hr._iso_from_wall(RESET)}]


def test_a_plan_with_a_session_window_and_a_weekly_window_names_both() -> None:
    result = _snapshot(30, minutes=300)
    result["rateLimitsByLimitId"]["codex"]["secondary"] = {
        "usedPercent": 41, "windowDurationMins": 10080, "resetsAt": int(RESET)}
    windows, problem = usage_probe.parse_codex_rate_limits(result)
    assert problem is None
    assert {w["name"]: w["used_percent"] for w in windows} == {
        "five_hour": 30.0, "weekly_all": 41.0}


@pytest.mark.parametrize(
    "result,why",
    [
        (None, "not an object"),
        ({}, "no rateLimits"),
        (_snapshot(10, minutes=300), "no weekly-length window"),
        (_snapshot(10, minutes=1234), "no weekly-length window"),
        (_snapshot(True), "boolean"),
        (_snapshot("lots"), "missing a number"),
        (_snapshot(float("nan")), "out-of-range"),
        (_snapshot(-5), "out-of-range"),
        (_snapshot(10, ordinaryUsageAllowed=False), "not allowed"),
    ],
)
def test_an_unusable_reading_is_inconclusive_with_a_reason(result, why) -> None:
    windows, problem = usage_probe.parse_codex_rate_limits(result)
    assert windows == [] and why in (problem or "")


def test_a_percent_above_100_is_capped_not_rejected() -> None:
    windows, _ = usage_probe.parse_codex_rate_limits(_snapshot(130))
    assert windows[0]["used_percent"] == 100.0


# ── the subprocess boundary ─────────────────────────────────────────────────


def test_the_plan_windows_are_read_from_the_app_server(fake_codex) -> None:
    result = usage_probe.read_codex_rate_limits(fake_codex, env=_env(_snapshot(78)))
    assert result.status == "available" and result.source == usage_probe.CODEX_RATE_LIMITS_SOURCE
    assert result.windows[0]["used_percent"] == 78.0


@pytest.mark.parametrize("mode", ["crash", "error:not signed in", "{}", "[]"])
def test_a_failed_or_unusable_read_is_unknown_not_a_verdict(fake_codex, mode) -> None:
    result = usage_probe.read_codex_rate_limits(fake_codex, env=_env(mode), timeout=5)
    assert result.status == "unknown" and not result.windows


def test_an_app_server_that_never_answers_times_out_as_unknown(fake_codex) -> None:
    result = usage_probe.read_codex_rate_limits(fake_codex, env=_env("hang"), timeout=1.0)
    assert result.status == "unknown" and "did not answer" in result.detail


def test_a_missing_binary_is_unknown(tmp_path) -> None:
    result = usage_probe.read_codex_rate_limits(str(tmp_path / "nope"), env={})
    assert result.status == "unknown" and "did not run" in result.detail


# ── the gate ────────────────────────────────────────────────────────────────


def test_codex_over_the_weekly_pace_line_is_refused_as_paced(fake_codex) -> None:
    """78% a day in is well past ceiling x elapsed + burst (~17.9%)."""
    assert _refresh(fake_codex, _snapshot(78)) == {"codex": "refreshed"}
    gate = hr.usage_gate("codex", now_wall=NOW)
    assert gate is not None and not gate.allowed and gate.code == hr.GATE_PACED
    assert gate.weekly_used_percent == 78.0
    assert "capacity for codex returns at" in gate.message
    assert hr.usage_gate_refusals_all({"codex": fake_codex}, now_wall=NOW) is not None
    # The numbers come from Codex's own report, and the source says so.
    entry = json.loads(Path(os.environ["APIS_HARNESS_USAGE_FILE"]).read_text())["codex"]
    assert entry["probe"]["source"] == usage_probe.CODEX_RATE_LIMITS_SOURCE


def test_codex_under_the_pace_line_is_allowed(fake_codex) -> None:
    assert _refresh(fake_codex, _snapshot(9)) == {"codex": "refreshed"}
    gate = hr.usage_gate("codex", now_wall=NOW)
    assert gate is not None and gate.allowed and gate.code == hr.GATE_OK
    assert hr.live_headroom("codex", now_wall=NOW) == pytest.approx(0.91)


def test_a_young_paced_reading_is_not_probed_again(fake_codex) -> None:
    _refresh(fake_codex, _snapshot(78))
    outcome = usage_probe.refresh_usage_if_stale(
        {"codex": fake_codex}, env=_env("crash"), now_wall=NOW + 60,
    )
    assert outcome == {"codex": "fresh"}


def test_unknown_is_neither_exhaustion_nor_headroom(fake_codex) -> None:
    """The app-server fails AND the native request is inconclusive: the gate
    says UNKNOWN and refuses (so it is never read as headroom), but nothing is
    cooled and Codex's headroom is not zeroed (so it is never read as spent)."""
    outcome = _refresh(fake_codex, "crash", run=_native_probe(1, "", "something odd"))
    assert outcome["codex"].startswith("probe failed")
    gate = hr.usage_gate("codex", now_wall=NOW)
    assert gate is not None and not gate.allowed
    assert gate.code == hr.GATE_UNKNOWN != hr.GATE_PACED
    assert "plan windows:" in gate.message  # why the numeric read failed
    assert hr.persisted_cooling("codex", now_wall=NOW) is None
    assert hr.configured_headroom(now_wall=NOW)["codex"] > hr.minimum_headroom()
    entry = json.loads(Path(os.environ["APIS_HARNESS_USAGE_FILE"]).read_text())["codex"]
    assert not entry.get("exhausted_until")


def test_capacity_without_a_weekly_reading_does_not_authorize_dispatch(fake_codex) -> None:
    """The native request still runs, but its success is capacity evidence, not
    budget evidence: with no weekly reading the gate refuses and says why."""
    native = _native_probe(0, '{"type":"result","result":"ok"}')
    outcome = _refresh(fake_codex, "crash", run=native)
    assert outcome["codex"].startswith("probe failed: no weekly reading")
    assert native.calls and native.calls[0][1] == "exec"
    gate = hr.usage_gate("codex", now_wall=NOW)
    assert gate is not None and not gate.allowed and gate.code == hr.GATE_UNKNOWN
    assert "weekly budget reading unavailable" in gate.message
    assert "plan windows:" in gate.message  # the reading's own failure reason
    assert hr.usage_gate_refusals_all({"codex": fake_codex}, now_wall=NOW) is not None


def test_a_reading_with_no_weekly_window_is_not_paced_on_the_wrong_one(fake_codex) -> None:
    native = _native_probe(0, '{"type":"result","result":"ok"}')
    outcome = _refresh(fake_codex, _snapshot(95, minutes=300), run=native)
    assert outcome["codex"].startswith("probe failed: no weekly reading")
    assert native.calls  # the numeric reading was inconclusive, so the probe ran
    assert not hr.usage_windows("codex")  # and the five-hour window was not recorded
    assert not hr.usage_gate("codex", now_wall=NOW).allowed


# -- budget evidence handling tightened per security review --------------------


def _ok_native():
    return _native_probe(0, '{"type":"result","result":"ok"}')


def test_budget_evidence_survives_a_failed_refresh(fake_codex) -> None:
    _refresh(fake_codex, _snapshot(78))
    before = hr.usage_gate("codex", now_wall=NOW)
    assert before is not None and before.code == hr.GATE_PACED and not before.allowed
    later = NOW + 601  # an ordinary refresh interval, still inside the stale bound
    outcome = _refresh(fake_codex, "crash", run=_ok_native(), now=later)
    assert outcome["codex"].startswith("probe failed")
    after = hr.usage_gate("codex", now_wall=later)
    assert after is not None and not after.allowed
    assert after.code == hr.GATE_PACED and after.weekly_used_percent == 78.0
    assert [w["used_percent"] for w in hr.usage_windows("codex")] == [78.0]
    assert hr.provider_candidates({"codex": fake_codex}, now_wall=later) == []


def test_budget_evidence_that_ages_out_is_refused_until_refreshed(fake_codex) -> None:
    _refresh(fake_codex, _snapshot(9))
    assert hr.usage_gate("codex", now_wall=NOW).allowed
    later = NOW + hr.usage_stale_seconds() + 60
    _refresh(fake_codex, "crash", run=_ok_native(), now=later)
    gate = hr.usage_gate("codex", now_wall=later)
    assert gate is not None and not gate.allowed and gate.code == hr.GATE_STALE


def test_capacity_only_evidence_does_not_authorize(fake_codex) -> None:
    hr.record_probe_available("codex", source="codex_exec", observed_at=NOW)
    gate = hr.usage_gate("codex", now_wall=NOW)
    assert gate is not None and not gate.allowed and gate.code == hr.GATE_UNKNOWN


def test_a_later_good_reading_replaces_the_kept_evidence(fake_codex) -> None:
    _refresh(fake_codex, _snapshot(78))
    _refresh(fake_codex, "crash", run=_ok_native(), now=NOW + 601)
    _refresh(fake_codex, _snapshot(9), now=NOW + 1200)
    gate = hr.usage_gate("codex", now_wall=NOW + 1200)
    assert gate is not None and gate.allowed and gate.weekly_used_percent == 9.0
