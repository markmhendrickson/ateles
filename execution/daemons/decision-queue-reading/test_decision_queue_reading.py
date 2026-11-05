"""Tests for the daily checkpoint queue reading. Synthetic data only."""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_queue_reading as dqr  # noqa: E402

NOW = datetime(2026, 11, 5, 7, 30, tzinfo=timezone.utc)
AUTO = "not scored by a producer — score is a mechanical estimate"
SUMMARY = "Assigned to builder. Action: local_edit. Trigger: created."


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def checkpoint(cid, task_id, hours_ago, *, status="awaiting_operator", reason=AUTO, handler="apis"):
    return {
        "entity_id": cid,
        "last_observation_at": iso(hours_ago),
        "observation_count": 1,
        "snapshot": {
            "status": status,
            "handler": handler,
            "checkpoint_name": "PLAN",
            "gate_action": "checkpoint_plan_approval",
            "reason": reason,
            "blast_radius": "low",
            "plan_summary": SUMMARY,
            "task_entity_id": task_id,
        },
    }


def task(tid, **over):
    snap = {
        "status": "awaiting_approval",
        "blocked_reason": AUTO,
        "title": "Routine edit",
        "assigned_to": "builder",
        "owner": "builder",
    }
    snap.update(over)
    return {"entity_id": tid, "observation_count": 1, "last_observation_at": iso(100), "snapshot": snap}


def corpus():
    """Open checkpoints: 2 real (one 100h old, one 10h old), 3 noise (2 new)."""
    briefs = [
        checkpoint("c1", "t1", 100, reason="please decide this one", handler="human"),  # real, over limit
        checkpoint("c2", "t2", 10, reason="please look", handler="human"),  # real, new
        checkpoint("c3", "t3", 5),  # live_low, new, not real
        checkpoint("c4", "t4", 3),  # live_low, new, not real
        checkpoint("c5", "t5", 200),  # live_low, old, not real
        checkpoint("c6", "t6", 1, status="approved"),  # closed: ignored
    ]
    tasks = [task(f"t{i}") for i in range(1, 7)]
    return briefs, tasks


