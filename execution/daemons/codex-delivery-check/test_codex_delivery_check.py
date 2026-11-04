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
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import codex_delivery_check as cdc  # noqa: E402
from lib.github_app_token import without_app_keys as _real_without_app_keys  # noqa: E402

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
    def __init__(self, deliver=True, silence=False):
        self.sent, self.cleared = [], []
        self.deliver, self.silence = deliver, silence

    def send(self, message, priority, handler="", dedupe_key=None, **kw):
        self.sent.append((message, priority, dedupe_key))
        return self.deliver

    def in_silence_window(self):
        return self.silence

    def clear_dedupe(self, key):
        self.cleared.append(key)

    def flush_digest(self):
        return False


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

    findings, _ = cdc.check_deploys([good, bad], run, NOW)
    # Both surface: the undeployed target, and the unreadable one as its own
    # alert, so a failed read can never pass for "nothing waiting".
    assert len(findings) == 2
    unreadable = [f for f in findings if "Could not check" in f.message]
    assert len(unreadable) == 1 and "not an all-clear" in unreadable[0].message


def test_gh_reads_use_an_app_token_for_that_repo(monkeypatch):
    seen = {}

    def fake_token_env(repo):
        seen["repo"] = repo
        return {"GH_TOKEN": "t", "GITHUB_TOKEN": "t"}

    def fake_subprocess_run(argv, **kw):
        seen["env"] = kw["env"]
        return type("P", (), {"returncode": 0, "stdout": "ok"})()

    monkeypatch.setitem(
        sys.modules,
        "lib.github_app_token",
        type(
            "M",
            (),
            {
                "token_env": staticmethod(fake_token_env),
                "without_app_keys": staticmethod(_real_without_app_keys),
            },
        ),
    )
    monkeypatch.setenv("ATELES_REVIEWER_APP_PRIVATE_KEY", "placeholder")
    monkeypatch.setenv("ATELES_APP_PR_OPENER_PRIVATE_KEY_PATH", "/placeholder")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(cdc.subprocess, "run", fake_subprocess_run)
    assert cdc.run_command(["gh", "api", "repos/o/r/commits?sha=main"]) == "ok"
    assert seen["repo"] == "o/r" and seen["env"]["GH_TOKEN"] == "t"
    seen.clear()
    assert cdc.run_command(["gh", "api", "repos/o/r", "--jq", ".x"]) == "ok"
    assert seen["repo"] == "o/r" and seen["env"]["GH_TOKEN"] == "t"
    fly_env = cdc._child_env(["fly", "releases"])
    assert "GH_TOKEN" not in fly_env
    for env in (seen["env"], fly_env):
        assert "ATELES_REVIEWER_APP_PRIVATE_KEY" not in env
        assert "ATELES_APP_PR_OPENER_PRIVATE_KEY_PATH" not in env


# ── an alert that did not reach the operator ─────────────────────────────────


def test_undelivered_alert_is_retried_next_run():
    n = FakeNotifier(deliver=False)
    state = cdc.apply_results([result("c", ["k1"])], {}, n, "warn")
    assert state["c"] == [] and n.cleared == ["k1"]
    n2 = FakeNotifier()
    state = cdc.apply_results([result("c", ["k1"])], state, n2, "warn")
    assert [s[2] for s in n2.sent] == ["k1"] and state["c"] == ["k1"]


def test_alert_held_through_silence_is_not_retried():
    n = FakeNotifier(deliver=False, silence=True)
    state = cdc.apply_results([result("c", ["k1"])], {}, n, "warn")
    assert state["c"] == ["k1"] and n.cleared == []
    n2 = FakeNotifier()
    cdc.apply_results([result("c", ["k1"])], state, n2, "warn")
    assert n2.sent == []


