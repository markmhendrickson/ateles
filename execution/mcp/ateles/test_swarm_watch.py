#!/usr/bin/env python3
"""Tests for get_task_timeline, watch_swarm and watch.py (ateles#1275, slice 1).

Nothing here reaches a live Neotoma. `FakeNeotoma` answers the three endpoints
the tools use (`GET /entities/{id}[/relationships]`, `POST /entities/query`,
`POST /observations/query`) from recorded fixture data, with the same
semantics the hosted instance has: observations newest first, `created_since`
as an inclusive lower bound, `offset`/`limit` paging, `total` counting every
match.

The fixture is task ent_18cb45736689441229b2b7f4 as recorded from the hosted
instance on 2026-09-25 — the task the design comment on ateles#1275
reconstructed end to end. See fixtures/task_timeline_*.json `_about`.

Run: .mcp-venv/bin/python -m pytest execution/mcp/ateles/test_swarm_watch.py -q
"""

from __future__ import annotations

import copy
import inspect
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import server as srv  # noqa: E402

FIXTURE = json.loads(
    (_HERE / "fixtures" / "task_timeline_ent_18cb45736689441229b2b7f4.json").read_text()
)
TASK = FIXTURE["task_id"]
CHECKPOINT = "ent_95d94353e7e8a5ec06c28334"
FIXED_NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class FakeNeotoma:
    def __init__(self, fixture: dict):
        self.entities: dict[str, dict] = copy.deepcopy(fixture["entities"])
        self.observations: list[dict] = copy.deepcopy(fixture["observations"])
        self.relationships: dict[str, list] = copy.deepcopy(fixture.get("relationships", {}))
        self.related: dict[str, dict] = copy.deepcopy(fixture.get("related_entities", {}))
        self.calls: list[tuple[str, str, dict]] = []
        self.fail = lambda method, path, body: False
        self._seq = 0

    # -- write helpers used by tests to simulate the swarm moving --
    def add_observation(self, entity_id: str, entity_type: str, at: str, fields: dict, **extra) -> dict:
        self._seq += 1
        obs = {
            "id": f"test-obs-{self._seq:04d}",
            "entity_id": entity_id,
            "entity_type": entity_type,
            "observed_at": at,
            "fields": fields,
            "idempotency_key": extra.get("idempotency_key"),
        }
        self.observations.append(obs)
        ent = self.entities.setdefault(
            entity_id,
            {"entity_id": entity_id, "entity_type": entity_type, "snapshot": {}, "created_at": at},
        )
        ent["snapshot"].update(fields)
        ent["last_observation_at"] = at
        return obs

    # -- the transport --
    def get(self, path: str, params: dict | None = None):
        self.calls.append(("GET", path, {}))
        if self.fail("GET", path, {}):
            srv._record_transport_error("request_failed", "GET", path, "simulated")
            return None
        route = path.split("?")[0]
        parts = route.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "entities" and parts[2] == "relationships":
            return {
                "relationships": self.relationships.get(parts[1], []),
                "related_entities": self.related.get(parts[1], {}),
            }
        if len(parts) == 2 and parts[0] == "entities":
            ent = self.entities.get(parts[1])
            if ent is None:
                srv._record_transport_error("not_found", "GET", path, "HTTP 404")
            return copy.deepcopy(ent)
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, body: dict, **_kw):
        self.calls.append(("POST", path, dict(body)))
        if self.fail("POST", path, body):
            srv._record_transport_error("request_failed", "POST", path, "simulated")
            return None
        if path == "/observations/query":
            rows = self.observations
            if body.get("entity_id"):
                rows = [o for o in rows if o["entity_id"] == body["entity_id"]]
            if body.get("entity_type"):
                rows = [o for o in rows if o["entity_type"] == body["entity_type"]]
            if body.get("created_since"):
                since = _ts(body["created_since"])
                rows = [o for o in rows if _ts(o["observed_at"]) >= since]
            rows = sorted(rows, key=lambda o: _ts(o["observed_at"]), reverse=True)
            offset, limit = body.get("offset", 0), body.get("limit", 100)
            return {
                "observations": copy.deepcopy(rows[offset: offset + limit]),
                "total": len(rows),
                "limit": limit,
                "offset": offset,
            }
        if path == "/entities/query":
            rows = [e for e in self.entities.values() if e["entity_type"] == body["entity_type"]]
            for field, cond in (body.get("snapshot_filters") or {}).items():
                rows = [e for e in rows if e["snapshot"].get(field) == cond["value"]]
            rows.sort(key=lambda e: e["entity_id"])
            return {"entities": copy.deepcopy(rows[: body.get("limit", 100)]), "total": len(rows)}
        raise AssertionError(f"unexpected POST {path}")


