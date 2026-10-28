"""The review panel is held until the PR head's gating checks are green.

An audit of 469 lens-reviewed PRs found 47 where a lens approved a head that
already had a failed check, 23 where a check went red after approval, and 6
that merged red. `SwarmDispatcher._hold_panel_until_checks_green` (and
`panel_check_gate`, which it and the bootstrap lens runner share) seats no lens
on a head until that head's checks are green by the standard the panel-gated
approval tool applies.

A "panel was seated" assertion is observed by replacing `run_skill` with a
recorder that raises, so a hold that leaks shows up as the recorder firing, not
as an absent log line. What each test looked like with the change reverted is
recorded in the PR body.

Run: pytest execution/daemons/apis/test_panel_check_hold.py -v
"""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx
import pytest

import panel_check_gate as pcg
import swarm_dispatch as sd
from github_gateway import SwarmTrigger

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
REPO = "owner/repo"
PR = 87
KEY = (REPO, PR)


class _Seated(Exception):
    """Raised by the fake `run_skill`: an agent was dispatched."""


class _Notifier:
    def __init__(self):
        self.sent = []

    def send(self, message, priority=None, handler=None, **kwargs):
        self.sent.append(message)

    def clear_dedupe(self, key):
        pass


def _outcome(name, state, passed):
    return type(
        "O", (), {"name": name, "state": state, "passed": passed, "superseded": False}
    )()


RED = pcg.describe_gate(
    HEAD,
    [_outcome("pytest", "failing (failure)", False), _outcome("lint", "success", True)],
    False,
)
PENDING = pcg.describe_gate(
    HEAD, [_outcome("pytest", "pending (in_progress)", False)], False
)
GREEN = pcg.describe_gate(HEAD, [_outcome("pytest", "success", True)], True)


def _pr(head=HEAD, state="open"):
    return {
        "number": PR, "state": state, "draft": False, "title": "t",
        "body": "Closes #80", "html_url": "u", "user": {"login": "someone"},
        "head": {"sha": head, "ref": "feat/x"}, "base": {"ref": "main"},
    }


def _trigger(**over):
    base = dict(
        kind="pr_opened", repository=REPO, number=PR, title="t", body="Closes #80",
        author="someone", html_url="u", delivery_id="d", action="opened",
        head_sha=HEAD,
    )
    base.update(over)
    return SwarmTrigger(**base)


@pytest.fixture
def world(monkeypatch):
    """A dispatcher with the REAL hold, a scripted PR and a scripted check gate."""
    monkeypatch.setattr(
        sd.SwarmDispatcher,
        "_hold_panel_until_checks_green",
        sd._REAL_HOLD_PANEL_UNTIL_CHECKS_GREEN,
    )
    state = {"pr": _pr(), "gate": RED, "seated": [], "gate_reads": 0}

    async def fetch_pr(self, repo, num):
        return state["pr"]

    async def read_gate(self, repository, head_sha, base_ref):
        state["gate_reads"] += 1
        state["asked"] = (repository, head_sha, base_ref)
        return state["gate"]

    async def run_skill(agent, *args, **kwargs):
        state["seated"].append(agent)
        raise _Seated(agent)

    async def review_signals(self, *a, **k):
        return None

    monkeypatch.setattr(sd.SwarmDispatcher, "_fetch_pr", fetch_pr)
    monkeypatch.setattr(sd.SwarmDispatcher, "_read_head_check_gate", read_gate)
    monkeypatch.setattr(sd.SwarmDispatcher, "_review_signals", review_signals)
    monkeypatch.setattr(sd, "run_skill", run_skill)
    d = sd.SwarmDispatcher(
        _Notifier(),
        sd.DispatchConfig(
            neotoma_token="", github_token="", auto_merge=False,
            auto_rereview_on_push=False,
        ),
    )
    return d, state


def _run(coro):
    return asyncio.run(coro)


# ── the hold itself ──────────────────────────────────────────────────────────


def test_a_red_head_holds_the_panel_and_says_why(world, caplog):
    d, state = world
    state["gate"] = RED
    with caplog.at_level(logging.INFO):
        _run(d._handle_pr(_trigger()))
    assert state["seated"] == [], "no agent may be dispatched on a red head"
    assert "review panel HELD" in caplog.text
    assert HEAD[:9] in caplog.text
    assert "pytest" in caplog.text and "failing" in caplog.text
    assert d._panel_ci_holds[KEY].head == HEAD
    assert state["asked"] == (REPO, HEAD, "main")


