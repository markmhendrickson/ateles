"""Who launched each dispatch, and the 50%/75% provider usage alerts.

Task ent_e5259618cae6cafd4be120e0 (provider spend governance), unit 1: every
tier-ledger row names its caller (daemon or interactive session), task and PR;
a read-only summary groups the ledger by them; and a provider usage window that
crosses 50% and then 75% alerts the operator once per threshold per window.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
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
import harness_router  # noqa: E402
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


def _replace_journal_entry(notifier, key: str, entry: dict) -> None:
    def change(journal: dict) -> bool:
        journal[key] = entry
        return True

    assert notifier._update_dedupe_journal(change)


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
        self.email_flags: list[bool] = []
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
        self.email_flags.append(kw.get("email_eligible", True))
        return self.inner.send(message, priority=priority, handler=handler,
                               dedupe_key=dedupe_key, **kw)


def test_usage_warning_is_not_email_eligible(monkeypatch, tmp_path, ledger) -> None:
    _usage_file(monkeypatch, tmp_path, "cursor", [_window(52)])
    spy = _SpyNotifier()
    assert len(usage_alerts.check_usage_thresholds("cursor", notifier=spy)) == 1
    assert spy.email_flags == [False]


def test_full_queue_preserves_usage_warning(monkeypatch, tmp_path, ledger) -> None:
    from lib.notify import Priority, neotoma_sink, notifier as notifier_mod

    monkeypatch.setattr(neotoma_sink, "record", lambda **kwargs: None)
    monkeypatch.setattr(notifier_mod, "HELD_QUEUE_MAX", 1)
    window = _window(52)
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    spy = _SpyNotifier(silenced=True)
    spy.inner._digest_path = tmp_path / "held.json"
    spy.inner.should_flush_digest = lambda: False
    key = usage_alerts.dedupe_key("cursor", window["name"], window["resets_at"], 50)

    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert spy.inner._dedupe_journal[key]["state"] == "held"
    spy.send("other warning", priority=Priority.WARN, handler="other",
             dedupe_key="other:warning", retryable=True)
    assert spy.inner._dedupe_journal[key]["state"] == "held"

    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert len(spy.sent) == 2
    assert spy.inner._dedupe_journal[key]["state"] == "held"
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert len(spy.sent) == 2


def test_unreadable_held_queue_is_not_overwritten_by_new_usage_warning(
    monkeypatch, tmp_path, ledger, capsys
) -> None:
    from lib.notify import neotoma_sink, held_view

    monkeypatch.setattr(neotoma_sink, "record", lambda **kwargs: None)
    window = _window(52)
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    spy = _SpyNotifier(silenced=True)
    spy.inner._digest_path = tmp_path / "held.json"
    spy.inner.should_flush_digest = lambda: False
    usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    retained = spy.inner._digest_path.read_text()
    key = usage_alerts.dedupe_key("cursor", window["name"], window["resets_at"], 50)

    spy.inner._digest_path.write_text("unreadable queue")
    window["used_percent"] = 76
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    assert spy.inner._digest_path.read_text() == "unreadable queue"
    assert key in spy.inner._dedupe_journal
    assert len(spy.sent) == 2
    assert held_view._print_notices(spy.inner._digest_path, False) == 1
    assert capsys.readouterr().out.count("RECOVERY REQUIRED:") == 1

    spy.inner._digest_path.write_text(retained)
    usage_alerts._retry_after.clear()
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert held_view.notice_text(spy.inner._digest_queue[0]) == "⚠ [apis] " + spy.sent[0][0]


def test_missing_held_notice_quarantines_usage_warning(
    monkeypatch, tmp_path, ledger
) -> None:
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setattr(neotoma_sink, "record", lambda **kwargs: None)
    window = _window(52)
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    spy = _SpyNotifier(silenced=True)
    spy.inner._digest_path = tmp_path / "held.json"
    spy.inner.should_flush_digest = lambda: False
    usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    key = usage_alerts.dedupe_key("cursor", window["name"], window["resets_at"], 50)
    spy.inner._digest_path.unlink()

    usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    assert len(spy.sent) == 1
    assert spy.inner._dedupe_journal[key]["state"] == "delivered_unconfirmed"
    assert any(row["key"] == key for row in held_view.read_recovery(spy.inner._digest_path))
    usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    assert len(spy.sent) == 1


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
    _replace_journal_entry(spy.inner, key, entry)
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


def test_timed_out_combined_handoff_keeps_all_thresholds_owned(
    monkeypatch, tmp_path, ledger
) -> None:
    import threading
    import time

    window = _window(60)
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    spy = _SpyNotifier(deliver=False)
    usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    window["used_percent"] = 80
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    spy.deliver = True
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(usage_alerts, "ALERT_SEND_TIMEOUT_SECONDS", 0.02)

    def slow_transport(message, force=False, email_eligible=True):
        entered.set()
        assert release.wait(timeout=2)
        spy.delivered.append(message)
        return True

    spy.inner._deliver = slow_transport
    try:
        assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
        assert entered.wait(timeout=2)
        from lib.notify import held_view
        info = held_view.summary(spy.inner._digest_path)
        assert info["sending_in_flight"] == 2
        assert info["unconfirmed"] == 0
        assert usage_alerts.check_usage_thresholds("cursor", notifier=_SpyNotifier()) == []
        assert len(spy.sent) == 2
    finally:
        release.set()
    keys = [usage_alerts.dedupe_key("cursor", window["name"], window["resets_at"], t)
            for t in (50, 75)]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not all(
        spy.inner._dedupe_journal.get(key, {}).get("state") == "delivered" for key in keys
    ):
        time.sleep(0.01)
    assert all(spy.inner._dedupe_journal[key]["state"] == "delivered" for key in keys)
    assert usage_alerts.check_usage_thresholds("cursor", notifier=_SpyNotifier()) == []


def test_a_send_that_raises_is_quarantined_on_the_next_check(
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
    assert spy.inner._dedupe_journal[key]["state"] == "delivered_unconfirmed"
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert len(spy.sent) == 1


def test_a_held_alert_drains_once_without_another_usage_check(monkeypatch, tmp_path, ledger) -> None:
    """The held queue delivers a crossing even if usage is never refreshed again."""
    from lib.notify import neotoma_sink

    monkeypatch.setattr(neotoma_sink, "record", lambda **kwargs: None)
    _usage_file(monkeypatch, tmp_path, "cursor", [_window(80)])
    spy = _SpyNotifier(silenced=True)
    spy.inner._digest_path = tmp_path / "held.json"
    spy.inner._apprise = object()
    spy.inner._email_primary = False
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert len(spy.sent) == 1
    assert len(spy.inner._digest_queue) == 1
    spy.silenced = False
    assert spy.inner.flush_digest()
    assert spy.inner._digest_queue == []
    assert len(spy.delivered) == 1
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert len(spy.sent) == 1


def test_concurrent_ordinary_write_preserves_delivered_usage_key(
    monkeypatch, tmp_path, ledger
) -> None:
    import threading

    from lib.notify import Priority, neotoma_sink

    monkeypatch.setattr(neotoma_sink, "record", lambda **kwargs: None)
    window = _window(50)
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    usage = _SpyNotifier()
    ordinary = _SpyNotifier()
    entered = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    original = usage.inner._persist_dedupe_journal

    def paused_persist(journal):
        if any(entry.get("state") == "delivered" for entry in journal.values()):
            entered.set()
            assert release.wait(timeout=3)
        return original(journal)

    monkeypatch.setattr(usage.inner, "_persist_dedupe_journal", paused_persist)
    worker = threading.Thread(
        target=lambda: usage_alerts.check_usage_thresholds("cursor", notifier=usage)
    )
    worker.start()
    try:
        assert entered.wait(timeout=3)
        def send_ordinary():
            second_started.set()
            ordinary.send(
                "ordinary", priority=Priority.WARN, handler="tyto",
                dedupe_key="ordinary:key",
            )

        second = threading.Thread(target=send_ordinary)
        second.start()
        assert second_started.wait(timeout=3)
        second.join(timeout=0.1)
        assert second.is_alive()  # blocked by the first writer's journal lock
    finally:
        release.set()
        worker.join(timeout=3)
    second.join(timeout=3)
    assert not worker.is_alive() and not second.is_alive()
    journal = usage.inner._dedupe_journal
    key = usage_alerts.dedupe_key("cursor", window["name"], window["resets_at"], 50)
    assert journal[key]["state"] == "delivered"
    assert "ordinary:key" in journal
    assert usage_alerts.check_usage_thresholds("cursor", notifier=usage) == []
    assert len(usage.sent) == 1


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


def test_documented_usage_script_invokes_alert_without_pythonpath(tmp_path):
    """An isolated direct script invocation reaches the fake transport."""
    script = _DAEMON_DIR.parent.parent / "scripts" / "harness_usage.py"
    reset = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    child = f'''import runpy, sys
sys.path.insert(0, {str(_DAEMON_DIR)!r})
import usage_alerts
class Fake:
    def is_dedupe_open(self, key): return False
    def dedupe_keys_with_prefix(self, prefix): return ()
    def send(self, *args, **kwargs):
        print("FAKE_TRANSPORT_DELIVERED", flush=True)
        return True
fake = Fake()
usage_alerts._default_journal_factory = lambda: fake
usage_alerts._default_notifier_factory = lambda: fake
sys.argv = [{str(script)!r}, "usage", "codex", "--window", {f"weekly=52@{reset}"!r}]
runpy.run_path({str(script)!r}, run_name="__main__")
'''
    env = {**os.environ, "HOME": str(tmp_path),
           "APIS_HARNESS_USAGE_FILE": str(tmp_path / "usage.json"),
           "APIS_HARNESS_HEADROOM_FILE": str(tmp_path / "headroom.json"),
           "ATELES_DEDUPE_JOURNAL_PATH": str(tmp_path / "dedupe.json"),
           "APIS_TIER_LEDGER_FILE": str(tmp_path / "ledger.jsonl")}
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-I", "-c", child], cwd=_DAEMON_DIR.parent.parent.parent,
                            env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "FAKE_TRANSPORT_DELIVERED" in result.stdout, result.stderr
    assert "No module named 'lib'" not in result.stderr


def test_active_ungated_providers_are_observed_and_alerted(monkeypatch, tmp_path, ledger):
    import harness_router
    import usage_probe

    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex,cursor")
    monkeypatch.setenv("APIS_USAGE_GATED_PROVIDERS", "claude")
    monkeypatch.setenv("APIS_USAGE_PROBE", "on")
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    called = []
    spy = _SpyNotifier()
    def refresh(provider, *args, **kwargs):
        called.append(provider)
        harness_router.record_usage(provider, [
            {"name": "weekly", "used_percent": 76,
             "resets_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}
        ])
        return "refreshed"
    monkeypatch.setattr(usage_probe, "_refresh_provider", refresh)
    monkeypatch.setattr(usage_probe, "_alert_on_new_reading",
                        lambda provider, **kw: usage_alerts.check_usage_thresholds(provider, notifier=spy))
    outcome = usage_probe.refresh_usage_if_stale(
        {"codex": "/bin/codex", "cursor": "/bin/cursor", "claude": "/bin/claude"},
        env={}, force=True,
    )
    assert outcome == {"codex": "refreshed", "cursor": "refreshed"}
    assert called == ["codex", "cursor"]
    assert all(len(harness_router.usage_windows(p)) == 1 for p in called)
    assert len(spy.delivered) == 2
    assert all("(crossed 50% and 75%)" in message for message in spy.delivered)


def test_only_active_gated_provider_is_probed(monkeypatch, tmp_path):
    import usage_probe

    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    monkeypatch.setenv("APIS_USAGE_GATED_PROVIDERS", "claude")
    monkeypatch.setenv("APIS_USAGE_GATE", "on")
    monkeypatch.setenv("APIS_USAGE_PROBE", "on")
    monkeypatch.setattr(usage_probe, "_refresh_provider", lambda provider, *args, **kwargs: "refreshed")
    monkeypatch.setattr(usage_probe, "_alert_on_new_reading", lambda *args, **kwargs: None)
    outcome = usage_probe.refresh_usage_if_stale(
        {"claude": "/bin/claude", "codex": "/bin/codex"}, env={}, force=True,
    )
    assert outcome == {"claude": "refreshed"}


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
    _replace_journal_entry(spy.inner, key, entry)
    usage_alerts._retry_after.clear()
    spy.deliver = True
    assert usage_alerts.check_usage_thresholds("codex", notifier=spy) == [key]
    assert len(spy.delivered) == 1
    assert usage_alerts.check_usage_thresholds("codex", notifier=spy) == []


def test_real_apis_notifier_retries_malformed_thresholds_on_both_entrances(
    monkeypatch, tmp_path, ledger
) -> None:
    from lib.notify import held_view, neotoma_sink
    import red_conditions

    monkeypatch.setattr(neotoma_sink, "record", lambda **kwargs: None)
    monkeypatch.setenv(red_conditions.STATE_ENV, str(tmp_path / "red.json"))
    notifier = red_conditions.ApisNotifier(rubric={
        "silence_start": "", "silence_end": "", "timezone": "Europe/Madrid",
    })
    notifier._digest_path = tmp_path / "held.json"
    notifier._dedupe_path = tmp_path / "dedupe.json"
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(notifier._digest_path))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(notifier._dedupe_path))
    notifier._email_primary = False
    delivered = []
    notifier._deliver = lambda message, **kwargs: delivered.append(message) or True
    window = _window(52)
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    first = usage_alerts.dedupe_key("cursor", window["name"], window["resets_at"], 50)
    notifier._dedupe_path.write_text(json.dumps({first: "malformed"}))

    # Usage-write entrance calls the threshold checker synchronously.
    assert usage_alerts.check_usage_thresholds("cursor", notifier=notifier) == []
    assert not [message for message in delivered if "crossed 50%" in message]
    assert held_view.main(["--clear-unconfirmed", first]) == 0
    assert usage_alerts.check_usage_thresholds("cursor", notifier=notifier) == [first]
    assert notifier._dedupe_journal[first]["state"] == "delivered"
    window["used_percent"] = 76
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    second = usage_alerts.dedupe_key("cursor", window["name"], window["resets_at"], 75)
    doc = notifier._dedupe_journal
    doc[second] = ["malformed"]
    notifier._dedupe_path.write_text(json.dumps(doc))

    # The dispatch entrance runs the same handoff in its worker thread.
    usage_alerts.schedule_dispatch_check("cursor", notifier=notifier).join(2)
    assert not [message for message in delivered if "crossed 75%" in message]
    assert held_view.main(["--clear-unconfirmed", second]) == 0
    usage_alerts.schedule_dispatch_check("cursor", notifier=notifier).join(2)
    assert len([message for message in delivered if "crossed" in message]) == 2
    assert notifier._dedupe_journal[second]["state"] == "delivered"


def test_a_new_window_alerts_again(monkeypatch, tmp_path, ledger) -> None:
    spy = _SpyNotifier()
    first = _window(60, resets_in_hours=2)
    _usage_file(monkeypatch, tmp_path, "codex", [first])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    _usage_file(monkeypatch, tmp_path, "codex", [_window(60, resets_in_hours=170)])
    usage_alerts.check_usage_thresholds("codex", notifier=spy,
        now=datetime.fromisoformat(first["resets_at"]) + timedelta(seconds=1))
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
    assert len(spy.sent) == 1


def test_probe_and_manual_window_aliases_share_keys() -> None:
    reset = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    weekly = {usage_alerts.dedupe_key("codex", name, reset, 50)
              for name in ("weekly", "weekly_all", "seven_day", "window_10070m")}
    session = {usage_alerts.dedupe_key("codex", name, reset, 50)
               for name in ("session", "five_hour", "window_310m")}
    assert len(weekly) == len(session) == 1
    assert weekly != session


def test_legacy_window_alias_migrates_uncertain_receipt(monkeypatch, tmp_path, ledger) -> None:
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    spy = _SpyNotifier()
    reset = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    old = f"provider-usage-threshold:codex:seven_day:{reset}:50"
    spy.inner._mark_dedupe_sending((old,), "apis")
    spy.inner._mark_delivery_uncertain((old,), "synthetic uncertain outcome")
    _usage_file(monkeypatch, tmp_path, "codex", [{
        "name": "window_10070m", "used_percent": 60, "resets_at": reset,
    }])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    new = usage_alerts.dedupe_key("codex", "weekly_all", reset, 50)
    assert not [message for message in spy.delivered if "Provider usage:" in message]
    assert spy.inner._dedupe_journal[new]["state"] == "delivered_unconfirmed"
    assert [row["key"] for row in held_view.read_recovery(spy.inner._digest_path)] == [new]


def test_reset_correction_finds_recovery_only_key(monkeypatch, tmp_path, ledger) -> None:
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    spy = _SpyNotifier()
    first = datetime.now(timezone.utc) + timedelta(hours=2)
    corrected = first + timedelta(hours=3)
    old = usage_alerts.dedupe_key("codex", "weekly_all", first.isoformat(), 50)
    spy.inner._raise_recovery(old, "synthetic missing receipt", notify=False)
    _usage_file(monkeypatch, tmp_path, "codex", [{
        "name": "weekly_all", "used_percent": 60, "resets_at": corrected.isoformat(),
    }])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    new = usage_alerts.dedupe_key("codex", "weekly_all", corrected.isoformat(), 50)
    assert spy.delivered == []
    assert spy.inner._dedupe_journal[new]["state"] == "delivered_unconfirmed"
    assert [row["key"] for row in held_view.read_recovery(spy.inner._digest_path)] == [new]


def test_earlier_reset_correction_migrates_without_resolving_or_resending(
    monkeypatch, tmp_path, ledger
) -> None:
    from lib.notify import neotoma_sink

    migrated = []
    resolved = []
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda old, new: (
        migrated.append((old, new)) or True
    ))
    monkeypatch.setattr(neotoma_sink, "resolve", lambda key: resolved.append(key))
    spy = _SpyNotifier()
    later = datetime.now(timezone.utc) + timedelta(days=4)
    earlier = later - timedelta(days=1)
    for reset in (later, earlier):
        _usage_file(monkeypatch, tmp_path, "codex", [{
            "name": "monthly", "used_percent": 60, "resets_at": reset.isoformat(),
        }])
        usage_alerts.check_usage_thresholds("codex", notifier=spy)
    new_key = usage_alerts.dedupe_key("codex", "monthly", earlier.isoformat(), 50)
    assert len(spy.delivered) == 1
    assert len(migrated) == 1 and migrated[0][1] == new_key
    assert resolved == []
    assert spy.inner._dedupe_journal[new_key]["state"] == "delivered"


def test_window_name_case_is_one_condition(monkeypatch, tmp_path, ledger) -> None:
    spy = _SpyNotifier()
    reset = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    for name in ("monthly", "Monthly", " MONTHLY "):
        _usage_file(monkeypatch, tmp_path, "codex", [{
            "name": name, "used_percent": 60, "resets_at": reset,
        }])
        usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert len(spy.delivered) == 1
    assert list(spy.inner._dedupe_journal) == [
        usage_alerts.dedupe_key("codex", "monthly", reset, 50)
    ]


def test_legacy_case_and_unparseable_reset_migrate_without_resolution(
    monkeypatch, tmp_path, ledger
) -> None:
    from lib.notify import neotoma_sink

    migrated = []
    resolved = []
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda old, new: (
        migrated.append((old, new)) or True
    ))
    monkeypatch.setattr(neotoma_sink, "resolve", lambda key: resolved.append(key))
    spy = _SpyNotifier()
    old_key = "provider-usage-threshold:codex:Monthly:bad-reset:50"
    spy.inner._mark_dedupe_delivered(old_key, "apis")
    reset = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    _usage_file(monkeypatch, tmp_path, "codex", [{
        "name": "monthly", "used_percent": 60, "resets_at": reset,
    }])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    new_key = usage_alerts.dedupe_key("codex", "monthly", reset, 50)
    assert migrated == [(old_key, new_key)]
    assert resolved == [] and spy.sent == []
    assert spy.inner._dedupe_journal[old_key] == {"state": "migrated", "migration_to": new_key}
    assert spy.inner._dedupe_journal[new_key]["state"] == "delivered"


def test_later_reset_within_active_window_is_a_correction(monkeypatch, tmp_path, ledger) -> None:
    from lib.notify import neotoma_sink

    resolved = []
    monkeypatch.setattr(neotoma_sink, "resolve", lambda key: resolved.append(key))
    spy = _SpyNotifier()
    first = datetime.now(timezone.utc) + timedelta(days=2)
    second = first + timedelta(days=30)
    for reset in (first, second):
        _usage_file(monkeypatch, tmp_path, "codex", [{
            "name": "monthly", "used_percent": 60, "resets_at": reset.isoformat(),
        }])
        usage_alerts.check_usage_thresholds("codex", notifier=spy)
    first_key = usage_alerts.dedupe_key("codex", "monthly", first.isoformat(), 50)
    second_key = usage_alerts.dedupe_key("codex", "monthly", second.isoformat(), 50)
    assert resolved == []
    assert len(spy.delivered) == 1
    assert spy.inner._dedupe_journal[first_key]["migration_to"] == second_key
    assert spy.inner._dedupe_journal[second_key]["state"] == "delivered"


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("outcome", ["uncertain", "delivered"])
@pytest.mark.parametrize("delta_days", [-1, 1])
def test_reset_correction_preserves_real_notifier_outcome(
    monkeypatch, tmp_path, ledger, held, outcome, delta_days
) -> None:
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    spy = _SpyNotifier(silenced=held)
    spy.inner._apprise = object()
    spy.inner.should_flush_digest = lambda: False
    first = datetime.now(timezone.utc) + timedelta(days=3)
    corrected = first + timedelta(days=delta_days)
    original_transport = spy.inner._deliver
    transport_calls = []

    def uncertain(message, **kwargs):
        if "Provider usage:" in message:
            transport_calls.append(message)
            raise OSError("synthetic accepted, receipt lost")
        return False

    if outcome == "uncertain" and not held:
        spy.inner._deliver = uncertain
    _usage_file(monkeypatch, tmp_path, "codex", [{
        "name": "monthly", "used_percent": 60, "resets_at": first.isoformat(),
    }])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    if held:
        assert spy.inner._dedupe_journal[usage_alerts.dedupe_key(
            "codex", "monthly", first.isoformat(), 50)]["state"] == "held"
        spy.silenced = False
        if outcome == "uncertain":
            spy.inner._deliver = uncertain
        spy.inner.flush_digest()
    spy.inner._deliver = original_transport
    before = len(transport_calls) + len(spy.delivered)
    _usage_file(monkeypatch, tmp_path, "codex", [{
        "name": "monthly", "used_percent": 60, "resets_at": corrected.isoformat(),
    }])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    spy.inner.flush_digest()
    assert len(transport_calls) + len(spy.delivered) == before
    old = usage_alerts.dedupe_key("codex", "monthly", first.isoformat(), 50)
    new = usage_alerts.dedupe_key("codex", "monthly", corrected.isoformat(), 50)
    assert spy.inner._dedupe_journal[old]["migration_to"] == new
    assert spy.inner._dedupe_journal[new]["state"] == (
        "delivered_unconfirmed" if outcome == "uncertain" else "delivered")
    recovery = held_view.read_recovery(spy.inner._digest_path)
    assert len(recovery) == (1 if outcome == "uncertain" else 0)
    if recovery:
        assert recovery[0]["key"] == new


@pytest.mark.parametrize("held", [False, True])
def test_real_notifier_new_window_after_previous_end_sends_once(
    monkeypatch, tmp_path, ledger, held
) -> None:
    from lib.notify import neotoma_sink

    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    spy = _SpyNotifier(silenced=held)
    spy.inner._apprise = object()
    spy.inner.should_flush_digest = lambda: False
    first = datetime.now(timezone.utc) + timedelta(hours=2)
    second = first + timedelta(days=30)
    for reset, current in ((first, datetime.now(timezone.utc)),
                           (second, first + timedelta(seconds=1))):
        _usage_file(monkeypatch, tmp_path, "codex", [{
            "name": "monthly", "used_percent": 60, "resets_at": reset.isoformat(),
        }])
        usage_alerts.check_usage_thresholds("codex", notifier=spy, now=current)
        if held:
            spy.silenced = False
            spy.inner.flush_digest()
            spy.silenced = True
    assert len([message for message in spy.delivered if "Provider usage:" in message]) == 2


def test_unreadable_ledger_is_not_described_as_zero_activity(monkeypatch, tmp_path, ledger) -> None:
    first = _window(60)
    _usage_file(monkeypatch, tmp_path, "codex", [first])
    spy = _SpyNotifier()

    def unreadable(**kwargs):
        raise model_tiering.LedgerReadError(ledger, OSError("unreadable"))

    monkeypatch.setattr(model_tiering, "top_callers", unreadable)
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert "attribution unavailable (ledger could not be read)" in spy.sent[0][0]
    assert "no attributed dispatches" not in spy.sent[0][0]
    monkeypatch.setattr(model_tiering, "top_callers", lambda **kwargs: [])
    _usage_file(monkeypatch, tmp_path, "codex", [_window(60, resets_in_hours=170)])
    usage_alerts.check_usage_thresholds("codex", notifier=spy,
        now=datetime.fromisoformat(first["resets_at"]) + timedelta(seconds=1))
    assert "no attributed dispatches in the ledger" in spy.sent[1][0]


@pytest.mark.parametrize("kind", ["missing", "valid", "mixed", "invalid"])
def test_partial_ledger_reports_valid_rows_and_missing_rows(
    monkeypatch, tmp_path, ledger, kind
) -> None:
    valid = {"ts": datetime.now(timezone.utc).isoformat(), "event": "dispatch",
             "provider": "codex", "tier": "top", "caller_kind": "daemon",
             "caller": "apis"}
    lines = ([] if kind == "missing" else
             [json.dumps(valid)] if kind == "valid" else
             [json.dumps(valid), "{broken"] if kind == "mixed" else ["{broken"])
    if lines:
        ledger.write_text("\n".join(lines) + "\n")
    expected_bad = int(kind in ("mixed", "invalid"))
    expected_good = int(kind in ("mixed", "valid"))
    for report in (model_tiering.tier_counts(), model_tiering.usage_totals(),
                   model_tiering.attribution_summary()):
        assert report["unreadable_rows"] == expected_bad
        assert report["attribution_available"] is (kind != "invalid")
    summary = model_tiering.attribution_summary()
    assert summary["dispatch_rows"] == expected_good
    text = model_tiering.render_attribution_summary(summary)
    assert ("totals are incomplete" in text) is (kind == "mixed")
    assert ("attribution unavailable" in text) is (kind == "invalid")
    if expected_bad:
        with pytest.raises(model_tiering.LedgerPartialReadError) as error:
            list(model_tiering.iter_ledger_rows())
        assert error.value.unreadable_rows == expected_bad
    else:
        assert len(list(model_tiering.iter_ledger_rows())) == expected_good
    callers, unreadable = model_tiering.top_callers(provider="codex", with_read_status=True)
    assert unreadable == expected_bad
    assert len(callers) == expected_good
    window = _window(60)
    _usage_file(monkeypatch, tmp_path, "codex", [window])
    spy = _SpyNotifier()
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    message = spy.sent[0][0]
    assert ("unreadable ledger rows" in message) is bool(expected_bad)
    assert ("attribution unavailable" in message) is (kind == "invalid")
    assert ("no attributed dispatches" in message) is (kind == "missing")


@pytest.mark.parametrize("name", [
    "quarter:part", "caf\u00e9", "cafe\u0301", "budget%used",
    "x" * 64, "emoji\U0001f9ed", "line\nfeed",
])
def test_real_writer_reset_correction_keeps_one_receipt_for_escaped_name(
    monkeypatch, tmp_path, ledger, name
) -> None:
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "held.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    spy = _SpyNotifier()
    first = datetime.now(timezone.utc) + timedelta(days=2)
    corrected = first + timedelta(days=1)
    for end in (first, corrected):
        harness_router.record_usage("codex", [{"name": name, "used_percent": 60,
                                                "resets_at": end.isoformat()}])
        usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert len([item for item in spy.delivered if "Provider usage:" in item]) == 1
    old = usage_alerts.dedupe_key("codex", name, first.isoformat(), 50)
    new = usage_alerts.dedupe_key("codex", name, corrected.isoformat(), 50)
    assert spy.inner._dedupe_journal[old]["migration_to"] == new
    assert spy.inner._dedupe_journal[new]["state"] == "delivered"
    assert held_view.parse_usage_key(new) == (
        "codex", held_view.canonical_window_name(name),
        corrected.isoformat(timespec="seconds"), 50,
    )


def test_legacy_colon_and_unicode_names_migrate_uncertain_receipts(
    monkeypatch, tmp_path, ledger
) -> None:
    from lib.notify import held_view, neotoma_sink

    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "held.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    reset = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    for index, (old_name, new_name) in enumerate((
        ("topic:part", "topic:part"), ("cafe\u0301", "caf\u00e9"),
        ("rate%used", "rate%used"),
    )):
        spy = _SpyNotifier()
        old = f"provider-usage-threshold:codex:{old_name}:{reset}:50"
        spy.inner._mark_dedupe_sending((old,), "apis")
        spy.inner._mark_delivery_uncertain((old,), "synthetic uncertain outcome")
        harness_router.record_usage("codex", [{"name": new_name,
                                                "used_percent": 60,
                                                "resets_at": reset}])
        usage_alerts.check_usage_thresholds("codex", notifier=spy)
        new = usage_alerts.dedupe_key("codex", new_name, reset, 50)
        assert not [message for message in spy.delivered
                    if "Provider usage:" in message], index
        assert spy.inner._dedupe_journal[old]["migration_to"] == new
        assert spy.inner._dedupe_journal[new]["state"] == "delivered_unconfirmed"
        assert any(row["key"] == new for row in held_view.read_recovery(spy.inner._digest_path))


def test_usage_key_round_trips_generated_names() -> None:
    from lib.notify import held_view
    import random

    randomizer = random.Random(7)
    alphabet = "aA: %/\n\t\x00\U0001f9ed\u00e9e\u0301"
    for _ in range(400):
        name = "x" + "".join(randomizer.choice(alphabet)
                             for _ in range(randomizer.randrange(1, 63)))
        canonical = held_view.canonical_window_name(name)
        if canonical == "unavailable":
            continue
        key = held_view.usage_key("codex", name, "2026-12-01T12:30:00+00:00", 50)
        assert held_view.parse_usage_key(key) == (
            "codex", canonical, "2026-12-01T12:30:00+00:00", 50,
        )


@pytest.mark.parametrize("name,span", [
    ("monthly", timedelta(days=31)),
    ("weekly_all", timedelta(days=7)),
    ("five_hour", timedelta(hours=5)),
    ("scope:part%\U0001f9ed", timedelta(days=28)),
])
@pytest.mark.parametrize("ledger_state", ["missing", "partial", "invalid"])
@pytest.mark.parametrize("after_end", [False, True])
def test_usage_alert_recovery_state_matrix(
    monkeypatch, tmp_path, ledger, name, span, ledger_state, after_end
) -> None:
    """Recovery, key encoding and ledger completeness share one real send path."""
    from lib.notify import held_view

    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "held.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    epoch = datetime.now(timezone.utc)
    old_end = epoch + span
    harness_router.record_usage("codex", [{"name": name, "used_percent": 60,
                                            "resets_at": old_end.isoformat()}])
    epoch = datetime.now(timezone.utc)
    marker = {"at": epoch.isoformat(),
              "windows": held_view.recovery_window_ends(epoch.isoformat())}
    canonical = held_view.canonical_window_name(name)
    assert marker["windows"]["codex"][canonical] == old_end.isoformat()
    spy = _SpyNotifier()
    assert spy.inner._persist_dedupe_journal({held_view.RECOVERED_AT: marker})
    if ledger_state != "missing":
        rows = [] if ledger_state == "invalid" else [json.dumps({
            "ts": (old_end + span - timedelta(hours=1) if after_end else epoch).isoformat(),
            "event": "dispatch", "tier": "top",
            "provider": "codex", "caller_kind": "daemon", "caller": "apis",
        })]
        ledger.write_text("\n".join([*rows, "{broken"]) + "\n")
    now = old_end + timedelta(seconds=1) if after_end else epoch + timedelta(seconds=1)
    spy.inner._now_local = lambda: now
    new_end = old_end + span
    harness_router.record_usage("codex", [{"name": name, "used_percent": 60,
                                            "resets_at": new_end.isoformat()}])
    usage_alerts.check_usage_thresholds("codex", notifier=spy, now=now)
    delivered = [message for message in spy.delivered if "Provider usage:" in message]
    assert len(delivered) == int(after_end)
    key = usage_alerts.dedupe_key("codex", name, new_end.isoformat(), 50)
    assert held_view.parse_usage_key(key)[1] == canonical
    assert spy.inner._dedupe_journal[key]["state"] == (
        "delivered" if after_end else "delivered_unconfirmed"
    )
    if after_end:
        assert ("unreadable ledger rows" in delivered[0]) is (ledger_state != "missing")
        assert ("attribution unavailable" in delivered[0]) is (ledger_state == "invalid")


def test_partial_usage_snapshot_is_reported_without_a_second_usage_key(
    monkeypatch, tmp_path, ledger
) -> None:
    from lib.notify import neotoma_sink

    monkeypatch.setattr(neotoma_sink, "migrate_key", lambda *args: True)
    spy = _SpyNotifier()
    first = datetime.now(timezone.utc) + timedelta(days=3)
    corrected = first + timedelta(days=1)
    _usage_file(monkeypatch, tmp_path, "codex", [{
        "name": "scope:part", "used_percent": 60, "resets_at": first.isoformat(),
    }])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert len([item for item in spy.delivered if "Provider usage:" in item]) == 1
    _usage_file(monkeypatch, tmp_path, "codex", [
        {"name": "scope:part", "used_percent": 60, "resets_at": corrected.isoformat()},
        {"name": "other", "used_percent": 60, "resets_at": "broken"},
    ])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert len([item for item in spy.delivered if "Provider usage:" in item]) == 1
    assert any("Usage snapshot unreadable" in item for item in spy.delivered)
    _usage_file(monkeypatch, tmp_path, "codex", [{
        "name": "scope:part", "used_percent": 60, "resets_at": corrected.isoformat(),
    }])
    usage_alerts.check_usage_thresholds("codex", notifier=spy)
    assert len([item for item in spy.delivered if "Provider usage:" in item]) == 1


def test_provider_report_with_bad_sibling_labels_delivered_alert_incomplete(
    monkeypatch, tmp_path, ledger
) -> None:
    import usage_probe

    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    reset = datetime.now(timezone.utc) + timedelta(days=3)
    output = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "unifiedWindows": {
            "five_hour": {"utilization": "bad", "resetsAt": reset.timestamp()},
            "seven_day": {"utilization": 0.6, "resetsAt": reset.timestamp()},
        },
    }})
    result = usage_probe.probe_claude(
        "/bin/claude", env={}, run=lambda *args, **kwargs:
        subprocess.CompletedProcess(args, 0, output, ""),
    )
    assert result.ok and result.reading_failure == "1 unreadable provider windows"
    harness_router.record_usage("claude", result.windows, provider_probe=True,
                                probe_detail=result.reading_failure)
    spy = _SpyNotifier()
    usage_alerts.check_usage_thresholds("claude", notifier=spy)
    messages = [item for item in spy.delivered if "Provider usage:" in item]
    assert len(messages) == 1
    assert "Usage reading incomplete: 1 provider windows unreadable" in messages[0]


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
        usage_alerts.dedupe_key("cursor", "monthly", None, 50),
    ] * 2


def test_no_reset_rollover_waits_for_in_flight_delivery(monkeypatch, tmp_path, ledger) -> None:
    import threading
    import time

    spy = _SpyNotifier()
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(usage_alerts, "ALERT_SEND_TIMEOUT_SECONDS", 0.02)
    key = usage_alerts.dedupe_key("cursor", "monthly", None, 50)

    def slow_transport(message, force=False, email_eligible=True):
        entered.set()
        assert release.wait(timeout=2)
        spy.delivered.append(message)
        return True

    spy.inner._deliver = slow_transport
    other = _SpyNotifier()
    _usage_file(monkeypatch, tmp_path, "cursor", [{"name": "monthly", "used_percent": 60}])
    try:
        assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
        assert entered.wait(timeout=2)
        _usage_file(monkeypatch, tmp_path, "cursor", [{"name": "monthly", "used_percent": 5}])
        usage_alerts.check_usage_thresholds("cursor", notifier=other)
        assert key in other.inner._dedupe_journal
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and spy.inner._dedupe_journal.get(key, {}).get("state") != "delivered":
        time.sleep(0.01)
    assert spy.inner._dedupe_journal[key]["state"] == "delivered"
    # The sender marks delivery before it releases its lock, so a check that
    # lands in between skips and a later one clears the key. Assert the
    # rollover happens once the in-flight send has fully finished.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        usage_alerts.check_usage_thresholds("cursor", notifier=spy)
        if key not in spy.inner._dedupe_journal:
            break
        time.sleep(0.01)
    assert key not in spy.inner._dedupe_journal


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


@pytest.mark.parametrize("contents", [
    "{not json", "[]", "{}", '{"cursor": []}',
    '{"cursor": {}}', '{"cursor": {"windows": [1]}}',
    '{"cursor": {"windows": [{"used_percent": 55}, {}]}}',
])
def test_unreadable_usage_snapshot_has_one_held_notice(
    monkeypatch, tmp_path, ledger, capsys, contents
) -> None:
    from lib.notify import held_view

    path = tmp_path / "harness-usage.json"
    path.write_text(contents)
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(path))
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    spy = _SpyNotifier(silenced=True)
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert len(spy.sent) == 1
    assert len(held_view.read_queue(tmp_path / "digest.json")) == 1
    held_view.main([])
    output = capsys.readouterr().out
    assert str(path) in output
    assert "harness_usage.py refresh" in output
    hook = Path(__file__).resolve().parents[3] / ".claude" / "hooks" / "user_prompt_submit.py"
    env = {**os.environ, "HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path)}
    run = subprocess.run(
        [sys.executable, str(hook)], input='{"session_id":"synthetic"}',
        text=True, capture_output=True, env=env, timeout=10,
    )
    assert run.returncode == 0
    assert "APIS_HARNESS_USAGE_FILE" in run.stdout
    assert "harness_usage.py refresh" in run.stdout


def test_missing_usage_snapshot_does_not_raise_notice(monkeypatch, tmp_path, ledger) -> None:
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "missing.json"))
    spy = _SpyNotifier(silenced=True)
    assert usage_alerts.check_usage_thresholds("cursor", notifier=spy) == []
    assert spy.sent == []


@pytest.mark.parametrize("writer", [
    "usage", "exhausted", "probe_available", "probe_unknown", "cooling",
    "model_cooling", "probe_failure", "capacity_observation",
])
def test_each_usage_writer_shape_keeps_other_provider_thresholds(
    writer, monkeypatch, tmp_path, ledger
) -> None:
    import time

    path = tmp_path / "usage.json"
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(path))
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    harness_router.record_usage("claude", [_window(55)])
    now = time.time()
    writers = {
        "usage": lambda: harness_router.record_usage("codex", [_window(2)]),
        "exhausted": lambda: harness_router.record_exhausted("codex", now + 3600),
        "probe_available": lambda: harness_router.record_probe_available("codex", source="synthetic"),
        "probe_unknown": lambda: harness_router.record_probe_unknown("codex", source="synthetic", detail="unknown"),
        "cooling": lambda: harness_router.record_cooling("codex", now + 3600, reason="synthetic"),
        "model_cooling": lambda: harness_router.record_model_cooling("codex", "sol", now + 3600, reason="synthetic"),
        "probe_failure": lambda: harness_router.record_probe_failure("codex", "synthetic"),
        "capacity_observation": lambda: harness_router.record_capacity_observation("codex", source="synthetic"),
    }
    writers[writer]()
    assert harness_router.usage_snapshot_problem(path) is None
    spy = _SpyNotifier()
    assert len(usage_alerts.check_usage_thresholds("claude", notifier=spy)) == 1
    assert len(spy.delivered) == 1


def test_malformed_sibling_still_checks_readable_threshold(monkeypatch, tmp_path, ledger) -> None:
    path = _usage_file(monkeypatch, tmp_path, "claude", [_window(55)])
    data = json.loads(path.read_text())
    data["codex"] = []
    path.write_text(json.dumps(data))
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    spy = _SpyNotifier()
    assert len(usage_alerts.check_usage_thresholds("claude", notifier=spy)) == 1
    assert any("Usage snapshot unreadable" in message for message in spy.delivered)
    assert any("crossed 50%" in message for message in spy.delivered)


def test_probe_preserves_and_reports_unreadable_snapshot(monkeypatch, tmp_path, ledger) -> None:
    import usage_probe

    path = tmp_path / "usage.json"
    original = b"{malformed synthetic bytes"
    path.write_bytes(original)
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(path))
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    monkeypatch.setenv("APIS_USAGE_PROBE", "on")
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    spy = _SpyNotifier(silenced=True)
    monkeypatch.setattr(usage_alerts, "_default_journal_factory", lambda: spy)
    monkeypatch.setattr(usage_alerts, "_default_notifier_factory", lambda: spy)
    reset = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp())
    shape = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "resetsAt": reset, "rateLimitType": "seven_day",
        "utilization": 0.55, "isUsingOverage": False,
    }})

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, shape, "")

    outcome = usage_probe.refresh_usage_if_stale(
        {"claude": "/bin/claude"}, env={"PATH": "/usr/bin"},
        run=fake_run, alert_in_background=False,
    )
    assert outcome == {"claude": "refreshed"}
    assert path.with_name(path.name + ".unreadable").read_bytes() == original
    assert harness_router.usage_snapshot_problem(path) is None
    assert len([item for item in spy.sent if "Usage snapshot unreadable" in item[0]]) == 1
    assert len([item for item in spy.sent if "crossed 50%" in item[0]]) == 1


def test_repeated_usage_writes_keep_first_and_latest_unreadable_versions(
    monkeypatch, tmp_path, ledger
) -> None:
    path = tmp_path / "usage.json"
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(path))
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    first = b'{"cursor": [], "codex": {"windows": []}}'
    later = b'{"cursor": {"windows": []}, "codex": []}'
    final = b'{"cursor": [], "codex": []}'
    for raw in (first, first, later, final, final):
        path.write_bytes(raw)
        harness_router.record_cooling("claude", 1_900_000_000, reason="synthetic")
    copies = list(tmp_path.glob("usage.json.unreadable*"))
    assert len(copies) == 2
    assert (tmp_path / "usage.json.unreadable").read_bytes() == first
    assert (tmp_path / "usage.json.unreadable.latest").read_bytes() == final


def test_refused_source_notice_outside_silence_reaches_hook(
    monkeypatch, tmp_path, ledger
) -> None:
    from lib.notify import held_view

    path = tmp_path / "usage.json"
    path.write_text("broken")
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(path))
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    spy = _SpyNotifier(deliver=False, silenced=False)
    assert usage_alerts.check_usage_thresholds("claude", notifier=spy) == []
    assert len(held_view.read_queue(tmp_path / "digest.json")) == 1
    assert held_view.summary()["usage_snapshot_unreadable"] == 1
    hook = Path(__file__).resolve().parents[3] / ".claude" / "hooks" / "user_prompt_submit.py"
    run = subprocess.run(
        [sys.executable, str(hook)], input='{"session_id":"synthetic"}',
        text=True, capture_output=True,
        env={**os.environ, "HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path)},
        timeout=10,
    )
    assert run.returncode == 0
    assert "APIS_HARNESS_USAGE_FILE" in run.stdout
    assert "harness_usage.py refresh" in run.stdout


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


@pytest.mark.parametrize("name,reset", [
    ("window_" + "9" * 5000 + "m", None),
    ("weekly", "9999-12-31T23:59:59-23:59"),
    ("weekly", "1900-01-01T00:00:00Z"),
    ("weekly", "2010-01-01T00:00:00Z"),
    ("weekly", "2030-01-01T00:00:00Z"),
    ("weekly", "2200-01-01T00:00:00Z"),
    (42, None),
])
def test_malformed_window_metadata_reports_unreadable_snapshot_without_usage_key(
    monkeypatch, tmp_path, ledger, name, reset,
) -> None:
    import harness_router

    window = {"name": name, "used_percent": 80}
    if reset is not None:
        window["resets_at"] = reset
    with pytest.raises(ValueError):
        harness_router.record_usage("cursor", [window])
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    spy = _SpyNotifier()
    usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    assert len(spy.delivered) == 1
    assert "Usage snapshot unreadable" in spy.delivered[0]
    assert "Provider usage:" not in spy.delivered[0]


@pytest.mark.parametrize("percent", [float("nan"), float("inf"), float("-inf"), -1, 101])
def test_record_usage_rejects_invalid_percentage(monkeypatch, tmp_path, percent) -> None:
    import harness_router

    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    with pytest.raises(ValueError):
        harness_router.record_usage("cursor", [{"name": "weekly", "used_percent": percent}])
    assert not (tmp_path / "usage.json").exists()


def test_stale_usage_alert_labels_last_observation(monkeypatch, tmp_path, ledger) -> None:
    path = _usage_file(monkeypatch, tmp_path, "cursor", [_window(80)])
    doc = json.loads(path.read_text())
    doc["cursor"]["observed_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    path.write_text(json.dumps(doc))
    spy = _SpyNotifier()
    usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    assert len(spy.delivered) == 1
    assert "was last observed at" in spy.delivered[0]


@pytest.mark.parametrize("all_notices", [False, True])
def test_operator_clear_settles_usage_thresholds_without_reopening(
    monkeypatch, tmp_path, ledger, all_notices,
) -> None:
    from lib.notify import held_view

    window = _window(52)
    _usage_file(monkeypatch, tmp_path, "cursor", [window])
    spy = _SpyNotifier(silenced=True)
    spy.inner._digest_path = tmp_path / "held.json"
    spy.inner._dedupe_path = tmp_path / "dedupe.json"
    spy.inner.should_flush_digest = lambda: False
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(spy.inner._digest_path))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(spy.inner._dedupe_path))
    usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    first = usage_alerts.dedupe_key("cursor", window["name"], window["resets_at"], 50)
    if all_notices:
        window["used_percent"] = 76
        _usage_file(monkeypatch, tmp_path, "cursor", [window])
        usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    assert held_view.main(["--all"] if all_notices else []) == 0
    assert held_view.main(["--clear"]) == 0
    assert spy.inner._dedupe_journal[first]["cleared"] is True
    prior = len(spy.sent)
    usage_alerts.check_usage_thresholds("cursor", notifier=spy)
    assert len(spy.sent) == prior
    if not all_notices:
        window["used_percent"] = 76
        _usage_file(monkeypatch, tmp_path, "cursor", [window])
        usage_alerts.check_usage_thresholds("cursor", notifier=spy)
        assert len(spy.sent) == prior + 1
