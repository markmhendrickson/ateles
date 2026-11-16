"""Who launched each dispatch, and the 50%/75% provider usage alerts.

Task ent_e5259618cae6cafd4be120e0 (provider spend governance), unit 1: every
tier-ledger row names its caller (daemon or interactive session), task and PR;
a read-only summary groups the ledger by them; and a provider usage window that
crosses 50% and then 75% alerts the operator once per threshold per window.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import dispatch_role  # noqa: E402
import model_tiering  # noqa: E402
import skill_runner  # noqa: E402
import usage_alerts  # noqa: E402
from test_codex_tiering_ledger import codex_reports_tokens  # noqa: E402,F401
from test_dispatch_role import captured_codex_dispatches, fake_repo  # noqa: E402,F401


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("APIS_TIER_LEDGER_FILE", str(path))
    monkeypatch.delenv(model_tiering.SESSION_ID_ENV, raising=False)
    monkeypatch.setattr(model_tiering, "_PROCESS_CALLER", (None, None))
    return path


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── every ledger row carries its attribution ────────────────────────────────


def test_an_interactive_dispatch_records_session_task_and_pr_on_both_rows(
    captured_codex_dispatches, codex_reports_tokens, ledger, monkeypatch
) -> None:
    monkeypatch.setenv(model_tiering.SESSION_ID_ENV, "session-abc")
    rc = dispatch_role.main([
        "--role", "cicada", "--task", "t",
        "--task-entity-id", "ent_task1", "--pr", "1770",
    ])
    assert rc == 0
    rows = _rows(ledger)
    assert [r["event"] for r in rows] == ["dispatch", "usage"]
    for row in rows:
        assert row["caller_kind"] == "interactive"
        assert row["caller"] == "session-abc"
        assert row["task_entity_id"] == "ent_task1"
        assert row["pr"] == 1770


def test_explicit_caller_flag_wins_over_the_session_variable(
    captured_codex_dispatches, ledger, monkeypatch
) -> None:
    monkeypatch.setenv(model_tiering.SESSION_ID_ENV, "session-abc")
    dispatch_role.main(["--role", "cicada", "--task", "t", "--caller", "lead-session"])
    assert {r["caller"] for r in _rows(ledger)} == {"lead-session"}


def test_unknown_attribution_is_written_as_explicit_null(
    captured_codex_dispatches, ledger
) -> None:
    """No session id, no task, no PR: the keys are present and null, never
    guessed, and the kind is the entrypoint's own (interactive)."""
    dispatch_role.main(["--role", "cicada", "--task", "t"])
    for row in _rows(ledger):
        for name in model_tiering.ATTRIBUTION_FIELDS:
            assert name in row
        assert row["caller_kind"] == "interactive"
        assert row["caller"] is None
        assert row["task_entity_id"] is None
        assert row["pr"] is None


def test_a_daemon_process_attributes_its_dispatches_to_itself(ledger, monkeypatch) -> None:
    monkeypatch.setenv(model_tiering.SESSION_ID_ENV, "must-not-be-used")
    model_tiering.set_process_caller("daemon", "apis")
    attribution = model_tiering.resolve_attribution(task_entity_id="ent_x")
    assert attribution.as_fields() == {
        "caller_kind": "daemon", "caller": "apis", "task_entity_id": "ent_x", "pr": None,
    }


def test_run_skill_binds_attribution_for_rows_written_inside_the_dispatch(
    ledger, monkeypatch
) -> None:
    """The binding wraps run_skill and is reset afterwards, so it reaches rows
    written from a worker thread and does not leak to the next dispatch."""
    seen: list[dict] = []

    async def _fake_body(skill, prompt, **kwargs):
        def _write():
            model_tiering.record_dispatch(
                skill=skill, provider="codex", resolved=None, model=None
            )
        await asyncio.to_thread(_write)
        seen.append(model_tiering.current_attribution().as_fields())
        return "ok"

    wrapped = skill_runner._with_dispatch_attribution(_fake_body)
    asyncio.run(wrapped("cicada", "t", task_entity_id="ent_a", caller="apis",
                        caller_kind="daemon", pr="#12"))
    row = _rows(ledger)[0]
    assert (row["caller_kind"], row["caller"], row["task_entity_id"], row["pr"]) == (
        "daemon", "apis", "ent_a", 12,
    )
    assert seen[0]["caller"] == "apis"
    assert model_tiering.current_attribution().caller is None  # reset after the call