@pytest.fixture
def fake(monkeypatch):
    neo = FakeNeotoma(FIXTURE)
    monkeypatch.setattr(srv, "_get", neo.get)
    monkeypatch.setattr(srv, "_post", neo.post)
    # Pin the wall clock the cursor-age cap reads, so the fixture's dates do
    # not age out of it as real time passes. raising=False: absent before the
    # cap existed.
    monkeypatch.setattr(srv, "_watch_now", lambda: FIXED_NOW, raising=False)
    return neo


@pytest.fixture
def clock(monkeypatch):
    """A fake clock the long-poll sleeps against, so no test really waits."""
    state = {"now": 1000.0, "sleeps": [], "on_sleep": None}

    def sleep(seconds: float) -> None:
        state["sleeps"].append(seconds)
        state["now"] += seconds
        if state["on_sleep"]:
            state["on_sleep"](len(state["sleeps"]))

    monkeypatch.setattr(srv, "_watch_clock", lambda: state["now"])
    monkeypatch.setattr(srv, "_watch_sleep", sleep)
    return state


# ── get_task_timeline ────────────────────────────────────────────────────────


def test_timeline_reproduces_the_fixture_task_event_for_event(fake):
    """The design's reconstruction, reproduced from the record by the tool.

    Goes red if any source is dropped: without the harness_event scan the two
    runner rows vanish, without checkpoint history the four checkpoint rows do.
    """
    out = srv._get_task_timeline(TASK)
    assert "error" not in out, out
    got = [(e["at"], e["kind"], e.get("value")) for e in out["timeline"]]
    expected = FIXTURE["expected_timeline"]
    assert [(at, kind) for at, kind, _ in got] == [(at, kind) for at, kind, _ in expected]
    for (at, kind, value), (_, _, want) in zip(got, expected):
        if want is not None:
            assert value == want, (at, kind, value, want)
    for entry in out["timeline"]:
        assert entry["source"]["type"] and entry["at"], entry


def test_timeline_names_the_runner_and_its_outcome(fake):
    out = srv._get_task_timeline(TASK)
    runner = [e for e in out["timeline"] if e["kind"].startswith("runner_")]
    assert [(e["kind"], e["agent"], e["provider"]) for e in runner] == [
        ("runner_started", "corvus", "codex"),
        ("runner_ended", "corvus", "codex"),
    ]
    assert runner[1]["outcome"] == "timeout"
    assert runner[1]["duration_ms"] == 1800045
    assert out["current"]["phase"] == "ended"
    assert out["current"]["agent"] == "corvus"
    assert out["output"]["runner_outcome"] == "timeout after 1800s"
    assert out["task"]["status"] == "failed" and out["task"]["terminal"] is False
    status_writers = {e["by"] for e in out["timeline"] if e["kind"] == "status"}
    assert "apis" in status_writers


def test_timeline_states_what_it_joined_and_what_it_could_not(fake):
    out = srv._get_task_timeline(TASK)
    by_source = {s["source"]: s for s in out["sources"]}
    assert by_source["task observations"]["status"] == "joined"
    assert by_source["checkpoint_brief"]["status"] == "joined"
    assert by_source["harness_event runner events"]["count"] == 2
    assert by_source["PR and review events"]["status"] == "not_joinable"
    assert by_source["participation_record"]["status"] == "none_found"
    joined = " ".join(out["gaps"])
    assert "scanned window" in joined
    assert "host-local" in joined
    assert out["window"]["complete"] is True