def test_undelivered_could_not_check_alert_is_retried():
    n = FakeNotifier(deliver=False)
    state = cdc.apply_results([result("c", error="boom")], {}, n, "warn")
    assert cdc.failure_key("c") not in state["c"]
    n2 = FakeNotifier()
    state = cdc.apply_results([result("c", error="boom")], state, n2, "warn")
    assert len(n2.sent) == 1 and cdc.failure_key("c") in state["c"]


def test_undelivered_first_run_summary_leaves_the_first_run_open():
    n = FakeNotifier(deliver=False)
    state = cdc.apply_results([result("c", ["k1"])], {}, n, "warn", baseline=True)
    assert state == {}


def test_undelivered_overflow_summary_is_retried():
    keys = [f"k{i:02d}" for i in range(4)]
    delivered = iter([True, True, False])
    n = FakeNotifier()
    n.send = lambda *a, **kw: next(delivered)
    state = cdc.apply_results([result("c", keys)], {}, n, "warn", max_new=2)
    assert state["c"] == keys[:2]
    n2 = FakeNotifier()
    state = cdc.apply_results([result("c", keys)], state, n2, "warn", max_new=2)
    assert state["c"] == keys and [s[2] for s in n2.sent] == keys[2:]


def test_run_after_undelivered_first_summary_is_a_first_run_again(
    monkeypatch, tmp_path
):
    from lib.notify.notifier import Notifier

    notifiers = iter([FakeNotifier(deliver=False), FakeNotifier()])
    used = []

    def from_neotoma():
        used.append(next(notifiers))
        return used[-1]

    monkeypatch.setitem(sys.modules, "lib.daemon_runtime", type("M", (), {}))
    monkeypatch.setattr(Notifier, "from_neotoma", staticmethod(from_neotoma))
    monkeypatch.setattr(cdc, "run_checks", lambda **kw: [result("c", ["k1", "k2"])])
    monkeypatch.setenv("CODEX_DELIVERY_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    assert cdc.main(["--once"]) == 0
    assert (tmp_path / "state.json").exists()
    assert cdc.main(["--once"]) == 0
    assert len(used[1].sent) == 1 and "Already open" in used[1].sent[0][0]


def _real_notifier(monkeypatch, tmp_path, silence):
    from lib.notify.notifier import Notifier

    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "held.json"))
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(Notifier, "_in_silence_window", lambda self: silence)
    return Notifier(rubric={"timezone": "UTC", "digest_times": ""})


def test_real_notifier_failed_delivery_is_retried(monkeypatch, tmp_path):
    n = _real_notifier(monkeypatch, tmp_path, silence=False)
    state = cdc.apply_results([result("c", ["k1"])], {}, n, "warn")
    assert state["c"] == []  # no transport configured: not delivered
    assert "k1" not in n._dedupe_journal  # so the retry is not suppressed


def test_real_notifier_held_notice_counts_as_told(monkeypatch, tmp_path):
    n = _real_notifier(monkeypatch, tmp_path, silence=True)
    state = cdc.apply_results([result("c", ["k1"])], {}, n, "warn")
    assert state["c"] == ["k1"] and "k1" in n._dedupe_journal
    assert len(n._digest_queue) == 1


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


# ── first-run baseline, overdue backlog, repo map ────────────────────────────


def test_first_run_sends_one_summary_then_stays_quiet():
    many = [f"k{i}" for i in range(40)]
    n = FakeNotifier()
    state = cdc.apply_results([result("goals", many)], {}, n, "warn", baseline=True)
    assert len(n.sent) == 1
    assert "goals: 40" in n.sent[0][0] and "and 28 more" in n.sent[0][0]
    n2 = FakeNotifier()
    cdc.apply_results([result("goals", many)], state, n2, "warn")
    assert n2.sent == []


def test_finding_new_after_baseline_still_alerts():
    n = FakeNotifier()
    state = cdc.apply_results([result("goals", ["a"])], {}, n, "warn", baseline=True)
    n2 = FakeNotifier()
    cdc.apply_results([result("goals", ["a", "b"])], state, n2, "warn")
    assert [s[2] for s in n2.sent] == ["b"]