def test_a_pending_head_holds_the_panel_and_names_the_pending_check(world, caplog):
    d, state = world
    state["gate"] = PENDING
    with caplog.at_level(logging.INFO):
        _run(d._handle_pr(_trigger()))
    assert state["seated"] == []
    assert "pending checks: pytest" in caplog.text
    assert KEY in d._panel_ci_holds


def test_an_unreadable_check_state_holds(world, caplog):
    d, state = world
    state["gate"] = pcg.unreadable_gate(HEAD, "boom")
    with caplog.at_level(logging.INFO):
        _run(d._handle_pr(_trigger()))
    assert state["seated"] == []
    assert "check state unreadable" in caplog.text


def test_an_unfetchable_pr_holds(world):
    d, state = world
    state["pr"] = None
    _run(d._handle_pr(_trigger()))
    assert state["seated"] == []
    assert state["gate_reads"] == 0, "no PR means no live head to read; still a hold"
    assert KEY in d._panel_ci_holds


def test_a_check_reader_that_raises_is_a_hold_not_a_pass(monkeypatch):
    """The real reader, with the approval tool's evaluator broken."""
    def broken():
        async def evaluate(*a, **k):
            raise RuntimeError("github 502")
        return evaluate

    monkeypatch.setattr(pcg, "_evaluate_checks", broken)
    gate = _run(pcg.read_check_gate(REPO, HEAD, "main"))
    assert gate.green is False and gate.unreadable
    assert "unreadable" in gate.reason and HEAD[:9] in gate.reason
    assert _run(pcg.read_check_gate(REPO, "", "main")).green is False


def test_a_green_head_dispatches_the_panel(world):
    d, state = world
    state["gate"] = GREEN
    with pytest.raises(_Seated):
        _run(d._handle_pr(_trigger()))
    assert state["seated"] == ["lanius"], "the gate-inheritance check is the first seat"
    assert KEY not in d._panel_ci_holds


def test_a_closed_pr_is_not_held(world):
    d, state = world
    state["pr"] = _pr(state="closed")
    state["gate"] = RED
    with pytest.raises(_Seated):
        _run(d._handle_pr(_trigger()))
    assert state["gate_reads"] == 0


# ── the head going green re-dispatches the held panel, exactly once ──────────


def _ci_trigger(head=HEAD):
    return SwarmTrigger(
        kind="ci_status", repository=REPO, number=PR, title="", body="", author="",
        html_url="", delivery_id="d-ci", action="completed", ci_head_sha=head,
        ci_conclusion="success", ci_pr_numbers=[PR],
    )


def test_a_head_that_goes_green_after_a_hold_is_dispatched_exactly_once(
    world, monkeypatch
):
    d, state = world

    async def pending_ci(self, trigger):
        # The dispatcher's own coarse reading is deliberately NOT green: the
        # held-panel branch must not depend on it.
        return "pending"

    async def not_clear(self, repo, num, head_sha=""):
        return False

    monkeypatch.setattr(sd.SwarmDispatcher, "_required_ci_state", pending_ci)
    monkeypatch.setattr(sd.SwarmDispatcher, "_pr_review_is_clear", not_clear)

    state["gate"] = RED
    _run(d._handle_pr(_trigger()))
    assert state["seated"] == [] and KEY in d._panel_ci_holds

    # A completion while still red re-drives the PR, which stays held.
    _run(d._handle_ci_status(_ci_trigger()))
    assert state["seated"] == [] and KEY in d._panel_ci_holds

    # Checks go green; the completion event dispatches the panel.
    state["gate"] = GREEN
    with pytest.raises(_Seated):
        _run(d._handle_ci_status(_ci_trigger()))
    assert state["seated"] == ["lanius"]
    assert KEY not in d._panel_ci_holds

    # Duplicate completions for the same head must not seat a second panel.
    _run(d._handle_ci_status(_ci_trigger()))
    _run(d._handle_ci_status(_ci_trigger()))
    assert state["seated"] == ["lanius"], "exactly one dispatch for the head"


def test_a_completion_for_a_stale_head_does_not_release_a_hold(world):
    d, state = world
    state["gate"] = RED
    _run(d._handle_pr(_trigger()))
    state["gate"] = GREEN
    _run(d._handle_ci_status(_ci_trigger(head=OTHER_HEAD)))
    assert state["seated"] == []
    assert KEY in d._panel_ci_holds