def test_timeline_without_runner_events_says_none_found(fake):
    fake.observations = [
        o for o in fake.observations if (o["fields"].get("task_entity_id") != TASK or o["entity_type"] != "harness_event")
    ]
    out = srv._get_task_timeline(TASK)
    assert not [e for e in out["timeline"] if e["kind"].startswith("runner_")]
    by_source = {s["source"]: s for s in out["sources"]}
    assert by_source["harness_event runner events"]["status"] == "none_found"


def test_timeline_scan_reads_the_window_not_the_traffic_after_it(fake):
    """40 later rows sit newest-first ahead of the window; the scan skips them."""
    srv._get_task_timeline(TASK)
    scans = [
        body
        for method, path, body in fake.calls
        if path == "/observations/query"
        and body.get("entity_type") == "harness_event"
        and body.get("limit", 0) > 1
    ]
    assert scans, "no harness_event scan happened"
    later = sum(1 for o in fake.observations if o["observed_at"] >= "2026-09-23")
    assert later == 40
    assert scans[0]["offset"] == later - srv._SCAN_OFFSET_MARGIN


def test_timeline_window_cap_is_reported(fake):
    out = srv._get_task_timeline(TASK, window_hours=1)
    assert any("last 1 h" in g for g in out["gaps"]), out["gaps"]
    assert out["window"]["start"] == "2026-09-22T18:57:49.449Z"


def test_failed_task_read_is_an_error_not_an_empty_timeline(fake):
    fake.fail = lambda method, path, body: body.get("entity_id") == TASK
    out = srv._get_task_timeline(TASK)
    assert "error" in out and "timeline" not in out


def test_failed_runner_scan_is_reported_not_silent(fake):
    fake.fail = lambda method, path, body: body.get("entity_type") == "harness_event"
    out = srv._get_task_timeline(TASK)
    by_source = {s["source"]: s for s in out["sources"]}
    assert by_source["harness_event"]["status"] == "error"
    assert any("runner events could not be read" in g for g in out["gaps"])


def test_timeline_rejects_a_non_entity_id(fake):
    assert "error" in srv._get_task_timeline("not-an-id")


# ── watch_swarm ──────────────────────────────────────────────────────────────


def _baseline(tasks=(TASK,), include_checkpoints=False) -> dict:
    out = srv._watch_swarm(None, list(tasks), include_checkpoints, 0)
    assert "error" not in out, out
    return out


def test_cursor_round_trips():
    cursor = srv._encode_cursor("2026-09-22T19:52:49.449Z", {"b", "a"})
    assert srv._decode_cursor(cursor) == ("2026-09-22T19:52:49.449Z", {"a", "b"})
    assert srv._decode_cursor("w1.not-base64!!") is None
    assert srv._decode_cursor("something-else") is None


def test_baseline_reports_state_and_a_cursor_on_the_records_clock(fake):
    out = _baseline()
    assert out["baseline"] is True
    assert out["tasks"][0]["status"] == "failed"
    since, seen = srv._decode_cursor(out["cursor"])
    newest = max(fake.observations, key=lambda o: _ts(o["observed_at"]))
    assert since == newest["observed_at"] and newest["id"] in seen


def test_watch_returns_a_change_once_and_the_new_cursor_skips_it(fake, clock):
    cursor = _baseline()["cursor"]
    fake.add_observation(
        TASK, "task", "2026-09-26T09:00:00.000Z", {"status": "routed"},
        idempotency_key=f"taskstatus-apis-{TASK}-routed-retry",
    )
    first = srv._watch_swarm(cursor, [TASK], False, 0)
    assert [(c["kind"], c["value"], c["by"]) for c in first["changes"]] == [("status", "routed", "apis")]
    assert first["changes"][0]["subject"] == {"task": TASK}
    second = srv._watch_swarm(first["cursor"], [TASK], False, 0)
    assert second["changes"] == [] and second.get("timed_out") is True
    # Round trip: the cursor handed back is one this server accepts.
    assert srv._decode_cursor(second["cursor"]) is not None