def test_failed_check_on_first_run_is_not_folded_into_baseline():
    n = FakeNotifier()
    cdc.apply_results([result("deploys", error="boom")], {}, n, "warn", baseline=True)
    assert len(n.sent) == 1
    assert "Could not check deploys" in n.sent[0][0]


def test_old_overdue_task_is_counted_not_alerted():
    now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    recent = (now - timedelta(days=3)).date().isoformat()
    old = (now - timedelta(days=cdc.TASK_OVERDUE_ALERT_DAYS + 1)).date().isoformat()
    findings, notes = cdc.check_tasks(pager([task(1, recent), task(2, old)]), now)
    assert len(findings) == 1 and "3 day(s)" in findings[0].message
    assert any("1 more than" in n for n in notes)


def test_repo_map_makes_a_target_checkable():
    now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    conf = {"entity_id": "e1", "snapshot": {"project": "p", "fly_app": "app-x"}}
    run = fake_run(now - timedelta(days=5), [now - timedelta(days=3)])
    findings, _ = cdc.check_deploys([conf], run, now, {"app-x": "owner/repo"})
    assert len(findings) == 1 and "not been deployed" in findings[0].message
    with pytest.raises(RuntimeError):
        cdc.check_deploys([conf], run, now, {})


def test_repo_map_unreadable_or_malformed_fails_loudly(tmp_path):
    with pytest.raises(OSError):
        cdc.load_repo_map({cdc.REPO_MAP_ENV: str(tmp_path / "missing.json")})
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"app": "not a repo"}))
    with pytest.raises(ValueError):
        cdc.load_repo_map({cdc.REPO_MAP_ENV: str(bad)})
    assert cdc.load_repo_map({}) == {}


# ── email delivery from the swarm mailbox ────────────────────────────────────

TEST_TO = "ops@example.test"


class _Gws:
    """Stands in for subprocess.run inside the notifier. Never sends mail."""

    def __init__(self, rc=0):
        self.cmds, self.rc = [], rc

    def __call__(self, cmd, **kw):
        self.cmds.append(list(cmd))
        return type("R", (), {"returncode": self.rc, "stderr": "", "stdout": ""})()


def _email_setup(monkeypatch, tmp_path, silence, rc=0):
    """Real Notifier, email on, dotenv-like values set, no real mail."""
    from lib.notify import notifier as nmod

    state = tmp_path / "state.json"
    for k, v in {
        "ATELES_NOTIFY_EMAIL": "1",
        "OPERATOR_EMAIL": TEST_TO,
        "ATELES_SWARM_EMAIL": "alias+swarm@example.test",
        "ATELES_NOTIFY_TO": "",
        "ATELES_DEDUPE_JOURNAL_PATH": str(tmp_path / "dedupe.json"),
        "ATELES_DIGEST_QUEUE_PATH": str(tmp_path / "global-shared.json"),
    }.items():
        monkeypatch.setenv(k, v)
    gws = _Gws(rc)
    monkeypatch.setattr(nmod.subprocess, "run", gws)
    monkeypatch.setattr(nmod.Notifier, "_in_silence_window", lambda self: silence)
    queue = cdc.isolate_notifier_env(state)
    return nmod.Notifier(rubric={"timezone": "UTC", "digest_times": ""}), gws, queue


def test_email_has_no_from_and_targets_operator(monkeypatch, tmp_path):
    from lib.notify.notifier import Priority

    n, gws, _ = _email_setup(monkeypatch, tmp_path, silence=False)
    assert cdc._alert(n, "stuck goal", Priority.WARN, "k2") is True
    cmd = gws.cmds[-1]
    assert cmd[:3] == ["gws", "gmail", "+send"]
    assert "--from" not in cmd
    assert cmd[cmd.index("--to") + 1] == TEST_TO


