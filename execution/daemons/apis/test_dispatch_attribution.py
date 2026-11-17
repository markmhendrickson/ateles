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


def test_review_prompt_records_dispatch_and_usage_with_process_caller(
    ledger, monkeypatch
) -> None:
    model_tiering.set_process_caller("interactive", "loxia-session")
    monkeypatch.setenv("APIS_REVIEW_MODELS", '{"codex":"qualified-model"}')
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {"codex": "fake-codex"})
    monkeypatch.setattr(
        usage_alerts, "schedule_dispatch_check", lambda *a, **k: None, raising=False
    )

    async def one_attempt(skill, attempt, **kwargs):
        return await attempt("codex")

    async def no_event(**kwargs):
        return None

    class Process:
        returncode = 0

        async def communicate(self, input=None):
            assert input == b"review input"
            return b"Verdict: APPROVE", b"tokens used: 12"

    async def spawn(*cmd, **kwargs):
        assert cmd[0] == "fake-codex"
        return Process()

    monkeypatch.setattr(skill_runner, "_run_provider_attempts", one_attempt)
    monkeypatch.setattr(skill_runner, "_emit_harness_event", no_event)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = asyncio.run(skill_runner.run_review_prompt(role="loxia", prompt="review input"))
    assert result.ok
    rows = _rows(ledger)
    assert [row["event"] for row in rows] == ["dispatch", "usage"]
    assert rows[0]["dispatch_id"] == rows[1]["dispatch_id"]
    assert all(row["caller_kind"] == "interactive" and row["caller"] == "loxia-session" for row in rows)
    assert rows[1]["model"] == "qualified-model"


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
    def row(event, caller, total, dispatch_id, ts=None):
        return {"event": event, "dispatch_id": dispatch_id, "provider": "cursor",
                "model": "m", "tier": "mid", "caller_kind": "interactive",
                "caller": caller, "task_entity_id": "ent_t", "pr": 5,
                "total_tokens": total, "ts": ts}

    _write_rows(ledger, [
        row("dispatch", "s1", None, "a"), row("usage", "s1", 0, "a"),
        row("dispatch", "s1", None, "b"), row("usage", "s1", None, "b"),
        row("dispatch", "s2", None, "c"), row("usage", "s2", 700, "c"),
    ])
    # A row outside the window is excluded.
    _write_rows(ledger, [row("dispatch", "s3", None, "d")],
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


def test_summary_joins_usage_to_its_dispatch_when_reported_model_differs(ledger) -> None:
    """A dispatch requested ``opus``; the harness reported ``claude-opus-5``.
    The usage row joins its start row by ``dispatch_id`` and counts under the
    start row's keys: one group, labelled with the reported model. A usage row
    with no start row still counts, in its own labelled group."""
    common = {"provider": "claude", "tier": "top", "caller_kind": "daemon",
              "caller": "apis", "task_entity_id": "ent_t", "pr": 9}
    _write_rows(ledger, [
        {**common, "event": "dispatch", "dispatch_id": "d1", "model": "opus"},
        {**common, "event": "usage", "dispatch_id": "d1", "model": "claude-opus-5",
         "model_source": "reported", "total_tokens": 1200, "input_tokens": 1000},
        # A dispatch whose harness named no model keeps the requested one.
        {**common, "event": "dispatch", "dispatch_id": "d2", "model": "sonnet"},
        {**common, "event": "usage", "dispatch_id": "d2", "model": "sonnet",
         "model_source": "requested", "total_tokens": 50},
        # Usage whose start row is not in the ledger.
        {**common, "event": "usage", "dispatch_id": "gone", "model": "claude-opus-5",
         "model_source": "reported", "total_tokens": 7},
    ])
    report = model_tiering.attribution_summary()
    assert report["dispatch_rows"] == 2 and report["usage_rows"] == 3
    dispatch_groups = [g for g in report["groups"] if g["group"] == "dispatch"]
    assert len(dispatch_groups) == 2
    opus = next(g for g in dispatch_groups if g["requested_model"] == "opus")
    assert (opus["dispatches"], opus["usage_rows"]) == (1, 1)
    assert (opus["model"], opus["model_label"]) == ("claude-opus-5", "reported")
    assert opus["tokens"]["total_tokens"]["sum"] == 1200
    assert (opus["caller"], opus["pr"]) == ("apis", 9)
    sonnet = next(g for g in dispatch_groups if g["requested_model"] == "sonnet")
    assert (sonnet["model"], sonnet["model_label"]) == ("sonnet", "requested")
    assert sonnet["usage_rows"] == 1
    orphans = [g for g in report["groups"] if g["group"] == "usage_without_dispatch"]
    assert len(orphans) == 1
    assert (orphans[0]["dispatches"], orphans[0]["usage_rows"]) == (0, 1)
    assert orphans[0]["tokens"]["total_tokens"]["sum"] == 7
    table = model_tiering.render_attribution_summary(report)
    assert "usage, no dispatch row" in table and "reported" in table


def test_summary_joins_usage_whose_dispatch_started_before_the_window(ledger) -> None:
    common = {"provider": "codex", "caller_kind": "interactive", "caller": "s1"}
    _write_rows(ledger, [{**common, "event": "dispatch", "dispatch_id": "d1",
                          "model": "gpt"}],
                ts=datetime.now(timezone.utc) - timedelta(hours=3))
    _write_rows(ledger, [{**common, "event": "usage", "dispatch_id": "d1",
                          "model": "gpt-x", "model_source": "reported",
                          "total_tokens": 10}])
    report = model_tiering.attribution_summary(since_hours=1)
    (group,) = report["groups"]
    assert group["group"] == "dispatch" and group["requested_model"] == "gpt"
    assert (group["dispatches"], group["usage_rows"]) == (0, 1)


def test_a_repo_qualified_pr_is_recorded_on_both_rows(
    captured_codex_dispatches, codex_reports_tokens, ledger
) -> None:
    """The lens routes pass ``owner/repo#N``; the ledger keeps it as written."""
    assert dispatch_role.main(["--role", "pavo", "--task", "t",
                               "--pr", "owner/repo#1774"]) == 0
    assert {r["pr"] for r in _rows(ledger)} == {"owner/repo#1774"}


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
    """A real Notifier (its own send, dedupe journal and silence check) whose
    transport is faked: ``deliver`` says whether the transport delivers, and
    ``silenced`` whether the silence window is open. ``sent`` records every
    ``send`` call; ``delivered`` every message the transport accepted."""

    def __init__(self, *, deliver: bool = True, silenced: bool = False):
        from lib.notify import Notifier

        self.inner = Notifier()
        self.deliver = deliver
        self.silenced = silenced
        self.sent: list[tuple[str, str, str | None]] = []
        self.delivered: list[str] = []
        self.inner._deliver = self._transport
        self.inner._in_silence_window = lambda: self.silenced

    def _transport(self, message, force=False, email_eligible=True):
        if self.deliver:
            self.delivered.append(message)
        return self.deliver

    def __getattr__(self, name):
        return getattr(self.inner, name)

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


def test_a_first_reading_above_both_thresholds_alerts_both(
    monkeypatch, tmp_path, ledger
) -> None:
    """A first reading of 90%: one alert names 50% and 75%, both are recorded
    as delivered, and a repeat check is silent."""
    _usage_file(monkeypatch, tmp_path, "codex", [_window(90)])
    spy = _SpyNotifier()
    keys = usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert sorted(k.rsplit(":", 1)[1] for k in keys) == ["50", "75"]
    assert len(spy.delivered) == 1
    assert "(crossed 50% and 75%)" in spy.delivered[0]
    assert all(spy.is_dedupe_open(k) for k in keys)
    assert usage_alerts.check_usage_thresholds("codex", notifier=spy) == []
    assert len(spy.sent) == 1


def test_a_failed_delivery_is_retried_on_the_next_check(
    monkeypatch, tmp_path, ledger
) -> None:
    """The notifier journals a key before routing; an undelivered alert must
    not stay suppressed for the rest of the window."""
    _usage_file(monkeypatch, tmp_path, "cursor", [_window(60)])
    spy = _SpyNotifier(deliver=False)
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    key = spy.sent[0][2]
    assert key.endswith(":50")
    assert spy.inner._dedupe_journal[key]["state"] == "pending"
    # The same process backs off before retrying...
    spy.deliver = True
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert len(spy.sent) == 1
    # ...and retries once the backoff has passed.
    usage_alerts._retry_after.clear()
    entry = spy.inner._dedupe_journal[key]
    entry["pending_at"] = (spy.inner._now_local() - timedelta(minutes=1)).isoformat()
    spy.inner._persist_dedupe_journal({key: entry})
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == [key]
    assert len(spy.delivered) == 1
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert len(spy.sent) == 2


def test_combined_delivery_marks_an_earlier_pending_threshold_delivered(
    monkeypatch, tmp_path, ledger
) -> None:
    window = _window(60)
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    spy = _SpyNotifier(deliver=False)
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    first_key = spy.sent[0][2]
    assert spy.inner._dedupe_journal[first_key]["state"] == "pending"

    spy.deliver = True
    window["used_percent"] = 80
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    keys = usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    assert sorted(key.rsplit(":", 1)[1] for key in keys) == ["50", "75"]
    assert len(spy.delivered) == 1
    assert "crossed 50% and 75%" in spy.delivered[0]
    assert all(spy.inner._dedupe_journal[key]["state"] == "delivered" for key in keys)
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert len(spy.sent) == 2


def test_a_send_that_raises_is_retried_on_the_next_check(
    monkeypatch, tmp_path, ledger
) -> None:
    _usage_file(monkeypatch, tmp_path, "cursor", [_window(60)])
    spy = _SpyNotifier()

    def _raise(message, force=False, email_eligible=True):
        raise RuntimeError("transport exploded")

    spy.inner._deliver = _raise
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    spy.inner._deliver = spy._transport
    usage_alerts._retry_after.clear()  # past the backoff
    key = spy.sent[0][2]
    entry = spy.inner._dedupe_journal[key]
    entry["pending_at"] = (spy.inner._now_local() - timedelta(minutes=1)).isoformat()
    spy.inner._persist_dedupe_journal({key: entry})
    assert len(usage_alerts.check_usage_thresholds("cursor", notifier=spy)) == 1


def test_a_held_alert_is_not_sent_and_stays_eligible(monkeypatch, tmp_path, ledger) -> None:
    """In the silence window the alert is not sent (so it is not queued and
    then re-queued on every check) and is delivered on the first check after."""
    _usage_file(monkeypatch, tmp_path, "cursor", [_window(80)])
    spy = _SpyNotifier(silenced=True)
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert spy.sent == []
    spy.silenced = False
    assert len(usage_alerts.check_usage_thresholds("cursor", notifier=spy)) == 2
    assert len(spy.delivered) == 1


def test_release_dedupe_forgets_only_the_journal_entry(tmp_path, monkeypatch) -> None:
    from lib.notify import Notifier

    notifier = Notifier()
    notifier._mark_dedupe_notified("k1")
    notifier._mark_dedupe_notified("k2")
    notifier.release_dedupe("k1")
    notifier.release_dedupe("never-recorded")  # no-op, never raises
    assert not notifier.is_dedupe_open("k1") and notifier.is_dedupe_open("k2")
    assert notifier.dedupe_journal_path() == Path(
        __import__("os").environ["ATELES_DEDUPE_JOURNAL_PATH"]
    )


def _race_child(barrier, out_path: str) -> None:
    """One 'process' of the concurrency test (run under the spawn context).

    The barrier sits between the notifier's duplicate check and its journal
    write, the widest point of the old race: without a cross-process claim,
    both processes pass the check and both deliver.
    """
    import threading

    spy = _SpyNotifier()

    def _transport(message, force=False, email_eligible=True):
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write("delivered\n")
        return True

    spy.inner._deliver = _transport
    real_mark = spy.inner._mark_dedupe_notified

    def _slow_mark(key, handler=""):
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        real_mark(key, handler)

    spy.inner._mark_dedupe_notified = _slow_mark
    usage_alerts.check_usage_thresholds("cursor", notifier=spy)


def test_concurrent_processes_deliver_one_alert(monkeypatch, tmp_path, ledger) -> None:
    import multiprocessing

    _usage_file(monkeypatch, tmp_path, "cursor", [_window(60)])
    out = tmp_path / "deliveries.txt"
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    procs = [ctx.Process(target=_race_child, args=(barrier, str(out))) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    assert out.read_text().splitlines() == ["delivered"]


def test_a_refresh_with_no_later_dispatch_alerts_the_crossing(
    monkeypatch, tmp_path, ledger
) -> None:
    """The reading written by a usage refresh is checked at once: a crossing
    is alerted even though no dispatch follows."""
    import subprocess

    import usage_probe

    resets = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp())
    shape = json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed_warning", "resetsAt": resets,
            "rateLimitType": "seven_day", "utilization": 0.6, "isUsingOverage": False,
            "unifiedWindows": {
                "five_hour": {"utilization": 0.1, "resetsAt": resets},
                "seven_day": {"utilization": 0.6, "resetsAt": resets},
            },
        },
    })
    monkeypatch.setenv("APIS_USAGE_GATE", "on")
    monkeypatch.setenv("APIS_USAGE_PROBE", "on")
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "no-headroom.json"))
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    spy = _SpyNotifier()
    monkeypatch.setattr(usage_alerts, "_default_journal_factory", lambda: spy)
    monkeypatch.setattr(usage_alerts, "_default_notifier_factory", lambda: spy)
    dispatched: list = []
    monkeypatch.setattr(usage_alerts.model_tiering, "record_dispatch",
                        lambda **kw: dispatched.append(kw))

    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, shape, "")

    outcome = usage_probe.refresh_usage_if_stale(
        {"claude": "/bin/claude"}, env={"PATH": "/usr/bin"}, force=True, run=run,
        alert_in_background=False,
    )
    assert outcome == {"claude": "refreshed"}
    assert dispatched == []
    assert len(spy.delivered) == 1
    assert "claude" in spy.delivered[0] and "(crossed 50%)" in spy.delivered[0]