def test_watch_reports_runner_events_and_terminal_status(fake, clock):
    cursor = _baseline()["cursor"]
    fake.add_observation(
        "ent_7ec2df67ca3af97138398109", "harness_event", "2026-09-26T09:00:00.000Z",
        {"event_type": "subprocess", "tool_name": "codex:corvus", "success": "true",
         "task_entity_id": TASK, "output_summary": "ok", "duration_ms": 5},
    )
    fake.add_observation(TASK, "task", "2026-09-26T09:00:01.000Z", {"status": "done"})
    out = srv._watch_swarm(cursor, [TASK], False, 0)
    assert [c["kind"] for c in out["changes"]] == ["runner_ended", "status"]
    assert out["terminal_subjects"] == [TASK]


def test_watch_waits_then_times_out_with_a_fresh_cursor(fake, clock):
    cursor = _baseline()["cursor"]
    out = srv._watch_swarm(cursor, [TASK], False, 20)
    assert out["changes"] == [] and out["timed_out"] is True
    assert sum(clock["sleeps"]) == 20
    assert out["waited_seconds"] == 20
    assert out["polls"] == 1 + len(clock["sleeps"])
    assert srv._decode_cursor(out["cursor"]) is not None


def test_watch_wait_is_capped_at_45_seconds(fake, clock):
    cursor = _baseline()["cursor"]
    out = srv._watch_swarm(cursor, [TASK], False, 500)
    assert out["timed_out"] is True
    assert sum(clock["sleeps"]) == srv.WATCH_MAX_WAIT_SECONDS == 45


def test_watch_returns_as_soon_as_a_change_lands_mid_wait(fake, clock):
    cursor = _baseline()["cursor"]

    def on_sleep(n: int) -> None:
        if n == 2:
            fake.add_observation(TASK, "task", "2026-09-26T09:00:00.000Z", {"status": "routed"})

    clock["on_sleep"] = on_sleep
    out = srv._watch_swarm(cursor, [TASK], False, 45)
    assert [c["value"] for c in out["changes"]] == ["routed"]
    assert out["polls"] == 3 and len(clock["sleeps"]) == 2


def test_watch_error_keeps_the_callers_cursor(fake, clock):
    cursor = _baseline()["cursor"]
    fake.fail = lambda method, path, body: body.get("entity_id") == TASK
    out = srv._watch_swarm(cursor, [TASK], False, 10)
    assert "error" in out and out["cursor"] == cursor
    assert "changes" not in out


def test_watch_validates_its_arguments(fake):
    assert "error" in srv._watch_swarm(None, [], False, 0)
    assert "error" in srv._watch_swarm(None, ["bogus"], False, 0)
    many = [f"ent_{i:024x}" for i in range(srv.WATCH_MAX_TASKS + 1)]
    assert "error" in srv._watch_swarm(None, many, False, 0)
    assert "error" in srv._watch_swarm("w1.garbage", [TASK], False, 0)


