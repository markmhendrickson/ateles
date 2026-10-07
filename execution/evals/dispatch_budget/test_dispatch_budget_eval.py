"""Dispatch-budget eval: what an operator or agent sees from the pace gate and
the spend report.

Scenarios are `{meta, events, assertions}` fixtures (fixtures/scenarios.json),
replayed through the real refresh, gate and spend-report code against a
stand-in `codex` executable that speaks the real plan-usage JSON-RPC. Runs in
the `pytest execution/evals/` CI step, so a committed scenario is reproduced by
re-running that step.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_APIS = HERE.parents[1] / "daemons" / "apis"
_SCRIPTS = HERE.parents[1] / "scripts"
for _path in (_APIS, _SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import dispatch_usage  # noqa: E402
import harness_router as hr  # noqa: E402
import harness_usage  # noqa: E402
import model_tiering  # noqa: E402
import usage_probe  # noqa: E402

SCENARIOS = json.loads((HERE / "fixtures" / "scenarios.json").read_text())
WEEK = hr.WEEK_SECONDS
RESET = 1791223200.0
NOW = RESET - WEEK + 22 * 3600.0  # 22h into the week: pace line about 17.9%

_SERVER = textwrap.dedent(
    """\
    #!@PYTHON@
    import json, sys
    report = json.load(open("@REPORT@"))
    if report == "unreadable":
        sys.exit(3)
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") == 1:
            print(json.dumps({"id": 1, "result": {}}), flush=True)
        elif msg.get("id") == 2:
            window = {"usedPercent": report["used_percent"],
                      "windowDurationMins": report["window_minutes"],
                      "resetsAt": @RESET@}
            limits = {"limitId": "codex", "primary": window, "secondary": None}
            result = {"rateLimits": limits, "rateLimitsByLimitId": {"codex": limits},
                      "ordinaryUsageAllowed": True}
            print(json.dumps({"id": 2, "result": result}), flush=True)
            sys.stdin.read()
            sys.exit(0)
    """
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_USAGE_GATE", "on")
    monkeypatch.setenv("APIS_USAGE_PROBE", "on")
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "no-headroom.json"))
    monkeypatch.setenv("APIS_TIER_LEDGER_FILE", str(tmp_path / "ledger.jsonl"))
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


def _install_fake_codex(tmp_path: Path, report) -> str:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    binary = tmp_path / "codex"
    binary.write_text(
        _SERVER.replace("@PYTHON@", sys.executable)
        .replace("@REPORT@", str(report_path))
        .replace("@RESET@", str(int(RESET)))
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(binary)


def _native(outcome: str):
    def run(cmd, **kwargs):
        if outcome == "ok":
            return subprocess.CompletedProcess(cmd, 0, '{"type":"result","result":"ok"}', "")
        return subprocess.CompletedProcess(cmd, 1, "", "something odd")

    return run


def _apply(event: dict, tmp_path: Path) -> None:
    if event["op"] == "refresh":
        binary = _install_fake_codex(tmp_path, event["codex"])
        usage_probe.refresh_usage_if_stale(
            {"codex": binary},
            env={"PATH": "/usr/bin"},
            now_wall=NOW + event["at"],
            force=True,
            run=_native(event.get("native", "fail")),
        )
    elif event["op"] == "usage_row":
        fields = {k: v for k, v in event.items() if k not in ("op", "provider")}
        model_tiering.record_dispatch_usage(
            dispatch_id="eval", skill="eval", provider=event["provider"],
            resolved=model_tiering.ResolvedTier("mid", "policy", "lens_review:pm"),
            requested_model="m",
            usage=dispatch_usage.DispatchUsage(provider=event["provider"], **fields),
        )
    elif event["op"] == "ledger":
        path = model_tiering.tier_ledger_path()
        if path.is_dir():
            path.rmdir()
        path.unlink(missing_ok=True)
        state = event["state"]
        if state == "empty":
            path.write_text("")
        elif state == "directory":
            path.mkdir()
        elif state == "undecodable":
            path.write_bytes(b"\xff\xfe not utf-8 \xff\n")
        elif state != "missing":  # pragma: no cover
            raise AssertionError(f"unknown ledger state {state!r}")
    else:  # pragma: no cover - a fixture typo must not pass silently
        raise AssertionError(f"unknown event op {event['op']!r}")


def _check(assertion: dict, capsys) -> None:
    kind = assertion["type"]
    if kind == "gate":
        gate = hr.usage_gate("codex", now_wall=NOW + assertion["at"])
        assert gate is not None
        assert gate.allowed is assertion["allowed"], gate.message
        assert gate.code == assertion["code"], gate.message
        if "weekly_used_percent" in assertion:
            assert gate.weekly_used_percent == assertion["weekly_used_percent"]
        if "message_contains" in assertion:
            assert assertion["message_contains"] in gate.message
    elif kind == "not_exhausted":
        moment = NOW + assertion["at"]
        assert hr.persisted_cooling("codex", now_wall=moment) is None
        assert hr.configured_headroom(now_wall=moment)["codex"] > hr.minimum_headroom()
    elif kind == "spend_field":
        capsys.readouterr()
        assert harness_usage.main(["spend", "--by", assertion["by"]]) == 0
        groups = json.loads(capsys.readouterr().out)["groups"]
        field = groups[assertion["group"]]["tokens"][assertion["field"]]
        assert field == {"sum": assertion["sum"], "reported_rows": assertion["reported_rows"]}
    elif kind == "spend_outcome":
        for command in ("spend", "cost"):
            capsys.readouterr()
            exit_code = harness_usage.main([command])
            captured = capsys.readouterr()
            report = json.loads(captured.out)  # machine-readable either way
            assert exit_code == assertion["exit"], (command, captured.out)
            if "error_kind" in assertion:
                error = report["error"]
                assert error["kind"] == assertion["error_kind"]
                assert error["path"] == str(model_tiering.tier_ledger_path())
                assert assertion["cause_contains"] in error["cause"]
                assert "error" in captured.err  # also said on stderr
                assert "groups" not in report  # never mistaken for an empty report
            else:
                assert "error" not in report and report["rows"] == assertion["rows"]
                assert report["groups"] == {}
    else:  # pragma: no cover
        raise AssertionError(f"unknown assertion type {kind!r}")


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario(name, tmp_path, capsys) -> None:
    scenario = SCENARIOS[name]
    assert scenario["meta"]["id"] == f"dispatch_budget.{name}"
    events, applied = scenario["events"], 0
    for assertion in scenario["assertions"]:
        # An assertion is checked once `after_event` events have happened (all
        # of them by default), so a scenario can assert mid-timeline.
        while applied < assertion.get("after_event", len(events)):
            _apply(events[applied], tmp_path)
            applied += 1
        _check(assertion, capsys)
    assert applied <= len(events)


def test_every_scenario_asserts_something() -> None:
    assert all(s["assertions"] and s["events"] for s in SCENARIOS.values())