def test_a_closed_pr_drops_its_hold(world):
    d, state = world
    state["gate"] = RED
    _run(d._handle_pr(_trigger()))
    state["pr"] = _pr(state="closed")
    _run(d._handle_ci_status(_ci_trigger()))
    assert KEY not in d._panel_ci_holds


# ── the sweeps do not count a hold as a failed review ───────────────────────


def test_the_stalled_review_sweep_does_not_redispatch_or_burn_retries_on_a_held_pr(
    world, monkeypatch
):
    d, state = world
    state["gate"] = RED
    called = []

    async def ready(self, repos):
        return None

    async def candidates(self, repository, now, stall_after):
        return [_pr()]

    async def handle_pr(self, trigger):
        called.append(trigger.number)

    monkeypatch.setattr(sd.SwarmDispatcher, "_await_github_app_ready", ready)
    monkeypatch.setattr(sd.SwarmDispatcher, "_prs_with_stalled_review", candidates)
    monkeypatch.setattr(sd.SwarmDispatcher, "_handle_pr", handle_pr)
    for _ in range(5):  # more passes than the retry budget
        summary = _run(d.resume_stalled_reviews([REPO]))
    assert called == []
    assert d._stall_retries == {} and d._stall_escalated == {}
    assert summary["held_for_checks"] == 1
    assert d.notifier.sent == [], "a hold must not page as a failing review agent"


def test_the_missing_lens_sweep_does_not_reseat_a_lens_on_a_held_head(
    world, monkeypatch
):
    d, state = world
    state["gate"] = PENDING
    redispatched = []

    async def ready(self, repos):
        return None

    async def candidates(self, repository):
        return [(_pr(), ["security"])]

    async def redispatch(self, repository, pr, lens):
        redispatched.append(lens)

    monkeypatch.setattr(sd.SwarmDispatcher, "_await_github_app_ready", ready)
    monkeypatch.setattr(sd.SwarmDispatcher, "_prs_with_missing_lens", candidates)
    monkeypatch.setattr(sd.SwarmDispatcher, "_redispatch_missing_lens", redispatch)
    summary = _run(d.resume_missing_lens_reviews([REPO]))
    assert redispatched == []
    assert d._missing_lens_retries.get(f"{REPO}#{PR}", {}) == {}
    assert summary["held_for_checks"] == 1

    state["gate"] = GREEN
    _run(d.resume_missing_lens_reviews([REPO]))
    assert redispatched == ["security"]


# ── the gate reads checks by the approval tool's standard ───────────────────


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _ChecksClient:
    """GitHub, reduced to the endpoints `evaluate_checks` reads."""

    def __init__(self, runs, *, status_error=False):
        self.runs, self.status_error = runs, status_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        if url.endswith("/status"):
            if self.status_error:
                return _Resp({}, 500)
            return _Resp({"state": "pending", "total_count": 0})
        if url.endswith("/check-runs"):
            return _Resp({"check_runs": self.runs, "total_count": len(self.runs)})
        return _Resp({}, 404)  # branch protection unreadable: every check required


def _run_row(i, name, status="completed", conclusion="success"):
    row = {"id": i, "name": name, "status": status,
           "started_at": "2026-10-01T10:00:00Z", "app": {"id": 1}}
    if status == "completed":
        row["conclusion"] = conclusion
    return row


def _real_gate(monkeypatch, runs, **kw):
    pcg._evaluate_checks()  # imports approve_pr_as_app
    approve = sys.modules["approve_pr_as_app"]
    monkeypatch.setattr(approve, "_github_headers", lambda repo=None: {})
    client = _ChecksClient(runs, **kw)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)
    return _run(pcg.read_check_gate(REPO, HEAD, "main"))


def test_the_real_reader_holds_red_pending_and_unreadable_and_releases_green(
    monkeypatch,
):
    red = _real_gate(
        monkeypatch, [_run_row(1, "pytest", conclusion="failure"), _run_row(2, "lint")]
    )
    assert red.green is False and red.failing and "pytest" in red.reason

    pending = _real_gate(monkeypatch, [_run_row(1, "pytest", status="in_progress")])
    assert pending.green is False and pending.pending and "pytest" in pending.reason

    broken = _real_gate(monkeypatch, [_run_row(1, "pytest")], status_error=True)
    assert broken.green is False

    empty = _real_gate(monkeypatch, [])
    assert empty.green is False, "a head nothing has checked is not green"

    green = _real_gate(monkeypatch, [_run_row(1, "pytest"), _run_row(2, "lint")])
    assert green.green is True and green.reason == ""