def test_checkpoints_are_ordered_by_when_raised_not_by_id(fake, clock):
    """Three checkpoints whose id order is the reverse of their raised order."""
    raised = {
        "ent_cccccccccccccccccccccccc": "2026-09-26T10:00:00.000Z",
        "ent_bbbbbbbbbbbbbbbbbbbbbbbb": "2026-09-26T11:00:00.000Z",
        "ent_aaaaaaaaaaaaaaaaaaaaaaaa": "2026-09-26T12:50:00.000Z",
    }
    # c and b were raised before the cursor ...
    for cid in ("ent_cccccccccccccccccccccccc", "ent_bbbbbbbbbbbbbbbbbbbbbbbb"):
        fake.add_observation(
            cid, "checkpoint_brief", raised[cid],
            {"checkpoint_name": "PLAN", "task_entity_id": "ent_dddddddddddddddddddddddd",
             "status": "awaiting_operator", "reason": "r"},
        )
    cursor = _baseline(include_checkpoints=True)["cursor"]
    # ... and resolved after it, b first; a is raised after it, last of all.
    fake.add_observation("ent_bbbbbbbbbbbbbbbbbbbbbbbb", "checkpoint_brief", "2026-09-26T12:30:00.000Z", {"status": "approved"})
    fake.add_observation("ent_cccccccccccccccccccccccc", "checkpoint_brief", "2026-09-26T12:40:00.000Z", {"status": "rejected"})
    fake.add_observation(
        "ent_aaaaaaaaaaaaaaaaaaaaaaaa", "checkpoint_brief", raised["ent_aaaaaaaaaaaaaaaaaaaaaaaa"],
        {"checkpoint_name": "PLAN", "task_entity_id": "ent_dddddddddddddddddddddddd",
         "status": "awaiting_operator", "reason": "r"},
    )
    out = srv._watch_swarm(cursor, [], True, 0)
    order = [c["subject"]["checkpoint"] for c in out["checkpoints"]]
    assert order == [
        "ent_cccccccccccccccccccccccc",
        "ent_bbbbbbbbbbbbbbbbbbbbbbbb",
        "ent_aaaaaaaaaaaaaaaaaaaaaaaa",
    ], order
    assert order != sorted(order)
    assert [c["kind"] for c in out["checkpoints"]] == [
        "checkpoint_resolved", "checkpoint_resolved", "checkpoint_raised",
    ]
    assert out["checkpoints"][0]["raised_at"] == raised["ent_cccccccccccccccccccccccc"]


def test_checkpoints_on_a_watched_task_arrive_without_include_checkpoints(fake, clock):
    cursor = _baseline()["cursor"]
    fake.add_observation(
        "ent_eeeeeeeeeeeeeeeeeeeeeeee", "checkpoint_brief", "2026-09-26T09:00:00.000Z",
        {"checkpoint_name": "PLAN", "task_entity_id": TASK, "status": "awaiting_operator", "reason": "held"},
    )
    fake.add_observation(
        "ent_ffffffffffffffffffffffff", "checkpoint_brief", "2026-09-26T09:00:01.000Z",
        {"checkpoint_name": "PLAN", "task_entity_id": "ent_dddddddddddddddddddddddd", "status": "awaiting_operator"},
    )
    out = srv._watch_swarm(cursor, [TASK], False, 0)
    assert [c["subject"]["checkpoint"] for c in out["checkpoints"]] == ["ent_eeeeeeeeeeeeeeeeeeeeeeee"]


def test_baseline_pending_checkpoints_are_ordered_by_raised_time(fake):
    for cid, at in (
        ("ent_111111111111111111111111", "2026-09-26T11:00:00.000Z"),
        ("ent_000000000000000000000000", "2026-09-26T12:00:00.000Z"),
        ("ent_222222222222222222222222", "2026-09-26T10:00:00.000Z"),
    ):
        fake.add_observation(
            cid, "checkpoint_brief", at,
            {"task_entity_id": TASK, "status": "awaiting_operator", "checkpoint_name": "PLAN"},
        )
    out = _baseline()
    assert [c["checkpoint_id"] for c in out["pending_checkpoints"]] == [
        "ent_222222222222222222222222",
        "ent_111111111111111111111111",
        "ent_000000000000000000000000",
    ]


# ── watch.py ─────────────────────────────────────────────────────────────────