def test_the_usage_command_alerts_the_crossing_it_records(
    monkeypatch, tmp_path, ledger
) -> None:
    sys.path.insert(0, str(_DAEMON_DIR.parent.parent / "scripts"))
    import harness_usage

    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "no-headroom.json"))
    spy = _SpyNotifier()
    monkeypatch.setattr(usage_alerts, "_default_journal_factory", lambda: spy)
    monkeypatch.setattr(usage_alerts, "_default_notifier_factory", lambda: spy)
    resets = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    assert harness_usage.main(["usage", "codex", "--window", f"weekly=77@{resets}"]) == 0
    assert len(spy.delivered) == 1
    assert "(crossed 50% and 75%)" in spy.delivered[0]


def test_cli_exit_leaves_failed_alert_pending_for_next_process(
    monkeypatch, tmp_path, ledger
) -> None:
    sys.path.insert(0, str(_DAEMON_DIR.parent.parent / "scripts"))
    import harness_usage

    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "headroom.json"))
    spy = _SpyNotifier(deliver=False)
    monkeypatch.setattr(usage_alerts, "_default_journal_factory", lambda: spy)
    monkeypatch.setattr(usage_alerts, "_default_notifier_factory", lambda: spy)
    resets = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    assert harness_usage.main(["usage", "codex", "--window", f"weekly=60@{resets}"]) == 0
    key = spy.sent[0][2]
    assert spy.inner._dedupe_journal[key]["state"] == "pending"
    entry = spy.inner._dedupe_journal[key]
    entry["pending_at"] = (spy.inner._now_local() - timedelta(minutes=1)).isoformat()
    spy.inner._persist_dedupe_journal({key: entry})
    usage_alerts._retry_after.clear()
    spy.deliver = True
    assert usage_alerts.check_usage_thresholds("codex", notifier=spy) == [key]
    assert len(spy.delivered) == 1
    assert usage_alerts.check_usage_thresholds("codex", notifier=spy) == []


