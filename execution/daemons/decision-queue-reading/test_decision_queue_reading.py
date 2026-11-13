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
OP_ONLY = "operator-only action — never auto-executable at any confidence"
AUTO = "not scored by a producer — score is a mechanical estimate"
SUMMARY = "Assigned to builder. Action: local_edit. Trigger: created."


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def checkpoint(
    cid,
    task_id,
    hours_ago,
    *,
    status="awaiting_operator",
    reason=AUTO,
    handler="apis",
    summary=SUMMARY,
    count=1,
):
    return {
        "entity_id": cid,
        "last_observation_at": iso(hours_ago),
        "observation_count": count,
        "snapshot": {
            "status": status,
            "handler": handler,
            "checkpoint_name": "PLAN",
            "gate_action": "checkpoint_plan_approval",
            "reason": reason,
            "blast_radius": "low",
            "plan_summary": summary,
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
    return {
        "entity_id": tid,
        "observation_count": 1,
        "last_observation_at": iso(100),
        "snapshot": snap,
    }


def corpus():
    """Open checkpoints: 2 real (one 100h old, one 10h old), 3 noise (2 new)."""
    briefs = [
        checkpoint(
            "c1", "t1", 100, reason="please decide this one", handler="human"
        ),  # real, over limit
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

    def __init__(
        self,
        briefs,
        tasks,
        *,
        server_total=None,
        fail_type=None,
        profile=None,
        drop_stored=False,
        page=2,
    ):
        self.rows = {dqr.CHECKPOINT_RECORD_TYPE: briefs, "task": tasks}
        self.profile = profile
        self.server_total = server_total
        self.fail_type = fail_type
        self.drop_stored = drop_stored
        self.page = page
        self.calls = []
        self.stored = {}
        #: entity id -> (observed_at strings, server total) or an exception
        self.observations = {}

    def __call__(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        if method == "GET":
            assert path.startswith("/entities/") and "/observations?limit=" in path
            entity_id = path.split("/")[2]
            got = self.observations[entity_id]
            if isinstance(got, Exception):
                raise got
            times, total = got
            limit = int(path.rsplit("=", 1)[1])
            return {
                "observations": [{"observed_at": t} for t in times][:limit],
                "total": total,
            }
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
            return {
                "entities": [{"snapshot": self.profile}] if self.profile else [],
                "total": 0,
            }
        if etype in ("metric_reading", "metric_definition"):
            ents = [{"snapshot": s} for s in self.stored.get(etype, {}).values()]
            return {
                "entities": ents[body.get("offset", 0) :][: body["limit"]],
                "total": len(ents),
            }
        rows = self.rows[etype]
        if "snapshot_filters" in body:
            assert "cursor" not in body
            flt = body["snapshot_filters"]["status"]
            # the real server rejects a bare string here (HTTP 400 seen live)
            assert isinstance(flt, dict) and flt["op"] == "eq", (
                "filter must be {op, value}"
            )
            total = (
                self.server_total
                if self.server_total is not None and etype == dqr.CHECKPOINT_RECORD_TYPE
                else sum(1 for r in rows if r["snapshot"]["status"] == flt["value"])
            )
            return {"entities": [], "total": total}
        start = int(body["cursor"]) if body.get("cursor") else 0
        chunk = rows[start : start + self.page]
        nxt = str(start + self.page) if start + self.page < len(rows) else None
        return {"entities": chunk, "total": len(rows), "next_cursor": nxt}


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.setattr(dqr, "PAGE_PAUSE", 0)
    monkeypatch.setattr(dqr.neotoma_io, "PAGE_PAUSE", 0)
    monkeypatch.setenv(
        "DECISION_QUEUE_READING_PATH", str(tmp_path / "out" / "reading.json")
    )
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
    assert set(doc["real_decision_list"][0]) == {
        "task_id",
        "class",
        "age_hours",
        "first_seen",
    }
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
    assert (
        over["detail"]["real_decisions"] == 2
        and over["detail"]["oldest_hours"] == 100.0
    )
    assert over["detail"]["by_class"] == {
        "raised_by_person_or_session": 2,
        "live_low": 3,
    }
    assert auto["value"] == 2 and auto["detail"] == {"total": 3, "real": 1}
    assert set(fake.stored["metric_definition"]) == {
        dqr.METRIC_OVER_LIMIT,
        dqr.METRIC_AUTOMATIC,
        dqr.METRIC_OUTSIDE_FLY,
        dqr.METRIC_FLY_LANE,
    }
    assert all(
        d["cadence"] == "daily" and d["status"] == "proposed"
        for d in fake.stored["metric_definition"].values()
    )


def test_reading_key_idempotent_same_day():
    fake = FakeNeotoma(*corpus(), profile={"first_name": "Zed"})
    run(fake)
    first = set(fake.stored["metric_reading"])
    later = NOW + timedelta(hours=5)
    dqr.run(fake, later, dry_run=False, pause=0)
    assert set(fake.stored["metric_reading"]) == first and len(first) == 4
    dqr.run(fake, NOW + timedelta(days=1), dry_run=False, pause=0)
    assert len(fake.stored["metric_reading"]) == 8


def test_instrument_mismatch_means_no_reading():
    fake = FakeNeotoma(*corpus(), server_total=500, profile={"first_name": "Zed"})
    code, doc = run(fake)
    assert code == 1 and doc["status"] == "invalid"
    assert {"check": "instrument_check", "reason": "count_mismatch"} in doc[
        "failed_checks"
    ]
    assert "real_decisions" not in doc
    assert not fake.stored
    assert not any(c[1] == "/store" for c in fake.calls)
    assert json.loads(out_path().read_text())["status"] == "invalid"


def test_small_drift_is_tolerated():
    fake = FakeNeotoma(*corpus(), server_total=9, profile={"first_name": "Zed"})
    code, doc = run(fake)
    assert code == 0 and doc["status"] == "ok"


@pytest.mark.parametrize("failing", [dqr.CHECKPOINT_RECORD_TYPE, "task"])
def test_read_failure_non_zero_and_no_reading(failing, capsys):
    fake = FakeNeotoma(*corpus(), fail_type=failing, profile={"first_name": "Zed"})
    code = dqr.main(["--once"], requester=fake, now=NOW)
    assert code == 1
    assert not fake.stored and not any(c[1] == "/store" for c in fake.calls)
    doc = json.loads(out_path().read_text())
    assert (
        doc["status"] == "invalid"
        and doc["failed_checks"][0]["check"] == f"read_{failing}"
    )
    assert "real_decisions" not in doc  # no zero is claimed


def test_short_read_is_invalid():
    briefs, tasks = corpus()

    class Short(FakeNeotoma):
        def __call__(self, method, path, body=None, **kw):
            res = super().__call__(method, path, body, **kw)
            if (
                path == "/entities/query"
                and body.get("entity_type") == "task"
                and "cursor" not in body
            ):
                res["total"] = (
                    1000  # server says far more rows than the pages delivered
                )
            return res

    code, doc = run(Short(briefs, tasks))
    assert code == 1 and doc["failed_checks"][0]["reason"] == "short_read"


def test_failed_read_back_is_partial_and_non_zero():
    fake = FakeNeotoma(*corpus(), drop_stored=True, profile={"first_name": "Zed"})
    code, doc = run(fake)
    assert code == 1 and doc["status"] == "partial"
    assert {"check": "store_and_verify", "reason": "read_back_failed"} in doc[
        "failed_checks"
    ]


def test_file_is_owner_only_and_atomic(monkeypatch):
    fake = FakeNeotoma(*corpus(), profile={"first_name": "Zed"})
    run(fake)
    assert stat.S_IMODE(out_path().stat().st_mode) == 0o600
    assert [p.name for p in out_path().parent.iterdir()] == [
        "reading.json"
    ]  # no temp left
    before = out_path().read_text()
    monkeypatch.setattr(
        dqr.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
    )
    code, _ = dqr.run(
        FakeNeotoma(*corpus(), profile={"first_name": "Zed"}),
        NOW,
        dry_run=False,
        pause=0,
    )
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
        assert {e["entity_type"] for e in body["entities"]} <= {
            "metric_definition",
            "metric_reading",
        }


@pytest.mark.parametrize(
    "call",
    [
        ("POST", "/store", {"entities": [{"entity_type": "task", "title": "x"}]}),
        (
            "POST",
            "/store",
            {"entities": [{"entity_type": "metric_reading"}, {"entity_type": "task"}]},
        ),
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
    assert doc["by_class"].get("owned_by_person") == 1 and "operator_profile" not in [
        c["check"] for c in doc["failed_checks"]
    ]

    without = FakeNeotoma(briefs, tasks, profile=None)
    code, doc = run(without)
    assert code == 0 and doc["status"] == "ok"
    assert doc["by_class"].get("owned_by_person") is None
    assert "operator_profile" in [c["check"] for c in doc["failed_checks"]]
    assert doc["real_decisions"] == 2  # person-owned is not real either way
    reading = next(iter(without.stored["metric_reading"].values()))
    assert "classifier default" in reading["coverage_note"]


def test_operator_names_never_reach_the_file():
    fake = FakeNeotoma(
        *corpus(), profile={"first_name": "Zedediah", "handle": "zedhandle"}
    )
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


def _real_ages(doc):
    return {r["task_id"]: r["age_hours"] for r in doc["real_decision_list"]}


def test_refiled_hold_keeps_its_old_age():
    briefs, tasks = corpus()
    _, first = run(FakeNeotoma(briefs, tasks, profile={"first_name": "Zed"}))
    assert _real_ages(first)["t2"] == 10.0
    # a day later t2's hold is re-filed (fresh timestamp); t1 is untouched
    later = NOW + timedelta(hours=24)
    refiled = [dict(b) for b in briefs]
    refiled[1] = checkpoint("c2b", "t2", 0.5, reason="please look", handler="human")
    refiled[1]["last_observation_at"] = (later - timedelta(hours=0.5)).isoformat()
    for i in (0, 2, 3, 4):
        refiled[i] = dict(refiled[i])
    # touch t1 as well: its observation time is now recent
    refiled[0] = checkpoint(
        "c1", "t1", 0, reason="please decide this one", handler="human"
    )
    refiled[0]["last_observation_at"] = later.isoformat()
    code, doc = dqr.run(
        FakeNeotoma(refiled, tasks, profile={"first_name": "Zed"}),
        later,
        dry_run=False,
        pause=0,
    )
    ages = _real_ages(doc)
    assert (
        ages["t2"] == 34.0 and ages["t1"] == 124.0
    )  # old clocks carried, not restarted
    assert doc["real_decisions_over_limit"] == 1 and doc["oldest_hours"] == 124.0
    assert doc["age_basis"] == "first_seen"
    assert doc["failed_checks"] == []


def test_task_that_leaves_the_real_list_restarts_when_it_returns():
    briefs, tasks = corpus()
    run(FakeNeotoma(briefs, tasks, profile={"first_name": "Zed"}))
    gone = [b for b in briefs if b["entity_id"] != "c1"]
    t1 = NOW + timedelta(hours=1)
    _, doc = dqr.run(
        FakeNeotoma(gone, tasks, profile={"first_name": "Zed"}),
        t1,
        dry_run=False,
        pause=0,
    )
    assert "t1" not in _real_ages(doc)
    back = list(gone) + [
        checkpoint("c1x", "t1", 0.0, reason="please decide this one", handler="human")
    ]
    back[-1]["last_observation_at"] = (t1 + timedelta(hours=2)).isoformat()
    _, doc = dqr.run(
        FakeNeotoma(back, tasks, profile={"first_name": "Zed"}),
        t1 + timedelta(hours=3),
        dry_run=False,
        pause=0,
    )
    assert _real_ages(doc)["t1"] == 1.0


@pytest.mark.parametrize(
    "content,reason",
    [
        (None, "missing"),
        ("{not json", "unreadable"),
        ("[]", "unreadable"),
        ('{"status": "invalid"}', "no_carry"),
    ],
)
def test_previous_file_unusable_is_said_in_the_coverage(content, reason):
    if content is not None:
        out_path().parent.mkdir(parents=True)
        out_path().write_text(content)
    fake = FakeNeotoma(*corpus(), profile={"first_name": "Zed"})
    code, doc = run(fake)
    assert code == 0 and doc["status"] == "ok"
    assert any(
        c["check"] == "previous_reading" and reason in c["reason"]
        for c in doc["failed_checks"]
    )
    assert _real_ages(doc)["t1"] == 100.0
    # the stored readings must not read as a clean, fully covered ok
    for rec in fake.stored["metric_reading"].values():
        assert rec["coverage_ratio"] < 1.0
        assert "previous first-seen state unavailable" in rec["coverage_note"]


def test_good_run_with_state_has_full_coverage():
    briefs, tasks = corpus()
    run(FakeNeotoma(briefs, tasks, profile={"first_name": "Zed"}))
    fake = FakeNeotoma(briefs, tasks, profile={"first_name": "Zed"})
    run(fake)
    for rec in fake.stored["metric_reading"].values():
        assert rec["coverage_ratio"] == 1.0 and not rec.get("coverage_note")


def _refile(briefs, later):
    refiled = list(briefs)
    refiled[0] = checkpoint(
        "c1x", "t1", 0, reason="please decide this one", handler="human"
    )
    refiled[0]["last_observation_at"] = later.isoformat()
    return refiled


@pytest.mark.parametrize("invalid_runs", [1, 3])
def test_invalid_runs_keep_the_first_seen_clock(invalid_runs):
    briefs, tasks = corpus()
    run(FakeNeotoma(briefs, tasks, profile={"first_name": "Zed"}))
    for i in range(1, invalid_runs + 1):
        when = NOW + timedelta(hours=i)
        code, doc = dqr.run(
            FakeNeotoma(briefs, tasks, server_total=999, profile={"first_name": "Zed"}),
            when,
            dry_run=False,
            pause=0,
        )
        assert code == 1 and doc["status"] == "invalid"
        on_disk = json.loads(out_path().read_text())
        # the file still says the latest run was invalid and gives no numbers ...
        assert on_disk["status"] == "invalid" and "real_decision_list" not in on_disk
        assert on_disk["generated_at"] == when.strftime("%Y-%m-%dT%H:%M:%SZ")
        # ... and still carries the clock
        assert set(on_disk["first_seen_carry"]) == {"t1", "t2"}
    later = NOW + timedelta(hours=24)
    code, doc = dqr.run(
        FakeNeotoma(_refile(briefs, later), tasks, profile={"first_name": "Zed"}),
        later,
        dry_run=False,
        pause=0,
    )
    assert code == 0 and doc["status"] == "ok"
    assert _real_ages(doc)["t1"] == 124.0  # re-filed hold did not restart the clock
    assert doc["real_decisions_over_limit"] >= 1
    assert not any(c["check"] == "previous_reading" for c in doc["failed_checks"])


def test_invalid_run_after_read_failure_keeps_the_clock():
    briefs, tasks = corpus()
    run(FakeNeotoma(briefs, tasks, profile={"first_name": "Zed"}))
    code, doc = dqr.run(
        FakeNeotoma(briefs, tasks, fail_type="task"),
        NOW + timedelta(hours=1),
        dry_run=False,
        pause=0,
    )
    assert doc["status"] == "invalid"
    assert set(json.loads(out_path().read_text())["first_seen_carry"]) == {"t1", "t2"}


def test_invalid_run_with_no_usable_previous_state_says_so():
    out_path().parent.mkdir(parents=True)
    out_path().write_text("{not json")
    code, doc = dqr.run(
        FakeNeotoma(*corpus(), server_total=999), NOW, dry_run=False, pause=0
    )
    assert doc["status"] == "invalid"
    assert any(c["check"] == "previous_reading" for c in doc["failed_checks"])
    on_disk = json.loads(out_path().read_text())
    assert on_disk["first_seen_carry"] == {}
    assert any(c["check"] == "previous_reading" for c in on_disk["failed_checks"])


def test_status_line_has_headline_numbers_and_date(capsys, monkeypatch):
    fake = FakeNeotoma(*corpus(), profile={"first_name": "Zed"})
    assert dqr.main(["--once"], requester=fake, now=NOW) == 0
    line = capsys.readouterr().out.strip()
    assert "date=2026-11-05" in line
    assert "real_decisions_over_72h=1" in line and "automatic_holds_added_24h=2" in line


def test_dry_run_reads_previous_but_writes_nothing():
    briefs, tasks = corpus()
    run(FakeNeotoma(briefs, tasks, profile={"first_name": "Zed"}))
    before = out_path().read_text()
    refiled = list(briefs)
    refiled[0] = checkpoint(
        "c1", "t1", 0, reason="please decide this one", handler="human"
    )
    later = NOW + timedelta(hours=24)
    refiled[0]["last_observation_at"] = later.isoformat()
    _, doc = dqr.run(
        FakeNeotoma(refiled, tasks, profile={"first_name": "Zed"}),
        later,
        dry_run=True,
        pause=0,
    )
    assert _real_ages(doc)["t1"] == 124.0
    assert out_path().read_text() == before


def test_metric_ids_match_the_stored_definitions():
    defs = {d["metric_id"]: d for d in dqr.own_definitions()}
    assert set(defs) == {
        dqr.METRIC_OVER_LIMIT,
        dqr.METRIC_AUTOMATIC,
        dqr.METRIC_OUTSIDE_FLY,
        dqr.METRIC_FLY_LANE,
    }
    assert dqr.METRIC_OVER_LIMIT.startswith("Q1_") and dqr.METRIC_AUTOMATIC.startswith(
        "Q2_"
    )
    assert dqr.METRIC_OUTSIDE_FLY.startswith("Q3_")
    assert dqr.METRIC_FLY_LANE.startswith("Q4_")
    assert all(
        d["cadence"] == "daily" and d["status"] == "proposed" for d in defs.values()
    )
    fake = FakeNeotoma(*corpus(), profile={"first_name": "Zed"})
    run(fake)
    refs = {r["metric_ref"] for r in fake.stored["metric_reading"].values()}
    assert refs == set(defs) == set(fake.stored["metric_definition"])


# ── holds counted by when they were raised ───────────────────────────────────

DELIVERY = "Assigned to cicada. Action: open_or_merge_pr. Trigger: created."
SESSION = "Coordinator session"


def lane_task(tid, **over):
    base = {"assigned_to": "cicada", "owner": SESSION}
    base.update(over)
    return task(tid, **base)


def lane_hold(cid, tid, hours_ago, **kw):
    kw.setdefault("summary", DELIVERY)
    return checkpoint(cid, tid, hours_ago, **kw)


def obs(*hours_ago):
    """Observation times, newest first as a server may return them."""
    return [iso(h) for h in hours_ago]


def raised_run(briefs, tasks, observations=None, *, now=NOW, dry_run=False):
    fake = FakeNeotoma(briefs, tasks, profile={"first_name": "Zed"})
    fake.observations = observations or {}
    code, doc = dqr.run(fake, now, dry_run=dry_run, pause=0)
    return fake, code, doc


def reads(fake):
    return [c for c in fake.calls if c[0] == "GET"]


def test_a_lane_hold_is_counted_apart_and_shown_open_labelled():
    briefs = [lane_hold("c1", "t1", 3), lane_hold("c2", "t2", 9)]
    tasks = [lane_task("t1"), lane_task("t2")]
    fake, code, doc = raised_run(briefs, tasks)
    assert doc["fly_lane_holds_raised_24h"] == 2
    assert doc["outside_fly_holds_raised_24h"] == 0
    assert doc["by_class"] == {"fly_lane_hold": 2}
    assert doc["real_decisions"] == 0
    assert doc["open_fly_holds"] == [
        {"checkpoint_id": "c2", "task_id": "t2", "age_hours": 9.0},
        {"checkpoint_id": "c1", "task_id": "t1", "age_hours": 3.0},
    ]
    assert "Routine edit" not in out_path().read_text()
    assert not reads(fake)  # one observation each: no request


def test_each_lane_condition_failing_alone_leaves_the_lane():
    cases = [
        (lane_hold("c1", "t1", 3, summary=SUMMARY), lane_task("t1")),
        (lane_hold("c2", "t2", 3), lane_task("t2", assigned_to="vanellus")),
        (lane_hold("c3", "t3", 3), lane_task("t3", owner="builder")),
    ]
    briefs = [b for b, _ in cases]
    tasks = [t for _, t in cases]
    _, _, doc = raised_run(briefs, tasks)
    assert doc["fly_lane_holds_raised_24h"] == 0
    assert doc["outside_fly_holds_raised_24h"] == 3
    assert doc["outside_fly_hold_tasks"] == ["t1", "t2", "t3"]
    assert doc["open_fly_holds"] == []


def test_a_closed_hold_with_no_task_is_outside_the_lane():
    closed = lane_hold("c1", "t_missing", 3, status="approved")
    _, _, doc = raised_run([closed], [])
    assert doc["outside_fly_holds_raised_24h"] == 1
    assert doc["fly_lane_holds_raised_24h"] == 0


def test_an_open_hold_whose_task_cannot_be_read_is_kept_as_real():
    _, _, doc = raised_run([lane_hold("c1", "t_missing", 3)], [])
    assert doc["by_class"] == {"unrecognised": 1}
    assert doc["real_holds_raised_24h"] == 1


def test_effect_or_a_request_or_an_operator_label_beats_the_lane():
    effect = DELIVERY.replace("open_or_merge_pr", "payment")
    briefs = [
        lane_hold("c1", "t1", 3, summary=effect),
        lane_hold("c2", "t2", 3),
        lane_hold("c3", "t3", 3),
        lane_hold("c4", "t4", 3, status="approved"),
    ]
    tasks = [
        lane_task("t1"),
        lane_task("t2", title="Decide which route to take"),
        lane_task("t3", blocked_reason=OP_ONLY),
        lane_task("t4", title="Approve the plan"),
    ]
    _, _, doc = raised_run(briefs, tasks)
    assert doc["fly_lane_holds_raised_24h"] == 0
    assert doc["real_holds_raised_24h"] == 4
    assert doc["outside_fly_holds_raised_24h"] == 0
    assert doc["by_class"] == {
        "live_effect": 1,
        "live_asks_for_decision": 1,
        "operator_only_label": 1,
    }


def test_an_approved_hold_with_three_observations_raised_in_the_window_counts():
    briefs = [
        lane_hold("c1", "t1", 1, status="approved", count=3),
        lane_hold("c2", "t2", 2),  # single observation: no request
    ]
    tasks = [lane_task("t1"), lane_task("t2")]
    fake, _, doc = raised_run(briefs, tasks, {"c1": (obs(1, 5, 7), 3)})
    assert doc["fly_lane_holds_raised_24h"] == 2
    assert [c[1] for c in reads(fake)] == ["/entities/c1/observations?limit=8"]
    assert doc["open_fly_holds"] == [
        {"checkpoint_id": "c2", "task_id": "t2", "age_hours": 2.0}
    ]


def test_an_old_hold_retired_in_the_window_is_not_new():
    old_then_retired = lane_hold("c1", "t1", 1, status="superseded", count=3)
    old_outside = checkpoint("c2", "t2", 1, status="approved", count=2)
    fake, _, doc = raised_run(
        [old_then_retired, old_outside],
        [lane_task("t1"), task("t2")],
        {"c1": (obs(1, 30, 60), 3), "c2": (obs(1, 90), 2)},
    )
    assert len(reads(fake)) == 2
    assert doc["fly_lane_holds_raised_24h"] == 0
    assert doc["outside_fly_holds_raised_24h"] == 0
    assert doc["holds_raised_time_unknown_24h"] == 0
    assert doc["real_holds_raised_24h"] == 0


def test_a_hold_untouched_in_the_window_is_never_read():
    fake, _, doc = raised_run(
        [lane_hold("c1", "t1", 30, status="approved", count=4)], [lane_task("t1")]
    )
    assert not reads(fake)
    assert doc["fly_lane_holds_raised_24h"] == 0


def test_a_hold_a_person_wrote_is_not_an_automatic_hold():
    briefs = [checkpoint("c1", "t1", 2, reason="please decide", handler="human")]
    fake, _, doc = raised_run(briefs, [task("t1")])
    assert not reads(fake)
    assert doc["real_holds_raised_24h"] == 0
    assert doc["outside_fly_holds_raised_24h"] == 0
    assert doc["real_decisions"] == 1  # still a real decision in the open list


def test_an_open_real_class_hold_counts_as_real():
    briefs = [checkpoint("c1", "t1", 2)]
    tasks = [task("t1", blocked_reason="a person said why this waits")]
    _, _, doc = raised_run(briefs, tasks)
    assert doc["by_class"] == {"session_wrote_hold": 1}
    assert doc["real_holds_raised_24h"] == 1
    assert doc["outside_fly_holds_raised_24h"] == 0


@pytest.mark.parametrize(
    "got,category",
    [
        (OSError("boom"), "OSError"),
        ((obs(1, 5), 3), "short_read"),
        (({"x": 1}, 1), "no_observed_at"),
    ],
)
def test_a_failed_or_short_read_is_unknown_and_not_clean(got, category):
    if isinstance(got, tuple) and isinstance(got[0], dict):
        got = ([None], 1)
    briefs = [
        lane_hold("c1", "t1", 1, status="approved", count=3),
        lane_hold("c2", "t2", 1, status="approved", count=3),
    ]
    fake, code, doc = raised_run(
        briefs,
        [lane_task("t1"), lane_task("t2")],
        {"c1": got, "c2": got},
    )
    assert code == 0 and doc["status"] == "ok"
    assert doc["holds_raised_time_unknown_24h"] == 2
    assert doc["fly_lane_holds_raised_24h"] == 0
    checks = [c for c in doc["failed_checks"] if c["check"] == "hold_raised_time"]
    assert checks == [{"check": "hold_raised_time", "reason": category}]
    assert doc["finish_test"]["clean_today"] is False
    assert doc["finish_test"]["consecutive_clean_readings"] == 0
    assert "boom" not in out_path().read_text()


def test_the_read_cap_leaves_the_rest_unknown(monkeypatch):
    monkeypatch.setattr(dqr, "MAX_OBSERVATION_READS", 2)
    briefs = [
        lane_hold(f"c{i}", f"t{i}", 1, status="approved", count=2) for i in range(4)
    ]
    tasks = [lane_task(f"t{i}") for i in range(4)]
    fake, _, doc = raised_run(
        briefs, tasks, {f"c{i}": (obs(1, 2), 2) for i in range(4)}
    )
    assert len(reads(fake)) == 2
    assert doc["fly_lane_holds_raised_24h"] == 2
    assert doc["holds_raised_time_unknown_24h"] == 2
    assert [
        c["reason"] for c in doc["failed_checks"] if c["check"] == "hold_raised_time"
    ] == ["read_cap"]


def test_cap_constant_is_one_thousand():
    assert dqr.MAX_OBSERVATION_READS == 1000


def test_reads_are_sequential_with_the_pause(monkeypatch):
    sleeps = []
    monkeypatch.setattr(dqr.time, "sleep", lambda s: sleeps.append(s))
    briefs = [
        lane_hold(f"c{i}", f"t{i}", 1, status="approved", count=2) for i in range(3)
    ]
    fake = FakeNeotoma(briefs, [lane_task(f"t{i}") for i in range(3)])
    fake.observations = {f"c{i}": (obs(1, 2), 2) for i in range(3)}
    dqr.run(fake, NOW, dry_run=True, pause=0.25)
    assert sleeps.count(0.25) >= 2


def test_outside_hold_lists_task_ids_only():
    _, _, doc = raised_run(
        [checkpoint("c1", "t_b", 2), checkpoint("c2", "t_a", 3)],
        [task("t_a"), task("t_b")],
    )
    assert doc["outside_fly_hold_tasks"] == ["t_a", "t_b"]
    assert doc["outside_fly_holds_raised_24h"] == 2


def test_q3_and_q4_are_stored_with_their_detail():
    briefs = [
        lane_hold("c1", "t1", 2),
        checkpoint("c2", "t2", 3),
        lane_hold("c3", "t3", 1, status="approved", count=2),
    ]
    tasks = [lane_task("t1"), task("t2"), lane_task("t3")]
    fake, _, doc = raised_run(briefs, tasks, {"c3": ([], 2)})
    readings = fake.stored["metric_reading"]
    q3 = readings[f"{dqr.METRIC_OUTSIDE_FLY}|ateles-checkpoint-queue|2026-11-05"]
    q4 = readings[f"{dqr.METRIC_FLY_LANE}|ateles-checkpoint-queue|2026-11-05"]
    assert q3["value"] == 1 and q3["detail"] == {"real": 0, "unknown": 1}
    assert q4["value"] == 1
    assert q3["metric_ref"].startswith("Q3_") and q4["metric_ref"].startswith("Q4_")
    assert doc["real_decisions"] == 0


def test_the_job_still_writes_nothing_but_metrics_with_the_new_reads():
    briefs = [lane_hold("c1", "t1", 1, status="approved", count=3)]
    fake, _, _ = raised_run(briefs, [lane_task("t1")], {"c1": (obs(1, 2, 3), 3)})
    assert reads(fake)
    writes = [c for c in fake.calls if c[0] != "GET" and c[1] != "/entities/query"]
    assert writes and all(c[0] == "POST" and c[1] == "/store" for c in writes)
    guarded = dqr.guarded_request(lambda *a, **k: {})
    with pytest.raises(PermissionError):
        guarded("PUT", "/entities/c1/observations", {})
    with pytest.raises(PermissionError):
        guarded("POST", "/entities/c1/observations", {})


def test_the_new_reads_go_through_the_guarded_requester():
    seen = []

    def inner(method, path, body=None, **kw):
        seen.append((method, path))
        return {"observations": [{"observed_at": iso(1)}], "total": 1}

    guarded = dqr.guarded_request(inner)
    brief = lane_hold("c1", "t1", 1, status="approved", count=2)
    dqr.measure_raised_holds([brief], {}, [], now=NOW, requester=guarded, pause=0)
    assert seen == [("GET", "/entities/c1/observations?limit=7")]


# ── finish test ──────────────────────────────────────────────────────────────

DAY = datetime(2026, 11, 5, 7, 30, tzinfo=timezone.utc)
CLEAN = {
    "real_decisions_over_limit": 0,
    "outside_fly_holds_raised_24h": 0,
    "holds_raised_time_unknown_24h": 0,
}


def ft(count, through, clean=True):
    return {
        "needed": 3,
        "consecutive_clean_readings": count,
        "clean_today": clean,
        "through": through,
        "met": count >= 3,
    }


@pytest.mark.parametrize(
    "previous,numbers,expected",
    [
        (ft(1, "2026-11-04"), CLEAN, (2, True, "2026-11-05", False)),
        (ft(2, "2026-11-04"), CLEAN, (3, True, "2026-11-05", True)),
        (ft(2, "2026-11-05"), CLEAN, (2, True, "2026-11-05", False)),  # rerun
        (ft(0, "2026-11-05", False), CLEAN, (1, True, "2026-11-05", False)),
        (ft(2, "2026-11-03"), CLEAN, (1, True, "2026-11-05", False)),  # gap
        (None, CLEAN, (1, True, "2026-11-05", False)),
        ({"junk": 1}, CLEAN, (1, True, "2026-11-05", False)),
        (
            ft(2, "2026-11-04"),
            {**CLEAN, "real_decisions_over_limit": 1},
            (0, False, "2026-11-05", False),
        ),
        (
            ft(2, "2026-11-04"),
            {**CLEAN, "outside_fly_holds_raised_24h": 1},
            (0, False, "2026-11-05", False),
        ),
        (
            ft(2, "2026-11-04"),
            {**CLEAN, "holds_raised_time_unknown_24h": 1},
            (0, False, "2026-11-05", False),
        ),
    ],
)
def test_finish_test_branches(previous, numbers, expected):
    got = dqr.next_finish_test(previous, numbers=numbers, now=DAY)
    count, clean, through, met = expected
    assert got == {
        "needed": 3,
        "consecutive_clean_readings": count,
        "clean_today": clean,
        "through": through,
        "met": met,
    }


def test_finish_test_invalid_run_passes_the_previous_value_through():
    got = dqr.next_finish_test(ft(2, "2026-11-04"), numbers=None, now=DAY)
    assert got == {**ft(2, "2026-11-04"), "clean_today": None}
    none = dqr.next_finish_test(None, numbers=None, now=DAY)
    assert none["consecutive_clean_readings"] == 0 and none["through"] is None


def test_finish_test_survives_an_invalid_day_and_then_resets():
    briefs = [lane_hold("c1", "t1", 2)]
    tasks = [lane_task("t1")]
    day1 = NOW
    raised_run(briefs, tasks, now=day1 - timedelta(days=1))  # a previous file
    _, _, doc1 = raised_run(briefs, tasks, now=day1)
    assert doc1["finish_test"] == ft(1, "2026-11-05")
    day2 = day1 + timedelta(days=1)
    # invalid run on day 2 (server total far from the client count)
    bad = FakeNeotoma(briefs, tasks, server_total=999)
    code, doc_bad = dqr.run(bad, day2, dry_run=False, pause=0)
    assert doc_bad["status"] == "invalid"
    on_disk = json.loads(out_path().read_text())["finish_test"]
    assert on_disk == {**ft(1, "2026-11-05"), "clean_today": None}
    # good run on day 3: the gap resets to 1, it does not reach 2
    day3 = day1 + timedelta(days=2)
    _, _, doc3 = raised_run(briefs, tasks, now=day3)
    assert doc3["finish_test"]["consecutive_clean_readings"] == 1


def test_finish_test_counts_three_days_and_reports_met():
    briefs = [lane_hold("c1", "t1", 2)]
    tasks = [lane_task("t1")]
    raised_run(briefs, tasks, now=NOW - timedelta(days=1))  # a previous file
    for i in range(3):
        _, _, doc = raised_run(briefs, tasks, now=NOW + timedelta(days=i))
    assert doc["finish_test"] == ft(3, "2026-11-07")
    assert doc["finish_test"]["met"] is True


def test_an_unclean_day_zeroes_the_count_in_a_run():
    briefs = [checkpoint("c1", "t1", 2)]
    _, _, doc = raised_run(briefs, [task("t1")])
    assert doc["outside_fly_holds_raised_24h"] == 1
    assert doc["finish_test"] == ft(0, "2026-11-05", False)


def test_status_line_carries_the_new_numbers(capsys):
    seed = FakeNeotoma(
        [lane_hold("c1", "t1", 2)], [lane_task("t1")], profile={"first_name": "Zed"}
    )
    dqr.run(seed, NOW - timedelta(days=1), dry_run=False, pause=0)  # a previous file
    fake = FakeNeotoma(
        [lane_hold("c1", "t1", 2)], [lane_task("t1")], profile={"first_name": "Zed"}
    )
    assert dqr.main(["--once"], requester=fake, now=NOW) == 0
    line = capsys.readouterr().out
    assert "fly_lane_holds_raised_24h=1" in line
    assert "outside_fly_holds_raised_24h=0" in line
    assert "holds_raised_time_unknown_24h=0" in line
    assert "finish_test_clean_readings=1" in line
    assert "real_decisions_over_72h=0" in line and "automatic_holds_added_24h=" in line


def _clean_day(**fake_kw):
    briefs = [lane_hold("c1", "t1", 2)]
    fake = FakeNeotoma(briefs, [lane_task("t1")], **fake_kw)
    return dqr.run(fake, NOW, dry_run=False, pause=0)[1]


def test_a_lost_previous_reading_is_not_a_clean_day():
    out_path().parent.mkdir(parents=True)
    out_path().write_text("{not json")
    doc = _clean_day(profile={"first_name": "Zed"})
    assert doc["status"] == "ok"
    assert any(c["check"] == "previous_reading" for c in doc["failed_checks"])
    assert doc["finish_test"]["clean_today"] is False
    assert doc["finish_test"]["consecutive_clean_readings"] == 0


def test_an_unreadable_operator_profile_is_not_a_clean_day():
    _clean_day(profile={"first_name": "Zed"})  # a good previous file
    later = NOW + timedelta(days=1)
    fake = FakeNeotoma([lane_hold("c1", "t1", 2)], [lane_task("t1")], profile=None)
    doc = dqr.run(fake, later, dry_run=False, pause=0)[1]
    assert any(c["check"] == "operator_profile" for c in doc["failed_checks"])
    assert doc["finish_test"]["clean_today"] is False
    assert doc["finish_test"]["consecutive_clean_readings"] == 0


def test_a_failed_metric_store_is_not_a_clean_day():
    doc = _clean_day(profile={"first_name": "Zed"}, drop_stored=True)
    assert doc["status"] == "partial"
    assert doc["finish_test"]["clean_today"] is False
    assert doc["finish_test"]["consecutive_clean_readings"] == 0


def test_a_run_with_no_failed_check_still_counts():
    _clean_day(profile={"first_name": "Zed"})
    later = NOW + timedelta(days=1)
    fake = FakeNeotoma(
        [lane_hold("c1", "t1", 2)], [lane_task("t1")], profile={"first_name": "Zed"}
    )
    doc = dqr.run(fake, later, dry_run=False, pause=0)[1]
    assert doc["failed_checks"] == []
    assert doc["finish_test"]["consecutive_clean_readings"] == 1
    assert doc["finish_test"]["clean_today"] is True