def test_cli_exits_zero_when_a_change_appears(fake, clock, capsys):
    import watch

    def on_sleep(n: int) -> None:
        if n == 1:
            fake.add_observation(
                TASK, "task", "2026-09-26T09:00:00.000Z", {"status": "routed"},
                idempotency_key=f"taskstatus-apis-{TASK}-routed-retry",
            )

    clock["on_sleep"] = on_sleep
    code = watch.main(["--task", TASK, "--no-env-file", "--timeout", "600"])
    out = capsys.readouterr().out
    assert code == 0
    assert f"{TASK} status: failed -> routed (by apis)" in out
    assert out.strip().splitlines()[-1].startswith("cursor: w1.")
    assert f'get_task_timeline(task_entity_id="{TASK}")' in out


def test_cli_until_terminal_ignores_a_non_terminal_change(fake, clock, capsys):
    import watch

    def on_sleep(n: int) -> None:
        if n == 1:
            fake.add_observation(TASK, "task", "2026-09-26T09:00:00.000Z", {"status": "routed"})
        if n == 3:
            fake.add_observation(TASK, "task", "2026-09-26T09:05:00.000Z", {"status": "done"})

    clock["on_sleep"] = on_sleep
    code = watch.main(["--task", TASK, "--until", "terminal", "--no-env-file", "--timeout", "600"])
    out = capsys.readouterr().out
    assert code == 0
    assert "status: routed -> done" in out


def test_cli_times_out_with_exit_zero_and_says_so(fake, clock, capsys):
    import watch

    code = watch.main(["--task", TASK, "--no-env-file", "--timeout", "60"])
    out = capsys.readouterr().out
    assert code == 0
    assert "no change within 60s" in out
    assert sum(clock["sleeps"]) >= 60


def test_cli_exits_three_when_the_record_cannot_be_read(fake, clock, capsys):
    import watch

    fake.fail = lambda method, path, body: True
    assert watch.main(["--task", TASK, "--no-env-file", "--timeout", "60"]) == 3


# ── read-only by construction ────────────────────────────────────────────────


def test_watch_and_timeline_never_write():
    """_correct and /store are the write paths; none of this code reaches them."""
    import watch

    names = [n for n in dir(srv) if n.startswith(("_watch", "_get_task_timeline", "_scan_", "_observations_of", "_obs_", "_task_obs_", "_checkpoint_entries", "_runner_entry", "_advance_cursor", "_not_a_task"))]
    source = "".join(inspect.getsource(getattr(srv, n)) for n in names if callable(getattr(srv, n)) and inspect.isfunction(getattr(srv, n)))
    # Most of the timeline's reads live on this class, which the function
    # filter above never inspects.
    source += inspect.getsource(srv._TimelineBuilder)
    source += inspect.getsource(watch)
    for forbidden in ("_correct(", '"/correct"', '"/store"', "_post(\"/entities/", "delete"):
        assert forbidden not in source, forbidden


# ── scope: tasks only, allowlisted fields only (Falco on #1281) ─────────────

PAYMENT_PROFILE = "ent_0a1b2c3d4e5f60718293a4b5"
_PAYMENT_FIELDS = {
    "payee_name": "Example Payee Ltd",
    "iban": "placeholder-account-ref-001",
    "amount": 4321.5,
    "currency": "EUR",
}


def _add_payment_profile(fake) -> None:
    fake.add_observation(PAYMENT_PROFILE, "payment_profile", "2026-09-24T09:00:00.000Z", dict(_PAYMENT_FIELDS))


def _leaks_payment_values(out: dict) -> bool:
    blob = json.dumps(out, default=str)
    return any(str(v) in blob for v in _PAYMENT_FIELDS.values())


def test_timeline_refuses_a_payment_profile_id(fake):
    _add_payment_profile(fake)
    out = srv._get_task_timeline(PAYMENT_PROFILE)
    assert "not a task" in out.get("error", ""), out
    assert not _leaks_payment_values(out)


def test_watch_baseline_refuses_a_payment_profile_id(fake):
    _add_payment_profile(fake)
    out = srv._watch_swarm(None, [PAYMENT_PROFILE], False, 0)
    assert "not a task" in out.get("error", ""), out
    assert out.get("refused") is True
    assert not _leaks_payment_values(out)