def test_job_queue_is_its_own_and_shared_queue_is_untouched(monkeypatch, tmp_path):
    from lib.notify.notifier import Priority

    shared = tmp_path / "shared.json"
    shared.write_text(json.dumps(["⚠ other daemon notice"]))
    n, gws, queue = _email_setup(monkeypatch, tmp_path, silence=False)
    assert queue == tmp_path / "codex-delivery-held.json"
    # a global value pointing at the shared file must not win
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(shared))
    cdc.isolate_notifier_env(tmp_path / "state.json")
    n2 = type(n)(rubric={"timezone": "UTC", "digest_times": ""})
    assert n2._digest_path == queue
    before = shared.read_text()
    n2.send("a finding", Priority.WARN, handler="h")
    cdc.release_held_notices(n2)
    assert shared.read_text() == before
    assert all("other daemon" not in " ".join(c) for c in gws.cmds)


def test_held_notice_goes_out_after_silence_and_waits_during_it(
    monkeypatch, tmp_path
):
    from lib.notify import notifier as nmod
    from lib.notify.notifier import Priority

    n, gws, queue = _email_setup(monkeypatch, tmp_path, silence=True)
    assert n.send("overnight finding", Priority.WARN, handler="h") is False
    assert len(n._digest_queue) == 1 and gws.cmds == []
    assert cdc.release_held_notices(n) is False  # still silent: stays queued
    assert len(n._digest_queue) == 1 and gws.cmds == []
    monkeypatch.setattr(nmod.Notifier, "_in_silence_window", lambda self: False)
    assert cdc.release_held_notices(n) is True
    assert len(gws.cmds) == 1 and "overnight finding" in gws.cmds[0][-1]
    assert n._digest_queue == []


def test_held_notice_stays_queued_when_delivery_fails(monkeypatch, tmp_path):
    from lib.notify import notifier as nmod
    from lib.notify.notifier import Priority

    n, gws, queue = _email_setup(monkeypatch, tmp_path, silence=True, rc=1)
    n.send("overnight finding", Priority.WARN, handler="h")
    monkeypatch.setattr(nmod.Notifier, "_in_silence_window", lambda self: False)
    monkeypatch.setattr(n, "_apprise", None)  # no Telegram fallback
    assert cdc.release_held_notices(n) is False
    assert len(gws.cmds) == 1 and len(n._digest_queue) == 1


def test_main_releases_held_notices_before_alerting(monkeypatch, tmp_path):
    import os

    from lib.notify.notifier import Notifier

    order = []
    fake = FakeNotifier()
    fake.flush_digest = lambda: order.append("flush") or False
    orig_send = fake.send
    fake.send = lambda *a, **k: order.append("send") or orig_send(*a, **k)
    monkeypatch.setitem(sys.modules, "lib.daemon_runtime", type("M", (), {}))
    monkeypatch.setattr(Notifier, "from_neotoma", staticmethod(lambda: fake))
    monkeypatch.setattr(cdc, "run_checks", lambda **kw: [result("c", ["k1"])])
    monkeypatch.setenv("CODEX_DELIVERY_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "dedupe.json"))
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", "/should/be/overridden")
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "alias+swarm@example.test")
    assert cdc.main(["--once"]) == 0
    assert order[0] == "flush" and "send" in order
    assert os.environ["ATELES_DIGEST_QUEUE_PATH"] == str(
        tmp_path / "codex-delivery-held.json"
    )
    assert os.environ["ATELES_SWARM_EMAIL"] == ""


def test_plist_template_sends_email_from_swarm_mailbox():
    path = Path(__file__).parent / "com.ateles.codex-delivery-check.plist.tmpl"
    text = path.read_text()
    assert "@" not in text
    env = plistlib.loads(text.replace("<HOME>", "/h").encode())["EnvironmentVariables"]
    assert env["ATELES_NOTIFY_EMAIL"] == "1"
    assert env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == "/h/.config/gws-swarm"
    assert "/h/.nvm/versions/node/v20.20.0/bin" in env["PATH"].split(":")