def test_an_invalid_caller_kind_is_recorded_as_unknown(ledger) -> None:
    attribution = model_tiering.resolve_attribution(caller="x", caller_kind="robot")
    assert attribution.caller_kind is None and attribution.caller == "x"


# ── rows written before attribution still parse everywhere ──────────────────

OLD_ROWS = [
    # The oldest shape: no event, no dispatch_id, no attribution.
    {"ts": None, "skill": "pavo", "provider": "claude", "action_class": "build",
     "tier": "top", "source": "policy", "model": "opus"},
    {"event": "dispatch", "dispatch_id": "d1", "ts": None, "skill": "cicada",
     "provider": "codex", "action_class": "", "tier": "untiered",
     "source": "no_action_class", "model": "", "escalation_reasons": []},
    {"event": "usage", "dispatch_id": "d1", "ts": None, "skill": "cicada",
     "provider": "codex", "action_class": "", "tier": "untiered",
     "source": "no_action_class", "model": "gpt", "model_source": "requested",
     "ok": True, "input_tokens": None, "output_tokens": None,
     "cache_read_tokens": None, "cache_write_tokens": None,
     "reasoning_tokens": None, "total_tokens": 0, "total_cost_usd": None},
]


def _write_rows(path: Path, rows: list[dict], *, ts: datetime | None = None) -> None:
    stamp = (ts or datetime.now(timezone.utc)).isoformat()
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps({**row, "ts": row.get("ts") or stamp}) + "\n")


def test_old_rows_without_attribution_still_parse_in_every_reader(ledger) -> None:
    _write_rows(ledger, OLD_ROWS)
    assert model_tiering.tier_counts()["total"] == 2
    assert model_tiering.usage_totals()["rows"] == 1
    assert model_tiering.usage_totals(group_by="caller")["groups"]["(none)"]["dispatches"] == 1
    summary = model_tiering.attribution_summary()
    assert summary["dispatch_rows"] == 2 and summary["usage_rows"] == 1
    for group in summary["groups"]:
        assert group["caller_kind"] is None and group["caller"] is None
    assert model_tiering.top_callers() == [(None, None, 2)]


# ── the read-only summary ───────────────────────────────────────────────────


def test_summary_groups_by_caller_and_keeps_nulls_apart_from_zeros(ledger) -> None:
    def row(event, caller, total, ts=None):
        return {"event": event, "dispatch_id": "x", "provider": "cursor",
                "model": "m", "tier": "mid", "caller_kind": "interactive",
                "caller": caller, "task_entity_id": "ent_t", "pr": 5,
                "total_tokens": total, "ts": ts}

    _write_rows(ledger, [
        row("dispatch", "s1", None), row("usage", "s1", 0),
        row("dispatch", "s1", None), row("usage", "s1", None),
        row("dispatch", "s2", None), row("usage", "s2", 700),
    ])
    # A row outside the window is excluded.
    _write_rows(ledger, [row("dispatch", "s3", None)],
                ts=datetime.now(timezone.utc) - timedelta(hours=48))
    report = model_tiering.attribution_summary(since_hours=24)
    by_caller = {g["caller"]: g for g in report["groups"]}
    assert set(by_caller) == {"s1", "s2"}
    s1 = by_caller["s1"]
    assert s1["dispatches"] == 2 and s1["usage_rows"] == 2
    assert s1["tokens"]["total_tokens"] == {"sum": 0, "reported_rows": 1, "null_rows": 1}
    assert s1["tokens"]["input_tokens"] == {"sum": None, "reported_rows": 0, "null_rows": 2}
    assert by_caller["s2"]["tokens"]["total_tokens"]["sum"] == 700
    assert (s1["provider"], s1["model"], s1["task_entity_id"], s1["pr"]) == (
        "cursor", "m", "ent_t", 5,
    )