class FakeNeotoma:
    """Serves cursor pages; records every call. Stores metric entities in memory."""

    def __init__(self, briefs, tasks, *, server_total=None, fail_type=None, profile=None,
                 drop_stored=False, page=2):
        self.rows = {"checkpoint_brief": briefs, "task": tasks}
        self.profile = profile
        self.server_total = server_total
        self.fail_type = fail_type
        self.drop_stored = drop_stored
        self.page = page
        self.calls = []
        self.stored = {}

    def __call__(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        if path == "/store":
            for e in body["entities"]:
                if not self.drop_stored:
                    self.stored.setdefault(e["entity_type"], {})[
                        e.get("reading_key") or e.get("metric_id")
                    ] = e
            return {"success": True, "observations_created": len(body["entities"])}
        assert path == "/entities/query", path
        etype = body["entity_type"]
        if etype == self.fail_type:
            raise OSError("boom")
        if etype == "operator_profile":
            return {"entities": [{"snapshot": self.profile}] if self.profile else [], "total": 0}
        if etype in ("metric_reading", "metric_definition"):
            ents = [{"snapshot": s} for s in self.stored.get(etype, {}).values()]
            return {"entities": ents[body.get("offset", 0):][: body["limit"]], "total": len(ents)}
        rows = self.rows[etype]
        if "snapshot_filters" in body:
            assert "cursor" not in body
            total = (
                self.server_total
                if self.server_total is not None and etype == "checkpoint_brief"
                else sum(1 for r in rows if r["snapshot"]["status"] == body["snapshot_filters"]["status"])
            )
            return {"entities": [], "total": total}
        start = int(body["cursor"]) if body.get("cursor") else 0
        chunk = rows[start:start + self.page]
        nxt = str(start + self.page) if start + self.page < len(rows) else None
        return {"entities": chunk, "total": len(rows), "next_cursor": nxt}


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.setattr(dqr, "PAGE_PAUSE", 0)
    monkeypatch.setattr(dqr.neotoma_io, "PAGE_PAUSE", 0)
    monkeypatch.setenv("DECISION_QUEUE_READING_PATH", str(tmp_path / "out" / "reading.json"))
    monkeypatch.delenv("APIS_OPERATOR_LOGIN", raising=False)
    monkeypatch.delenv("ATELES_OPERATOR_NAMES", raising=False)


def run(fake, *, dry_run=False, tmp_path=None):
    return dqr.run(fake, NOW, dry_run=dry_run, pause=0)


def out_path():
    return Path(os.environ["DECISION_QUEUE_READING_PATH"])


def test_summary_numbers():
    fake = FakeNeotoma(*corpus(), profile={"first_name": "Zed"})
    code, doc = run(fake)
    assert code == 0 and doc["status"] == "ok"
    assert doc["total_open"] == 5
    assert doc["real_decisions"] == 2
    assert doc["real_decisions_over_limit"] == 1
    assert doc["oldest_hours"] == 100.0 and doc["median_hours"] == 100.0
    assert doc["by_class"] == {"raised_by_person_or_session": 2, "live_low": 3}
    assert doc["new_holds_24h"] == 3  # c2, c3, c4
    assert doc["new_holds_24h_real"] == 1 and doc["new_holds_24h_not_real"] == 2
    assert doc["instrument"]["passed"] and doc["instrument"]["tasks_read"] == 6
    assert {r["task_id"] for r in doc["real_decision_list"]} == {"t1", "t2"}
    assert set(doc["real_decision_list"][0]) == {"task_id", "class", "age_hours"}
    on_disk = json.loads(out_path().read_text())
    assert on_disk == json.loads(json.dumps(doc))
    assert "Routine edit" not in out_path().read_text()


def test_readings_stored_and_read_back():
    fake = FakeNeotoma(*corpus(), profile={"first_name": "Zed"})
    run(fake)
    readings = fake.stored["metric_reading"]
    over = readings[f"{dqr.METRIC_OVER_LIMIT}|ateles-checkpoint-queue|2026-11-05"]
    auto = readings[f"{dqr.METRIC_AUTOMATIC}|ateles-checkpoint-queue|2026-11-05"]
    assert over["value"] == 1 and over["status"] == "ok" and over["probe_ok"] is True
    assert over["detail"]["real_decisions"] == 2 and over["detail"]["oldest_hours"] == 100.0
    assert over["detail"]["by_class"] == {"raised_by_person_or_session": 2, "live_low": 3}
    assert auto["value"] == 2 and auto["detail"] == {"total": 3, "real": 1}
    assert set(fake.stored["metric_definition"]) == {dqr.METRIC_OVER_LIMIT, dqr.METRIC_AUTOMATIC}
    assert all(d["cadence"] == "daily" and d["status"] == "proposed"
               for d in fake.stored["metric_definition"].values())


def test_reading_key_idempotent_same_day():
    fake = FakeNeotoma(*corpus(), profile={"first_name": "Zed"})
    run(fake)
    first = set(fake.stored["metric_reading"])
    later = NOW + timedelta(hours=5)
    dqr.run(fake, later, dry_run=False, pause=0)
    assert set(fake.stored["metric_reading"]) == first and len(first) == 2
    dqr.run(fake, NOW + timedelta(days=1), dry_run=False, pause=0)
    assert len(fake.stored["metric_reading"]) == 4


def test_instrument_mismatch_means_no_reading():
    fake = FakeNeotoma(*corpus(), server_total=500, profile={"first_name": "Zed"})
    code, doc = run(fake)
    assert code == 1 and doc["status"] == "invalid"
    assert {"check": "instrument_check", "reason": "count_mismatch"} in doc["failed_checks"]
    assert "real_decisions" not in doc
    assert not fake.stored
    assert not any(c[1] == "/store" for c in fake.calls)
    assert json.loads(out_path().read_text())["status"] == "invalid"


def test_small_drift_is_tolerated():
    fake = FakeNeotoma(*corpus(), server_total=9, profile={"first_name": "Zed"})
    code, doc = run(fake)
    assert code == 0 and doc["status"] == "ok"


@pytest.mark.parametrize("failing", ["checkpoint_brief", "task"])
def test_read_failure_non_zero_and_no_reading(failing, capsys):
    fake = FakeNeotoma(*corpus(), fail_type=failing, profile={"first_name": "Zed"})
    code = dqr.main(["--once"], requester=fake, now=NOW)
    assert code == 1
    assert not fake.stored and not any(c[1] == "/store" for c in fake.calls)
    doc = json.loads(out_path().read_text())
    assert doc["status"] == "invalid" and doc["failed_checks"][0]["check"] == f"read_{failing}"
    assert "real_decisions" not in doc  # no zero is claimed


def test_short_read_is_invalid():
    briefs, tasks = corpus()

    class Short(FakeNeotoma):
        def __call__(self, method, path, body=None, **kw):
            res = super().__call__(method, path, body, **kw)
            if path == "/entities/query" and body.get("entity_type") == "task" and "cursor" not in body:
                res["total"] = 1000  # server says far more rows than the pages delivered
            return res

    code, doc = run(Short(briefs, tasks))
    assert code == 1 and doc["failed_checks"][0]["reason"] == "short_read"


def test_failed_read_back_is_partial_and_non_zero():
    fake = FakeNeotoma(*corpus(), drop_stored=True, profile={"first_name": "Zed"})
    code, doc = run(fake)
    assert code == 1 and doc["status"] == "partial"
    assert {"check": "store_and_verify", "reason": "read_back_failed"} in doc["failed_checks"]


def test_file_is_owner_only_and_atomic(monkeypatch):
    fake = FakeNeotoma(*corpus(), profile={"first_name": "Zed"})
    run(fake)
    assert stat.S_IMODE(out_path().stat().st_mode) == 0o600
    assert [p.name for p in out_path().parent.iterdir()] == ["reading.json"]  # no temp left
    before = out_path().read_text()
    monkeypatch.setattr(dqr.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    code, _ = dqr.run(FakeNeotoma(*corpus(), profile={"first_name": "Zed"}), NOW, dry_run=False, pause=0)
    assert code == 1
    assert out_path().read_text() == before
    assert [p.name for p in out_path().parent.iterdir()] == ["reading.json"]


def test_dry_run_writes_nothing(capsys):
    fake = FakeNeotoma(*corpus(), profile={"first_name": "Zed"})
    code = dqr.main(["--once", "--dry-run"], requester=fake, now=NOW)
    assert code == 0
    assert not out_path().exists() and not out_path().parent.exists()
    assert not any(c[1] == "/store" for c in fake.calls)
    printed = json.loads(capsys.readouterr().out)
    assert printed["real_decisions"] == 2


def test_no_write_path_beyond_metric_store():
    fake = FakeNeotoma(*corpus(), profile={"first_name": "Zed"})
    run(fake)
    writes = [c for c in fake.calls if c[0] != "GET" and c[1] != "/entities/query"]
    assert writes and all(c[0] == "POST" and c[1] == "/store" for c in writes)
    for _, _, body in writes:
        assert {e["entity_type"] for e in body["entities"]} <= {"metric_definition", "metric_reading"}


@pytest.mark.parametrize(
    "call",
    [
        ("POST", "/store", {"entities": [{"entity_type": "task", "title": "x"}]}),
        ("POST", "/store", {"entities": [{"entity_type": "metric_reading"}, {"entity_type": "task"}]}),
        ("POST", "/store", {"entities": []}),
        ("POST", "/correct", {}),
        ("POST", "/resolve_checkpoint", {}),
        ("DELETE", "/entities/x", None),
        ("PATCH", "/entities/x", {}),
    ],
)
def test_guard_refuses_every_other_write(call):
    seen = []
    guarded = dqr.guarded_request(lambda *a, **k: seen.append(a) or {})
    with pytest.raises(PermissionError):
        guarded(*call)
    assert not seen


def test_operator_profile_absent_degrades():
    briefs, tasks = corpus()
    tasks[2] = task("t3", assigned_to="zed")  # owned by a person once the name is known
    with_profile = FakeNeotoma(briefs, tasks, profile={"first_name": "Zed"})
    _, doc = run(with_profile)
    assert doc["by_class"].get("owned_by_person") == 1 and doc["failed_checks"] == []

    without = FakeNeotoma(briefs, tasks, profile=None)
    code, doc = run(without)
    assert code == 0 and doc["status"] == "ok"
    assert doc["by_class"].get("owned_by_person") is None
    assert doc["failed_checks"][0]["check"] == "operator_profile"
    assert doc["real_decisions"] == 2  # person-owned is not real either way
    reading = next(iter(without.stored["metric_reading"].values()))
    assert "classifier default" in reading["coverage_note"]


def test_operator_names_never_reach_the_file():
    fake = FakeNeotoma(*corpus(), profile={"first_name": "Zedediah", "handle": "zedhandle"})
    run(fake)
    text = out_path().read_text().lower()
    assert "zedediah" not in text and "zedhandle" not in text


def test_user_agent_is_set_by_the_shared_client():
    from lib.capabilities import neotoma_http

    assert neotoma_http.USER_AGENT.startswith("ateles-")


def test_cursor_reader_stops_on_repeated_cursor():
    def loop(method, path, body=None, **kw):
        return {"entities": [{"entity_id": "x"}], "total": 99, "next_cursor": "same"}

    with pytest.raises(dqr.ReadFailure) as exc:
        dqr.read_all_by_cursor("task", loop, pause=0)
    assert exc.value.reason == "cursor_repeated"