def test_a_new_window_alerts_again(monkeypatch, tmp_path, ledger) -> None:
    spy = _SpyNotifier()
    _usage_file(monkeypatch, tmp_path, "codex", [_window(60, resets_in_hours=2)])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    _usage_file(monkeypatch, tmp_path, "codex", [_window(60, resets_in_hours=170)])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert len(spy.sent) == 2


def test_reset_offsets_for_one_instant_share_a_dedupe_key(monkeypatch, tmp_path, ledger) -> None:
    spy = _SpyNotifier()
    instant = datetime.now(timezone.utc) + timedelta(days=2)
    for reset in (
        instant.isoformat().replace("+00:00", "Z"),
        instant.isoformat(),
        instant.astimezone(timezone(timedelta(hours=1))).isoformat(),
        (instant + timedelta(days=7)).isoformat(),
    ):
        _usage_file(monkeypatch, tmp_path, "codex", [{"name": "weekly", "used_percent": 60, "resets_at": reset}])
        usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert len(spy.sent) == 2


def test_unreadable_ledger_is_not_described_as_zero_activity(monkeypatch, tmp_path, ledger) -> None:
    _usage_file(monkeypatch, tmp_path, "codex", [_window(60)])
    spy = _SpyNotifier()

    def unreadable(**kwargs):
        raise model_tiering.LedgerReadError(ledger, OSError("unreadable"))

    monkeypatch.setattr(model_tiering, "top_callers", unreadable)
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert "attribution unavailable (ledger could not be read)" in spy.sent[0][0]
    assert "no attributed dispatches" not in spy.sent[0][0]
    monkeypatch.setattr(model_tiering, "top_callers", lambda **kwargs: [])
    _usage_file(monkeypatch, tmp_path, "codex", [_window(60, resets_in_hours=170)])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert "no attributed dispatches in the ledger" in spy.sent[1][0]


