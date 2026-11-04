"""Fixture tests for the hourly Codex delivery check.

Every database, TOML file and entity here is synthetic. Each check has a
known-positive case that goes red if the check is removed or inverted.
"""

from __future__ import annotations

import json
import plistlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_delivery_check as cdc  # noqa: E402

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
HOUR_MS = 3600 * 1000
DAY_MS = 24 * HOUR_MS


def make_dbs(
    tmp_path: Path, threads: list[tuple], goals: list[tuple]
) -> tuple[Path, Path]:
    state = tmp_path / "state_5.sqlite"
    goals_db = tmp_path / "goals_1.sqlite"
    c = sqlite3.connect(state)
    c.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, "
        "archived INTEGER, updated_at_ms INTEGER)"
    )
    c.executemany("INSERT INTO threads VALUES (?,?,?,?,?)", threads)
    c.commit()
    c.close()
    g = sqlite3.connect(goals_db)
    g.execute(
        "CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, objective TEXT, "
        "status TEXT, updated_at_ms INTEGER)"
    )
    g.executemany("INSERT INTO thread_goals VALUES (?,?,?,?)", goals)
    g.commit()
    g.close()
    return goals_db, state


# ── goals ────────────────────────────────────────────────────────────────────


def test_blocked_goal_alerts_with_name_and_duration_not_objective(tmp_path):
    goals_db, state = make_dbs(
        tmp_path,
        [("t1", "Alpha lane", "", 0, NOW_MS)],
        [("t1", "SECRET OBJECTIVE TEXT", "blocked", NOW_MS - 50 * HOUR_MS)],
    )
    findings = cdc.check_goals(goals_db, state, NOW_MS)
    assert len(findings) == 1
    msg = findings[0].message
    assert "Alpha lane" in msg and "2d 2h" in msg and "resume" in msg
    assert "SECRET" not in msg


@pytest.mark.parametrize("status", ["usage_limited", "budget_limited"])
def test_limited_goals_alert(tmp_path, status):
    goals_db, state = make_dbs(
        tmp_path, [("t1", "n", "", 0, 0)], [("t1", "o", status, NOW_MS - HOUR_MS)]
    )
    assert len(cdc.check_goals(goals_db, state, NOW_MS)) == 1


def test_paused_active_and_complete_goals_do_not_alert(tmp_path):
    goals_db, state = make_dbs(
        tmp_path,
        [("a", "A", "", 0, 0), ("b", "B", "", 0, 0), ("c", "C", "", 0, 0)],
        [("a", "o", "paused", 0), ("b", "o", "active", 0), ("c", "o", "complete", 0)],
    )
    assert cdc.check_goals(goals_db, state, NOW_MS) == []


def test_archived_thread_is_ignored(tmp_path):
    goals_db, state = make_dbs(
        tmp_path, [("t1", "Old", "", 1, 0)], [("t1", "o", "blocked", 0)]
    )
    assert cdc.check_goals(goals_db, state, NOW_MS) == []


def test_goal_databases_opened_read_only(tmp_path):
    goals_db, _ = make_dbs(tmp_path, [], [])
    conn = cdc._ro_connect(goals_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO thread_goals VALUES ('x','o','blocked',0)")
    conn.close()


# ── automations ──────────────────────────────────────────────────────────────


def write_automation(root: Path, auto_id: str, **fields) -> None:
    d = root / auto_id
    d.mkdir(parents=True)
    base = {
        "id": auto_id,
        "name": f"Watch {auto_id}",
        "rrule": "FREQ=HOURLY;INTERVAL=1",
        "target_thread_id": "t1",
        "created_at": NOW_MS - DAY_MS,
        "updated_at": NOW_MS - HOUR_MS,
        "status": "ACTIVE",
    }
    base.update(fields)
    lines = [
        f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}" for k, v in base.items()
    ]
    (d / "automation.toml").write_text("\n".join(lines) + "\n")


def test_recent_paused_automation_alerts_old_paused_does_not(tmp_path):
    _, state = make_dbs(tmp_path, [("t1", "T", "", 0, NOW_MS)], [])
    root = tmp_path / "automations"
    write_automation(root, "fresh", status="PAUSED")
    write_automation(root, "old", status="PAUSED", created_at=NOW_MS - 30 * DAY_MS)
    findings, _ = cdc.check_automations(root, state, NOW_MS)
    assert [f.key for f in findings] == [f"{cdc.KEY_PREFIX}:automation-paused:fresh"]


def test_active_automation_with_quiet_thread_alerts(tmp_path):
    _, state = make_dbs(tmp_path, [("t1", "Quiet", "", 0, NOW_MS - 10 * HOUR_MS)], [])
    root = tmp_path / "automations"
    write_automation(root, "w")
    findings, _ = cdc.check_automations(root, state, NOW_MS)
    assert len(findings) == 1 and "10h" in findings[0].message


