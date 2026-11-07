"""The labelled-panel sweep repeats while Apis runs, and reads each head fresh.

GitHub gives a webhook 10 seconds and does not resend one that timed out, so a
lost `check_suite/completed` leaves a held panel with nothing to release it.
`SwarmDispatcher.repeat_labelled_panel_sweep` re-runs the startup sweep's
approach on an interval (`APIS_PANEL_SWEEP_INTERVAL_S`), bounded so it never
re-enters a running panel, a head still held on pending checks, a head it has
already re-entered in this process, or a PR under an unexpired usage-limit
deferral. Both sweeps re-read a PR's head just before judging it.

Re-entry is observed through `_handle_pr_entrance` (the body behind the real
`_handle_pr` wrapper), so the wrapper's running-panel bookkeeping is the real
one. What each test looked like with the change reverted is in the PR body.

Run: pytest execution/daemons/apis/test_panel_sweep_repeat.py -q
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

import panel_check_gate as pcg
import swarm_dispatch as sd

REPO = "owner/repo"
PR = 87
KEY = (REPO, PR)
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40
CANARY = "swarm-canary"


class _Notifier:
    def send(self, *a, **k):
        pass

    def clear_dedupe(self, key):
        pass


def _pr(head, number=PR):
    return {
        "number": number, "state": "open", "draft": False, "title": "t",
        "body": "Closes #80", "html_url": "u", "user": {"login": "someone"},
        "head": {"sha": head, "ref": "feat/x"}, "base": {"ref": "main"},
        "labels": [{"name": CANARY}],
    }


def _outcome(name, state, passed):
    return type(
        "O", (), {"name": name, "state": state, "passed": passed, "superseded": False}
    )()


PENDING = pcg.describe_gate(
    OLD_HEAD, [_outcome("pytest", "pending (in_progress)", False)], False
)
GREEN = pcg.describe_gate(OLD_HEAD, [_outcome("pytest", "success", True)], True)


@pytest.fixture
def sweep(monkeypatch):
    """A dispatcher with the label gate set and GitHub's answers scripted.

    `state["listed"]` is the PR list read at the start of a sweep;
    `state["current"]` is what a fresh read of the PR returns now.
    """
    state = {
        "listed": [_pr(OLD_HEAD)], "current": _pr(OLD_HEAD), "fetch_ok": True,
        "finished": False, "deferred": False, "gate": GREEN,
        "entered": [], "judged": [],
    }

    async def ready(self, repos):
        return None

    async def open_prs(self, repository, label):
        return state["listed"]

    async def fetch_pr(self, repository, number):
        return state["current"] if state["fetch_ok"] else None

    async def gate_allows(self, trigger, source=""):
        return True

    async def finished(self, trigger):
        state["judged"].append(trigger.head_sha)
        return state["finished"]

    async def deferral(self, repository, number):
        return state["deferred"]

    async def read_gate(self, repository, head_sha, base_ref):
        return state["gate"]

    async def entrance(self, trigger):
        state["entered"].append((trigger.number, trigger.head_sha))

    monkeypatch.setattr(sd.SwarmDispatcher, "_await_github_app_ready", ready)
    monkeypatch.setattr(sd.SwarmDispatcher, "_open_labelled_prs", open_prs)
    monkeypatch.setattr(sd.SwarmDispatcher, "_fetch_pr", fetch_pr)
    monkeypatch.setattr(sd.SwarmDispatcher, "_label_gate_allows", gate_allows)
    monkeypatch.setattr(sd.SwarmDispatcher, "_panel_finished_for_pr", finished)
    monkeypatch.setattr(sd.SwarmDispatcher, "_unexpired_deferral", deferral)
    monkeypatch.setattr(sd.SwarmDispatcher, "_read_head_check_gate", read_gate)
    monkeypatch.setattr(sd.SwarmDispatcher, "_handle_pr_entrance", entrance)
    d = sd.SwarmDispatcher(
        _Notifier(),
        sd.DispatchConfig(
            neotoma_token="", github_token="", auto_merge=False,
            auto_rereview_on_push=False, require_label=CANARY,
        ),
    )
    return d, state


def _run(coro):
    return asyncio.run(coro)


def test_a_stranded_held_head_whose_checks_finished_is_re_entered(sweep):
    # The lost-webhook case: a hold for this head is remembered, its checks
    # have since gone green, and no check_suite/completed arrived to release it.
    d, state = sweep
    d._panel_ci_holds[KEY] = sd._PanelCIHold(head=OLD_HEAD, reason="pending")
    summary = _run(d.repeat_labelled_panel_sweep([REPO]))
    assert state["entered"] == [(PR, OLD_HEAD)]
    assert summary["resumed"] == 1
    assert d._panels_running == {}, "the running mark is released after the panel"


def test_a_stranded_head_with_no_hold_is_re_entered(sweep):
    d, state = sweep
    _run(d.repeat_labelled_panel_sweep([REPO]))
    assert state["entered"] == [(PR, OLD_HEAD)]


def test_a_head_still_held_on_pending_checks_is_left_alone(sweep):
    d, state = sweep
    d._panel_ci_holds[KEY] = sd._PanelCIHold(head=OLD_HEAD, reason="pending")
    state["gate"] = PENDING
    summary = _run(d.repeat_labelled_panel_sweep([REPO]))
    assert state["entered"] == [] and summary["held"] == 1


def test_a_pr_with_a_panel_running_now_is_left_alone(sweep, monkeypatch):
    d, state = sweep
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_entrance(self, trigger):
        started.set()
        await release.wait()

    monkeypatch.setattr(sd.SwarmDispatcher, "_handle_pr_entrance", slow_entrance)

    async def scenario():
        # A webhook-driven panel is in progress through the real `_handle_pr`.
        running = asyncio.create_task(
            d._handle_pr(d._pr_trigger_from_api(REPO, _pr(OLD_HEAD)))
        )
        await started.wait()
        summary = await d.repeat_labelled_panel_sweep([REPO])
        release.set()
        await running
        return summary

    summary = _run(scenario())
    assert summary["running"] == 1 and summary["resumed"] == 0
    assert state["judged"] == [], "a running panel is not even re-judged"
    assert d._panels_running == {}


def test_the_same_head_is_not_re_entered_twice(sweep):
    d, state = sweep
    _run(d.repeat_labelled_panel_sweep([REPO]))
    second = _run(d.repeat_labelled_panel_sweep([REPO]))
    assert state["entered"] == [(PR, OLD_HEAD)]
    assert second["already_entered"] == 1


def test_a_head_the_startup_sweep_entered_is_not_re_entered(sweep):
    d, state = sweep
    _run(d.resume_labelled_panels([REPO]))
    _run(d.repeat_labelled_panel_sweep([REPO]))
    assert state["entered"] == [(PR, OLD_HEAD)]


def test_a_new_head_on_the_same_pr_is_re_entered(sweep):
    d, state = sweep
    _run(d.repeat_labelled_panel_sweep([REPO]))
    state["listed"] = [_pr(NEW_HEAD)]
    state["current"] = _pr(NEW_HEAD)
    _run(d.repeat_labelled_panel_sweep([REPO]))
    assert state["entered"] == [(PR, OLD_HEAD), (PR, NEW_HEAD)]


@pytest.mark.parametrize("which", ["startup", "repeating"])
def test_a_head_that_moved_after_the_list_was_read_uses_the_new_head(sweep, which):
    d, state = sweep
    state["listed"] = [_pr(OLD_HEAD)]
    state["current"] = _pr(NEW_HEAD)
    if which == "startup":
        _run(d.resume_labelled_panels([REPO]))
    else:
        _run(d.repeat_labelled_panel_sweep([REPO]))
    assert state["judged"] == [NEW_HEAD]
    assert state["entered"] == [(PR, NEW_HEAD)]


@pytest.mark.parametrize("which", ["startup", "repeating"])
def test_a_failed_head_re_read_leaves_the_pr_alone(sweep, which):
    d, state = sweep
    state["fetch_ok"] = False
    sweeper = (
        d.resume_labelled_panels if which == "startup" else d.repeat_labelled_panel_sweep
    )
    summary = _run(sweeper([REPO]))
    assert state["entered"] == [] and state["judged"] == []
    assert summary["unreadable"] == 1
    # Not remembered as entered: the next sweep may still recover it.
    state["fetch_ok"] = True
    _run(d.repeat_labelled_panel_sweep([REPO]))
    assert state["entered"] == [(PR, OLD_HEAD)]


def test_an_unexpired_deferral_is_left_alone(sweep):
    d, state = sweep
    state["deferred"] = True
    summary = _run(d.repeat_labelled_panel_sweep([REPO]))
    assert state["entered"] == [] and summary["deferred"] == 1


def _comments_client(monkeypatch, comments):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return comments

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **k: _Client())
    monkeypatch.setattr(sd.SwarmDispatcher, "_github_headers", lambda self, repo=None: {})

    async def authored(self, repository, number, cs):
        return cs

    monkeypatch.setattr(sd.SwarmDispatcher, "_swarm_authored", authored)


@pytest.mark.parametrize("hours, expected", [(2, True), (-2, False)])
def test_unexpired_deferral_reads_the_marker_time(monkeypatch, hours, expected):
    when = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _comments_client(
        monkeypatch, [{"body": f"<!-- review-deferred-until:{when} -->"}]
    )
    d = sd.SwarmDispatcher(
        _Notifier(),
        sd.DispatchConfig(neotoma_token="", github_token="", auto_merge=False),
    )
    assert _run(d._unexpired_deferral(REPO, PR)) is expected


def test_two_sweeps_never_run_at_once(sweep, monkeypatch):
    d, state = sweep
    release = asyncio.Event()

    async def slow_list(self, repository, label):
        await release.wait()
        return state["listed"]

    monkeypatch.setattr(sd.SwarmDispatcher, "_open_labelled_prs", slow_list)

    async def scenario():
        first = asyncio.create_task(d.repeat_labelled_panel_sweep([REPO]))
        await asyncio.sleep(0)
        second = await d.repeat_labelled_panel_sweep([REPO])
        release.set()
        return second, await first

    second, first = _run(scenario())
    assert second is None and first["resumed"] == 1
    assert state["entered"] == [(PR, OLD_HEAD)]


# ── APIS_PANEL_SWEEP_INTERVAL_S ─────────────────────────────────────────────


def test_the_interval_defaults_to_600_seconds():
    import apis

    assert apis.panel_sweep_interval_s({}) == 600
    assert apis.panel_sweep_interval_s({"APIS_PANEL_SWEEP_INTERVAL_S": "120"}) == 120


@pytest.mark.parametrize("raw", ["0", "abc", "-5", "1.5"])
def test_zero_or_an_invalid_value_turns_the_sweep_off(raw, caplog):
    import apis

    with caplog.at_level(logging.WARNING):
        assert apis.panel_sweep_interval_s({"APIS_PANEL_SWEEP_INTERVAL_S": raw}) == 0
    warned = any("APIS_PANEL_SWEEP_INTERVAL_S" in r.getMessage() for r in caplog.records)
    assert warned is (raw != "0"), "only an invalid value warns"


def test_an_off_sweep_never_runs(sweep):
    import apis

    d, state = sweep
    _run(asyncio.wait_for(apis.repeating_panel_sweep(d, 0), timeout=1))
    assert state["entered"] == [] and state["judged"] == []


def test_a_failed_pass_never_stops_the_loop(monkeypatch):
    import apis

    calls = []

    class _D:
        config = type("C", (), {"resume_repositories": [REPO]})()

        async def repeat_labelled_panel_sweep(self, repositories):
            calls.append(repositories)
            if len(calls) == 1:
                raise RuntimeError("github down")
            if len(calls) == 3:
                raise asyncio.CancelledError

    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(apis.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        _run(apis.repeating_panel_sweep(_D(), 600))
    assert len(calls) == 3, "the loop kept going after the failed pass"


def test_apis_starts_the_repeating_sweep_beside_the_startup_sweep():
    import inspect

    import apis

    src = inspect.getsource(apis.main)
    assert "repeating_panel_sweep(dispatcher, panel_sweep_interval_s())" in src