def test_a_window_with_no_reset_time_alerts_again_after_it_rolls_over(
    monkeypatch, tmp_path, ledger
) -> None:
    """With no resets_at the key cannot name the window, so falling back under
    a threshold clears it and the next crossing alerts again."""
    spy = _SpyNotifier()
    for percent in (60, 61, 5, 55):
        _usage_file(monkeypatch, tmp_path, "cursor",
                    [{"name": "monthly", "used_percent": percent}])
        usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    assert [k for _, _, k in spy.sent] == [
        "provider-usage-threshold:cursor:monthly:no-reset:50",
    ] * 2


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
    for thread in __import__("threading").enumerate():
        if thread.name == "usage-alert-codex":
            thread.join(timeout=2)
    assert rc == 0 and calls == ["codex"]


def test_slow_alert_delivery_does_not_delay_dispatch(
    captured_codex_dispatches, ledger, monkeypatch, tmp_path
) -> None:
    import threading
    import time

    _usage_file(monkeypatch, tmp_path, "codex", [_window(60)])
    delivered = threading.Event()
    release = threading.Event()
    spy = _SpyNotifier()

    def slow_transport(message, force=False, email_eligible=True):
        release.wait(timeout=2)
        delivered.set()
        return True

    spy.inner._deliver = slow_transport
    monkeypatch.setattr(usage_alerts, "_default_journal_factory", lambda: spy)
    monkeypatch.setattr(usage_alerts, "_default_notifier_factory", lambda: spy)
    started = time.monotonic()
    try:
        assert dispatch_role.main(["--role", "cicada", "--task", "t"]) == 0
        assert time.monotonic() - started < 0.5
        assert not delivered.is_set()
    finally:
        release.set()
    assert delivered.wait(timeout=2)