def test_active_automation_with_recent_thread_is_quiet(tmp_path):
    _, state = make_dbs(tmp_path, [("t1", "Busy", "", 0, NOW_MS - HOUR_MS)], [])
    root = tmp_path / "automations"
    write_automation(root, "w")
    assert cdc.check_automations(root, state, NOW_MS)[0] == []


def test_active_automation_with_missing_thread_alerts(tmp_path):
    _, state = make_dbs(tmp_path, [], [])
    root = tmp_path / "automations"
    write_automation(root, "w")
    assert len(cdc.check_automations(root, state, NOW_MS)[0]) == 1


# ── deploys ──────────────────────────────────────────────────────────────────


def cfg(**kw):
    base = {
        "entity_id": "ent_cfg1",
        "snapshot": {
            "project": "Proj",
            "environment": "prod",
            "fly_app": "app-x",
            "repo": "org/repo",
        },
    }
    base["snapshot"].update(kw)
    return base


def fake_run(deployed: datetime, commit_dates: list[datetime]):
    def run(argv):
        if argv[0] == "fly":
            return json.dumps(
                [
                    {
                        "Version": 3,
                        "Status": "complete",
                        "CreatedAt": deployed.isoformat(),
                    }
                ]
            )
        if argv[:2] == ["gh", "api"] and "--jq" in argv:
            return "main\n"
        return json.dumps(
            [{"commit": {"committer": {"date": d.isoformat()}}} for d in commit_dates]
        )

    return run


def test_merge_undeployed_over_24h_alerts():
    run = fake_run(
        NOW - timedelta(days=3), [NOW - timedelta(hours=30), NOW - timedelta(hours=2)]
    )
    findings, _ = cdc.check_deploys([cfg()], run, NOW)
    assert len(findings) == 1 and "2 change(s)" in findings[0].message


def test_recent_merge_does_not_alert():
    run = fake_run(NOW - timedelta(days=3), [NOW - timedelta(hours=2)])
    assert cdc.check_deploys([cfg()], run, NOW)[0] == []


def test_nothing_after_deploy_does_not_alert():
    run = fake_run(NOW - timedelta(hours=1), [NOW - timedelta(days=2)])
    assert cdc.check_deploys([cfg()], run, NOW)[0] == []


def test_uncheckable_target_noted_but_others_checked():
    run = fake_run(NOW - timedelta(days=3), [NOW - timedelta(hours=30)])
    findings, notes = cdc.check_deploys(
        [cfg(fly_app=None), cfg(entity_id="x")], run, NOW
    )
    assert any("not checkable" in n for n in notes)
    assert len(findings) == 1


def test_no_checkable_target_is_a_failure_not_all_clear():
    with pytest.raises(RuntimeError):
        cdc.check_deploys([cfg(repo=None, fly_app=None)], lambda a: "", NOW)


def test_repo_inferred_from_deploy_command():
    snap = {"deploy_command": "git clone https://github.com/org/repo.git && fly deploy"}
    assert cdc._repo_for(snap) == "org/repo"


# ── tasks ────────────────────────────────────────────────────────────────────


def task(i, due, status="pending", **kw):
    return {
        "entity_id": f"ent_{i}",
        "snapshot": {"title": f"Task {i}", "due_date": due, "status": status, **kw},
    }


def pager(rows):
    return lambda offset: (rows, False)


def test_task_checks_flag_overdue_and_blocked_soon_only():
    rows = [
        task(1, "2026-11-01"),  # overdue -> alert
        task(2, "2026-11-01", status="done"),  # done -> quiet
        task(3, "2026-11-08", status="blocked"),  # due soon, blocked -> alert
        task(4, "2026-11-08", blocked_reason="waiting"),  # blocked_reason -> alert
        task(5, "2026-11-08"),  # due soon, fine -> quiet
        task(6, "2026-12-30", status="blocked"),  # far away -> quiet
        task(7, "2026-11-04"),  # due today, not overdue
    ]
    findings, _ = cdc.check_tasks(pager(rows), NOW)
    assert sorted(f.key.rsplit(":", 1)[-1] for f in findings) == [
        "ent_1",
        "ent_3",
        "ent_4",
    ]


def test_task_reads_are_bounded():
    calls = []

    def fetch(offset):
        calls.append(offset)
        return [task(offset, "2026-12-30")] * 25, True

    _, notes = cdc.check_tasks(fetch, NOW)
    assert len(calls) == cdc.TASK_MAX_PAGES and notes


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://localhost:3000",
        "https://staging.example.com",
        "https://dev.example.com",
        "https://127.0.0.1",
    ],
)
def test_non_prod_target_refused(url):
    with pytest.raises(RuntimeError):
        cdc.assert_prod_target(url)


def test_prod_target_accepted_and_missing_env_refused():
    assert (
        cdc.assert_prod_target("https://neotoma.example.org/x")
        == "https://neotoma.example.org"
    )
    with pytest.raises(RuntimeError):
        cdc.NeotomaReader({})