def test_watch_with_a_hand_built_cursor_refuses_a_payment_profile_id(fake, clock):
    """Falco's probe: a crafted cursor skipped the baseline's check entirely."""
    _add_payment_profile(fake)
    cursor = srv._encode_cursor("2026-09-23T00:00:00.000Z", set())
    out = srv._watch_swarm(cursor, [PAYMENT_PROFILE], False, 0)
    assert "not a task" in out.get("error", ""), out
    assert "changes" not in out
    assert not _leaks_payment_values(out)
    # Refused before any observation of it was read.
    assert not any(body.get("entity_id") == PAYMENT_PROFILE for _, _, body in fake.calls)


def test_an_entity_of_no_stated_type_is_refused(fake):
    fake.add_observation(PAYMENT_PROFILE, "", "2026-09-24T09:00:00.000Z", dict(_PAYMENT_FIELDS))
    fake.entities[PAYMENT_PROFILE].pop("entity_type")
    assert "not a task" in srv._get_task_timeline(PAYMENT_PROFILE).get("error", "")
    assert "not a task" in srv._watch_swarm(None, [PAYMENT_PROFILE], False, 0).get("error", "")


_EXTRA_TASK_FIELDS = {
    "description": "free text holding placeholder-secret-002",
    "notes": "call the payee on their private line",
    "amount": 987.65,
    "beneficiary_name": "Example Beneficiary",
    "payment_method": "wire",
    "linked_email_thread_id": "thread-abc-123",
}


def _emitted_fields(entries: list[dict]) -> set[str]:
    """Every task field name an entry list reports, by kind or by name."""
    by_kind = {v: k for k, v in srv._TASK_FIELD_KINDS.items()} if hasattr(srv, "_TASK_FIELD_KINDS") else {}
    names = set()
    for e in entries:
        if (e.get("source") or {}).get("type") != "task" or e.get("kind") == "created":
            continue
        names.add(e["field"] if e.get("kind") == "field" else by_kind.get(e.get("kind"), e.get("kind")))
    return names


def test_watch_emits_only_allowlisted_task_fields(fake, clock):
    cursor = _baseline()["cursor"]
    fake.add_observation(
        TASK, "task", "2026-09-26T09:00:00.000Z",
        {"status": "routed", "assigned_to": "corvus", "due_date": "2026-10-01", **_EXTRA_TASK_FIELDS},
    )
    out = srv._watch_swarm(cursor, [TASK], False, 0)
    assert "error" not in out, out
    assert _emitted_fields(out["changes"]) == {"status", "assigned_to", "due_date"}
    blob = json.dumps(out, default=str)
    for value in _EXTRA_TASK_FIELDS.values():
        assert str(value) not in blob, value
    for name in _EXTRA_TASK_FIELDS:
        assert f'"{name}"' not in blob, name


def test_timeline_emits_only_allowlisted_task_fields(fake):
    fake.add_observation(
        TASK, "task", "2026-09-22T19:40:00.000Z",
        {"status": "failed", "owner": "apis", **_EXTRA_TASK_FIELDS},
    )
    out = srv._get_task_timeline(TASK)
    assert "error" not in out, out
    emitted = _emitted_fields(out["timeline"])
    assert emitted <= srv._TASK_REPORTED_FIELDS, emitted - srv._TASK_REPORTED_FIELDS
    assert "owner" in emitted
    blob = json.dumps(out, default=str)
    for value in _EXTRA_TASK_FIELDS.values():
        assert str(value) not in blob, value
    assert any("withheld" in g for g in out["gaps"])


def test_the_allowlist_excludes_every_payment_contact_and_free_text_task_field():
    for name in (
        "description", "notes", "details", "summary", "context", "amount", "amount_eur",
        "amount_eur_override", "currency", "payment_method", "payment_approved",
        "payment_event_id", "beneficiary_name", "beneficiary_entity_id", "beneficiary_kind",
        "contact_entity_id", "linked_email_message_id", "linked_email_draft_id",
        "linked_email_thread_id", "location", "conversation_id",
    ):
        assert name not in srv._TASK_REPORTED_FIELDS, name