def test_refreshed_reading_does_not_wait_for_slow_alert(monkeypatch, tmp_path, ledger) -> None:
    import subprocess
    import threading
    import time

    import usage_probe

    resets = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp())
    shape = json.dumps({
        "type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "resetsAt": resets,
            "rateLimitType": "seven_day", "utilization": 0.6,
            "isUsingOverage": False,
        },
    })
    monkeypatch.setenv("APIS_USAGE_GATE", "on")
    monkeypatch.setenv("APIS_USAGE_PROBE", "on")
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "headroom.json"))
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    spy = _SpyNotifier()
    entered = threading.Event()
    release = threading.Event()

    def slow_transport(*args, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return True

    spy.inner._deliver = slow_transport
    monkeypatch.setattr(usage_alerts, "_default_journal_factory", lambda: spy)
    monkeypatch.setattr(usage_alerts, "_default_notifier_factory", lambda: spy)
    started = time.monotonic()
    try:
        outcome = usage_probe.refresh_usage_if_stale(
            {"claude": "/bin/claude"}, env={"PATH": "/usr/bin"}, force=True,
            run=lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, shape, ""),
        )
        assert outcome == {"claude": "refreshed"}
        assert time.monotonic() - started < 0.5
        assert entered.wait(timeout=2)
    finally:
        release.set()


def test_window_span_names() -> None:
    assert usage_alerts.window_span("five_hour") == (timedelta(hours=5), True)
    assert usage_alerts.window_span("weekly_all") == (timedelta(days=7), True)
    assert usage_alerts.window_span("window_90m") == (timedelta(minutes=90), True)
    assert usage_alerts.window_span("mystery")[1] is False