# ── failure handling, dedupe, rendering ──────────────────────────────────────


class FakeNotifier:
    def __init__(self):
        self.sent, self.cleared = [], []

    def send(self, message, priority, handler="", dedupe_key=None, **kw):
        self.sent.append((message, priority, dedupe_key))

    def clear_dedupe(self, key):
        self.cleared.append(key)


def result(name, keys=(), error=None):
    return cdc.CheckResult(name, [cdc.Finding(k, f"msg {k}") for k in keys], error)


def test_failed_read_reports_could_not_check(tmp_path):
    results = cdc.run_checks(
        now=NOW,
        home=tmp_path / "missing",
        reader_factory=lambda: (_ for _ in ()).throw(RuntimeError("no env")),
    )
    assert all(not r.ok for r in results)
    n = FakeNotifier()
    cdc.apply_results(results, {}, n, "warn")
    assert len(n.sent) == 4 and all(
        m.startswith("Could not check") for m, _, _ in n.sent
    )
    assert "COULD NOT CHECK" in cdc.render_report(results)


def test_alert_only_on_change_and_clears_resolved_key():
    n = FakeNotifier()
    state = cdc.apply_results([result("c", ["k1"])], {}, n, "warn")
    assert [d for _, _, d in n.sent] == ["k1"]
    state = cdc.apply_results([result("c", ["k1"])], state, n, "warn")
    assert len(n.sent) == 1 and not n.cleared  # unchanged: silent
    state = cdc.apply_results([result("c", [])], state, n, "warn")
    assert n.cleared == ["k1"]  # resolved: key cleared
    cdc.apply_results([result("c", ["k1"])], state, n, "warn")
    assert len(n.sent) == 2  # recurrence alerts again


def test_failed_check_keeps_prior_keys_and_clears_failure_when_it_recovers():
    n = FakeNotifier()
    state = cdc.apply_results([result("c", ["k1"])], {}, n, "warn")
    state = cdc.apply_results([result("c", error="boom")], state, n, "warn")
    assert not n.cleared and "k1" in state["c"]  # unknown != resolved
    state = cdc.apply_results([result("c", error="boom")], state, n, "warn")
    assert len(n.sent) == 2  # failure alerts once
    cdc.apply_results([result("c", ["k1"])], state, n, "warn")
    assert n.cleared == [cdc.failure_key("c")]


def test_new_alerts_per_run_are_capped_with_one_summary_then_quiet():
    n = FakeNotifier()
    keys = [f"k{i:02d}" for i in range(14)]
    state = cdc.apply_results([result("c", keys)], {}, n, "warn", max_new=10)
    assert len(n.sent) == 11 and "4 more" in n.sent[-1][0]  # 10 findings + one summary
    cdc.apply_results([result("c", keys)], state, n, "warn", max_new=10)
    assert len(n.sent) == 11  # nothing new: silent


def test_one_unreadable_target_does_not_hide_the_others():
    good = cfg()
    bad = cfg(entity_id="bad", fly_app="app-bad")

    def run(argv):
        if "app-bad" in argv:
            raise RuntimeError("fly exited 1")
        return fake_run(NOW - timedelta(days=3), [NOW - timedelta(hours=30)])(argv)

    findings, notes = cdc.check_deploys([good, bad], run, NOW)
    assert len(findings) == 1 and any("could not check" in n for n in notes)


def test_every_target_unreadable_is_a_failure():
    def run(argv):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        cdc.check_deploys([cfg()], run, NOW)


# ── end to end, read-only, launchd template ──────────────────────────────────


def test_run_checks_end_to_end_on_fixtures(tmp_path):
    goals_db, state = make_dbs(
        tmp_path,
        [("t1", "Lane", "", 0, NOW_MS)],
        [("t1", "o", "blocked", NOW_MS - DAY_MS)],
    )
    (tmp_path / "automations").mkdir()

    class Reader:
        def deployment_configs(self):
            return []

        def task_page_fetcher(self, now):
            return pager([task(1, "2026-11-01")])

    results = cdc.run_checks(now=NOW, home=tmp_path, reader_factory=Reader)
    assert [len(r.findings) for r in results] == [1, 0, 0, 1]
    assert all(r.ok for r in results)


def test_plist_template_is_hourly_and_identity_free():
    path = Path(__file__).parent / "com.ateles.codex-delivery-check.plist.tmpl"
    text = path.read_text()
    assert "/Users/" not in text and "<HOME>" in text
    data = plistlib.loads(text.replace("<HOME>", "/h").encode())
    assert data["StartCalendarInterval"] == {"Minute": 7}
    assert (
        "--once" in data["ProgramArguments"]
        and "--dry-run" not in data["ProgramArguments"]
    )
    assert "ateles-rc-src" in data["ProgramArguments"][1]