# ── cursor age cap (Falco on #1281) ──────────────────────────────────────────


def test_a_cursor_older_than_the_cap_is_refused_before_any_read(fake, clock):
    stale = srv._encode_cursor(srv._iso(FIXED_NOW - timedelta(days=8)), set())
    fake.calls.clear()
    out = srv._watch_swarm(stale, [TASK], False, 0)
    assert "fresh watch" in out.get("error", ""), out
    assert out.get("cursor_expired") is True
    assert fake.calls == []


def test_an_epoch_cursor_is_refused(fake, clock):
    epoch = srv._encode_cursor("1970-01-01T00:00:00.000Z", set())
    out = srv._watch_swarm(epoch, [], True, 0)
    assert "fresh watch" in out.get("error", ""), out


def test_a_cursor_inside_the_cap_is_still_accepted(fake, clock):
    recent = srv._encode_cursor(srv._iso(FIXED_NOW - timedelta(days=6)), set())
    out = srv._watch_swarm(recent, [TASK], False, 0)
    assert "error" not in out, out


def test_cli_exits_two_on_an_expired_cursor_without_retrying(fake, clock, capsys):
    import watch

    stale = srv._encode_cursor(srv._iso(FIXED_NOW - timedelta(days=30)), set())
    assert watch.main(["--task", TASK, "--no-env-file", "--cursor", stale]) == 2
    assert clock["sleeps"] == []
    assert "fresh watch" in capsys.readouterr().out


# ── transport-error state is per call, not shared (Falco on #1281) ──────────


def _patched_transport(monkeypatch):
    import httpx

    def request(method, url, **_kw):
        req = httpx.Request(method, url)
        if "/fail" in url:
            return httpx.Response(503, request=req)
        return httpx.Response(200, json={}, request=req)

    monkeypatch.setattr(srv, "NEOTOMA_BEARER_TOKEN", "unit-test-placeholder")
    monkeypatch.setattr(srv.httpx, "request", request)


def test_a_worker_threads_success_does_not_clear_another_calls_error(monkeypatch):
    """The interleaving Falco described, forced into order.

    Call A (another tool, on the loop thread) fails and has not yet read its
    error. A watch poll in a worker thread then succeeds, which clears the
    error. With one shared global, A then reads None and reports "not found"
    for an unreachable Neotoma.
    """
    import threading

    _patched_transport(monkeypatch)
    srv._clear_transport_error()
    assert srv._get("/fail") is None
    worker = threading.Thread(target=srv._get, args=("/ok",))
    worker.start()
    worker.join()
    err = srv._describe_transport_error()
    assert err is not None and "request_failed" in err, err
    srv._clear_transport_error()


def test_asyncio_to_thread_success_does_not_clear_the_callers_error(monkeypatch):
    """Same interleaving through asyncio.to_thread, which is how the tools run."""
    import asyncio

    _patched_transport(monkeypatch)

    async def run() -> str | None:
        srv._get("/fail")
        await asyncio.to_thread(srv._get, "/ok")
        return srv._describe_transport_error()

    err = asyncio.run(run())
    assert err is not None and "request_failed" in err, err


def test_a_worker_threads_failure_does_not_leak_into_another_call(monkeypatch):
    import threading

    _patched_transport(monkeypatch)
    srv._clear_transport_error()
    assert srv._get("/ok") == {}
    worker = threading.Thread(target=srv._get, args=("/fail",))
    worker.start()
    worker.join()
    assert srv._describe_transport_error() is None


def test_within_one_call_the_error_behaves_as_before(monkeypatch):
    _patched_transport(monkeypatch)
    srv._get("/fail")
    assert "request_failed" in (srv._describe_transport_error() or "")
    srv._get("/ok")
    assert srv._describe_transport_error() is None