def test_summary_cli_prints_a_table_and_json(ledger, capsys) -> None:
    _write_rows(ledger, OLD_ROWS)
    assert model_tiering.main(["--summary", "--since-hours", "24"]) == 0
    out = capsys.readouterr().out
    assert "caller_kind" in out and "null" in out
    assert model_tiering.main(["--summary", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dispatch_rows"] == 2


def test_check_mode_is_unchanged(tmp_path, capsys) -> None:
    bad = tmp_path / "policy.json"
    bad.write_text(json.dumps({"build": "nonsense"}))
    assert model_tiering.main(["--check", str(bad)]) == 1


# ── 50% / 75% alerts ────────────────────────────────────────────────────────


def _usage_file(monkeypatch, tmp_path, provider: str, windows: list[dict]) -> Path:
    path = tmp_path / "harness-usage.json"
    path.write_text(json.dumps({provider: {
        "observed_at": datetime.now(timezone.utc).isoformat(), "windows": windows,
    }}))
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(path))
    return path


def _window(percent: float, resets_in_hours: float = 24, name: str = "weekly_all") -> dict:
    resets = datetime.now(timezone.utc) + timedelta(hours=resets_in_hours)
    return {"name": name, "used_percent": percent, "resets_at": resets.isoformat()}


class _SpyNotifier:
    """A real Notifier (persistent dedupe journal, no transport) that records sends."""

    def __init__(self):
        from lib.notify import Notifier

        self.inner = Notifier()
        self.sent: list[tuple[str, str, str | None]] = []

    def is_dedupe_open(self, key):
        return self.inner.is_dedupe_open(key)

    def _mark_dedupe_notified(self, key, handler=""):
        self.inner._mark_dedupe_notified(key, handler)

    def send(self, message, priority="info", handler="", dedupe_key=None, **kw):
        self.sent.append((message, str(getattr(priority, "value", priority)), dedupe_key))
        return self.inner.send(message, priority=priority, handler=handler,
                               dedupe_key=dedupe_key, **kw)


def test_no_alert_below_fifty_percent(monkeypatch, tmp_path, ledger) -> None:
    _usage_file(monkeypatch, tmp_path, "cursor", [_window(49.9)])
    spy = _SpyNotifier()
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert spy.sent == []


def test_each_threshold_alerts_once_per_window(monkeypatch, tmp_path, ledger) -> None:
    """50% alerts once, repeated checks are silent, 75% alerts once more, and a
    fresh notifier (another process) reads the same journal and stays silent."""
    window = _window(52)
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    _write_rows(ledger, [
        {"event": "dispatch", "provider": "cursor", "caller_kind": "interactive",
         "caller": "s1", "tier": "mid"},
        {"event": "dispatch", "provider": "cursor", "caller_kind": "interactive",
         "caller": "s1", "tier": "mid"},
        {"event": "dispatch", "provider": "cursor", "caller_kind": "daemon",
         "caller": "apis", "tier": "mid"},
        {"event": "dispatch", "provider": "codex", "caller_kind": "daemon",
         "caller": "other-provider", "tier": "mid"},
    ])
    spy = _SpyNotifier()
    assert len(usage_alerts.check_usage_thresholds("cursor", notifier=spy)) == 1
    assert len(usage_alerts.check_usage_thresholds("cursor", notifier=spy)) == 0
    message, priority, key = spy.sent[0]
    assert key.endswith(":50") and priority == "warn"
    assert "cursor weekly_all window is 52% used" in message
    assert "interactive s1 (2), daemon apis (1)" in message
    assert "other-provider" not in message
    assert "Resets " in message

    window["used_percent"] = 76
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    assert len(usage_alerts.check_usage_thresholds("cursor", notifier=spy)) == 1
    assert spy.sent[-1][2].endswith(":75")
    assert len(usage_alerts.check_usage_thresholds("cursor", notifier=spy)) == 0

    other_process = _SpyNotifier()
    assert usage_alerts.check_usage_thresholds("cursor", notifier=other_process) == []
    assert other_process.sent == []
    assert len(spy.sent) == 2


def test_a_jump_past_both_thresholds_sends_one_alert(monkeypatch, tmp_path, ledger) -> None:
    _usage_file(monkeypatch, tmp_path, "codex", [_window(90)])
    spy = _SpyNotifier()
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert [k.rsplit(":", 1)[1] for _, _, k in spy.sent] == ["75"]


def test_a_new_window_alerts_again(monkeypatch, tmp_path, ledger) -> None:
    spy = _SpyNotifier()
    _usage_file(monkeypatch, tmp_path, "codex", [_window(60, resets_in_hours=2)])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    _usage_file(monkeypatch, tmp_path, "codex", [_window(60, resets_in_hours=170)])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert len(spy.sent) == 2


def test_a_window_past_its_reset_does_not_alert(monkeypatch, tmp_path, ledger) -> None:
    _usage_file(monkeypatch, tmp_path, "codex", [_window(99, resets_in_hours=-1)])
    spy = _SpyNotifier()
    assert usage_alerts.check_usage_thresholds("codex", notifier=spy) == []


def test_an_interactive_dispatch_builds_a_notifier_only_on_a_new_crossing(
    monkeypatch, tmp_path, ledger
) -> None:
    _usage_file(monkeypatch, tmp_path, "cursor", [_window(55)])
    built: list[_SpyNotifier] = []

    def factory():
        built.append(_SpyNotifier())
        return built[-1]

    usage_alerts.check_usage_thresholds("cursor", notifier_factory=factory)
    usage_alerts.check_usage_thresholds("cursor", notifier_factory=factory)
    assert len(built) == 1 and len(built[0].sent) == 1


def test_an_alert_failure_never_raises(monkeypatch, tmp_path, ledger) -> None:
    _usage_file(monkeypatch, tmp_path, "cursor", [_window(80)])

    class _Broken:
        def is_dedupe_open(self, key):
            return False

        def send(self, *a, **k):
            raise RuntimeError("transport down")

    assert usage_alerts.check_usage_thresholds("cursor", notifier=_Broken()) == []

    def _no_notifier():
        raise RuntimeError("neotoma unreachable")

    assert usage_alerts.check_usage_thresholds(
        "cursor", notifier_factory=_no_notifier
    ) == []
    (tmp_path / "harness-usage.json").write_text("{not json")
    assert usage_alerts.check_usage_thresholds("cursor", notifier=_Broken()) == []


def test_a_dispatch_succeeds_when_the_alert_check_blows_up(
    captured_codex_dispatches, ledger, monkeypatch
) -> None:
    """The alert hook sits on the dispatch path; even an exception escaping it
    must not fail the dispatch."""
    calls: list[str] = []

    def _explode(provider, **kwargs):
        calls.append(provider)
        raise RuntimeError("boom")

    monkeypatch.setattr(usage_alerts, "check_usage_thresholds", _explode)
    rc = dispatch_role.main(["--role", "cicada", "--task", "t"])
    assert rc == 0 and calls == ["codex"]


def test_window_span_names() -> None:
    assert usage_alerts.window_span("five_hour") == (timedelta(hours=5), True)
    assert usage_alerts.window_span("weekly_all") == (timedelta(days=7), True)
    assert usage_alerts.window_span("window_90m") == (timedelta(minutes=90), True)
    assert usage_alerts.window_span("mystery")[1] is False
