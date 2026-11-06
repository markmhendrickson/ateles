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
    _run(d._handle_pr(_trigger()))
    assert state["seated"] == []
    assert state["gate_reads"] == 0


@pytest.mark.parametrize("pr", [
    {}, [],
    {key: value for key, value in _pr().items() if key != "state"},
    *(_pr(state=value) for value in (None, "unknown", "OPEN", " open ", 1)),
    *({**_pr(), "head": value} for value in ([], {"sha": 1})),
    *({**_pr(), "base": value} for value in ([], {}, {"ref": None}, {"ref": 1})),
])
def test_invalid_live_metadata_cannot_release_even_green_checks(world, pr):
    d, state = world
    state.update(pr=pr, gate=GREEN)
    _run(d._handle_pr(_trigger()))
    assert state["seated"] == []
    assert state["gate_reads"] == 0
    assert KEY in d._panel_ci_holds


@pytest.mark.parametrize("live_head", [OTHER_HEAD, ""], ids=["changed", "missing"])
def test_green_checks_for_another_or_unknown_live_head_cannot_release_review(world, live_head):
    d, state = world
    state["pr"] = _pr(head=live_head)
    state["gate"] = GREEN
    _run(d._handle_pr(_trigger()))
    assert state["seated"] == []
    assert state["gate_reads"] == 0
    assert KEY in d._panel_ci_holds


def test_direct_missing_lens_entry_holds_before_preparing_or_seating(world, monkeypatch):
    d, state = world
    state["gate"] = RED

    async def no_files(*args, **kwargs):
        return []

    async def no_gates(*args, **kwargs):
        return {}

    async def providers(*args, **kwargs):
        return ["codex"]

    monkeypatch.setattr(d, "_changed_files", no_files)
    monkeypatch.setattr(d, "_live_gate_status", no_gates)
    monkeypatch.setattr(sd, "usable_providers_async", providers)
    _run(d._redispatch_missing_lens(REPO, _pr(), "security"))
    assert state["seated"] == []
    assert KEY in d._panel_ci_holds


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


# ── a restart drops holds and in-flight panels; the startup sweep restores ──

CANARY = "swarm-canary"


def _labelled_pr(head=HEAD, labels=(CANARY,), number=PR, draft=False):
    return {**_pr(head=head), "number": number, "draft": draft,
            "labels": [{"name": name} for name in labels]}


def _lens_comment(lens, head=HEAD, cid=1, verdict="**SIGNED_OFF**"):
    """A lens comment at `head` in the contract shape: marker, header, verdict.

    `verdict=None` gives a marker-only comment (what a review that died
    mid-post leaves); any other string is placed on the fixed verdict line.
    """
    agent = sd.lens_by_name(lens).agent
    if verdict is None:
        text = "looks fine"
    else:
        text = f"**🤖 {agent.capitalize()} — Ateles swarm, {lens} lens**\n{verdict}\n\nlooks fine"
    return {"id": cid, "created_at": f"2026-11-06T00:00:{cid:02d}Z",
            "body": f"<!-- review:{lens} commit={head} -->\n{text}"}


_VANELLUS_HEADER = "**🤖 Vanellus — Ateles swarm, PR steward**"


def _aggregation_comment(head=HEAD, cid=9, verdict="**APPROVE**"):
    """An aggregation at `head` in the contract shape: marker, header, verdict."""
    text = "" if verdict is None else f"\n{_VANELLUS_HEADER}\n{verdict}\n\nBlocking: 0"
    return {"id": cid, "created_at": f"2026-11-06T00:00:{cid:02d}Z",
            "body": f"<!-- vanellus-aggregation commit={head} -->{text}"}


@pytest.fixture
def sweep_world(world, monkeypatch):
    """`world` after a restart: a fresh dispatcher with the label gate set,
    GitHub's open-PR list and comment thread scripted."""
    _, state = world
    state.update(pr=_labelled_pr(), listed=[_labelled_pr()], comments=[],
                 files=["docs/x.md"],
                 list_error=None, comment_error=None)
    d = sd.SwarmDispatcher(
        _Notifier(),
        sd.DispatchConfig(
            neotoma_token="", github_token="", auto_merge=False,
            auto_rereview_on_push=False, require_label=CANARY,
        ),
    )

    async def ready(self, repos):
        return None

    async def comments(self, repository, number, client):
        if state["comment_error"]:
            raise state["comment_error"]
        return state["comments"]

    async def files(self, trigger):
        return state["files"]

    monkeypatch.setattr(sd.SwarmDispatcher, "_await_github_app_ready", ready)
    monkeypatch.setattr(sd.SwarmDispatcher, "_lens_scoped_comments", comments)
    monkeypatch.setattr(sd.SwarmDispatcher, "_changed_files", files)
    # The real `_open_labelled_prs` runs, so its label and draft filter is the
    # one under test; only GitHub's HTTP response is scripted.
    class _ListResp:
        def raise_for_status(self):
            if state["list_error"]:
                raise state["list_error"]

        def json(self):
            return state["listed"]

    class _ListClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _ListResp()

    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **k: _ListClient())
    monkeypatch.setattr(sd.SwarmDispatcher, "_github_headers", lambda self, repo=None: {})
    # The sweep paces itself between PRs; no test should really wait.
    monkeypatch.setattr(sd.SwarmDispatcher, "_LABELLED_SWEEP_PACE_SECONDS", 0)
    return d, state


def test_after_a_restart_a_labelled_pending_pr_is_held_then_runs_on_completion(
    sweep_world, monkeypatch
):
    d, state = sweep_world
    state["gate"] = PENDING
    assert d._panel_ci_holds == {}, "a fresh process remembers no hold"

    async def pending_ci(self, trigger):
        return "pending"

    monkeypatch.setattr(sd.SwarmDispatcher, "_required_ci_state", pending_ci)
    summary = _run(d.resume_labelled_panels([REPO]))
    assert summary["resumed"] == 1
    assert state["seated"] == []
    assert d._panel_ci_holds[KEY].head == HEAD, "the sweep rebuilt the hold"

    state["gate"] = GREEN
    with pytest.raises(_Seated):
        _run(d._handle_ci_status(_ci_trigger()))
    assert state["seated"] == ["lanius"], "the check completion ran the panel"


def test_a_labelled_green_pr_with_no_panel_at_its_head_runs_from_the_sweep(
    sweep_world,
):
    d, state = sweep_world
    state["gate"] = GREEN
    # A verdict for an OLDER head is not a finished panel for this one.
    state["comments"] = [
        {"id": 1, "created_at": "2026-11-05T00:00:00Z",
         "body": f"<!-- vanellus-aggregation commit={OTHER_HEAD} -->\nok"},
    ]
    summary = _run(d.resume_labelled_panels([REPO]))
    assert state["seated"] == ["lanius"]
    assert summary["failed"] == 1, "the recorder's raise is caught, never escapes"


@pytest.mark.parametrize("finished", ["aggregation", "every_lens"])
def test_a_finished_panel_is_left_alone_and_an_unlabelled_pr_never_touched(
    sweep_world, finished
):
    d, state = sweep_world
    state["gate"] = GREEN
    required = {
        lens.lens for lens in sd.select_panel(set(), state["files"], d.config.panel_max)
    }
    if finished == "aggregation":
        state["comments"] = [_aggregation_comment()]
    else:
        state["comments"] = [
            _lens_comment(lens, cid=i) for i, lens in enumerate(sorted(required), 1)
        ]
    state["listed"] = [
        _labelled_pr(),
        _labelled_pr(number=PR + 1, labels=("other",)),
        _labelled_pr(number=PR + 2, labels=()),
    ]
    summary = _run(d.resume_labelled_panels([REPO]))
    assert state["seated"] == []
    assert state["gate_reads"] == 0, "no PR was driven into review"
    assert summary["scanned"] == 1 and summary["finished"] == 1
    assert d._panel_ci_holds == {}


@pytest.mark.parametrize("finished", ["aggregation", "every_lens"])
def test_a_panel_finished_with_a_blocking_verdict_is_left_alone(sweep_world, finished):
    d, state = sweep_world
    state["gate"] = GREEN
    required = sorted(
        lens.lens for lens in sd.select_panel(set(), state["files"], d.config.panel_max)
    )
    if finished == "aggregation":
        state["comments"] = [_aggregation_comment(verdict="**REQUEST_CHANGES**")]
    else:
        state["comments"] = [
            _lens_comment(lens, cid=i, verdict="**REQUEST_CHANGES**" if i == 1 else "**SIGNED_OFF**")
            for i, lens in enumerate(required, 1)
        ]
    summary = _run(d.resume_labelled_panels([REPO]))
    assert state["seated"] == [] and state["gate_reads"] == 0
    assert summary["finished"] == 1


@pytest.mark.parametrize(
    "shape",
    ["aggregation_marker_only", "aggregation_malformed", "newer_aggregation_marker_only",
     "lens_marker_only", "lens_malformed"],
)
def test_a_marker_without_a_verdict_at_the_head_is_recovered(sweep_world, shape):
    d, state = sweep_world
    state["gate"] = GREEN
    required = sorted(
        lens.lens for lens in sd.select_panel(set(), state["files"], d.config.panel_max)
    )
    if shape == "aggregation_marker_only":
        state["comments"] = [_aggregation_comment(verdict=None)]
    elif shape == "newer_aggregation_marker_only":
        state["comments"] = [_aggregation_comment(cid=8), _aggregation_comment(cid=9, verdict=None)]
    elif shape == "aggregation_malformed":
        state["comments"] = [_aggregation_comment(verdict="Verdict: looks good to me")]
    else:
        # Every lens but the first reports properly; the first leaves a marker
        # with no verdict, or a verdict that is not on the fixed line.
        broken = None if shape == "lens_marker_only" else "no verdict line here\n**SIGNED_OFF**"
        state["comments"] = [
            _lens_comment(lens, cid=i, verdict=broken if i == 1 else "**SIGNED_OFF**")
            for i, lens in enumerate(required, 1)
        ]
    summary = _run(d.resume_labelled_panels([REPO]))
    assert state["seated"] == ["lanius"], "the unfinished panel was re-driven"
    assert summary["finished"] == 0


def test_one_missing_required_lens_is_not_a_finished_panel(sweep_world):
    d, state = sweep_world
    state["gate"] = GREEN
    required = sorted(
        lens.lens for lens in sd.select_panel(set(), state["files"], d.config.panel_max)
    )
    assert len(required) > 1
    state["comments"] = [_lens_comment(lens, cid=i) for i, lens in enumerate(required[1:], 1)]
    _run(d.resume_labelled_panels([REPO]))
    assert state["seated"] == ["lanius"]


def test_with_the_label_gate_unset_the_sweep_does_nothing(sweep_world):
    d, state = sweep_world
    d.config.require_label = ""
    state["gate"] = GREEN
    summary = _run(d.resume_labelled_panels([REPO]))
    assert summary["scanned"] == 0
    assert state["seated"] == [] and state["gate_reads"] == 0


@pytest.mark.parametrize("where", ["list", "comments"])
def test_a_github_read_failure_keeps_apis_running_and_starts_no_panel(
    sweep_world, where
):
    d, state = sweep_world
    state["gate"] = GREEN
    state[f"{'list' if where == 'list' else 'comment'}_error"] = httpx.ConnectError("down")
    summary = _run(d.resume_labelled_panels([REPO]))  # must not raise
    assert state["seated"] == [] and state["gate_reads"] == 0
    assert d._panel_ci_holds == {}
    if where == "comments":
        assert summary["unreadable"] == 1


def test_the_sweep_handles_at_most_twenty_prs(sweep_world, monkeypatch):
    d, state = sweep_world
    state["listed"] = [_labelled_pr(number=n) for n in range(1, 30)]
    driven = []

    async def handle_pr(self, trigger):
        driven.append(trigger.number)

    monkeypatch.setattr(sd.SwarmDispatcher, "_handle_pr", handle_pr)
    summary = _run(d.resume_labelled_panels([REPO]))
    assert len(driven) == 20 and summary["capped"] is True


# ── a GitHub limit during the sweep is waited out, and what it leaves is retried ──


def _gh(status, payload=None, headers=None, text=None):
    """A real `httpx.Response`, so `raise_for_status` behaves as in production."""
    request = httpx.Request("GET", "https://api.github.com/x")
    if text is not None:
        return httpx.Response(status, headers=headers or {}, text=text, request=request)
    return httpx.Response(status, headers=headers or {}, json=payload, request=request)


class _ScriptedGitHub:
    """GitHub answering each GET from a per-path script (last answer repeats)."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        assert "Authorization" not in (headers or {})
        for suffix, answers in self.script.items():
            if url.endswith(suffix):
                self.calls.append(suffix)
                return answers.pop(0) if len(answers) > 1 else answers[0]
        raise AssertionError(f"unscripted GitHub read: {url}")


@pytest.fixture
def limit_sleeps(monkeypatch):
    import github_rate_limit

    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(github_rate_limit, "_sleep", sleep)
    return waits


def test_a_secondary_limit_403_with_retry_after_is_waited_out_and_the_read_succeeds(
    sweep_world, monkeypatch, limit_sleeps
):
    d, state = sweep_world
    state["gate"] = GREEN
    limited = _gh(
        403,
        {"message": "You have exceeded a secondary rate limit."},
        headers={"Retry-After": "7", "x-ratelimit-remaining": "4800"},
    )
    github = _ScriptedGitHub({"/pulls": [limited, _gh(200, [_labelled_pr()])]})
    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **k: github)
    summary = _run(d.resume_labelled_panels([REPO]))
    assert limit_sleeps == [7.0], "the wait GitHub asked for was honoured"
    assert github.calls == ["/pulls", "/pulls"], "one retry, then success"
    assert summary["scanned"] == 1
    assert state["seated"] == ["lanius"], "the PR's panel ran after the limit"
    assert d._labelled_repos_due == {}


def test_a_plain_permission_403_is_a_logged_failure_and_is_not_retried(
    sweep_world, monkeypatch, limit_sleeps, caplog
):
    import github_rate_limit

    d, state = sweep_world
    state["gate"] = GREEN

    async def real_comments(self, repository, number, client):
        return (await self._issue_comments_scan(repository, number, client))[0]

    monkeypatch.setattr(sd.SwarmDispatcher, "_lens_scoped_comments", real_comments)
    refused = _gh(403, {"message": "Resource not accessible by integration"})
    github = _ScriptedGitHub(
        {"/pulls": [_gh(200, [_labelled_pr()])], "/comments": [refused]}
    )
    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **k: github)
    with caplog.at_level(logging.WARNING):
        summary = _run(d.resume_labelled_panels([REPO]))
    comment_reads = github.calls.count("/comments")
    assert 1 <= comment_reads <= github_rate_limit.MAX_ATTEMPTS
    assert comment_reads == 1 and limit_sleeps == [], "a real refusal is not retried"
    assert summary["unreadable"] == 1 and summary["finished"] == 0
    assert state["seated"] == [] and state["gate_reads"] == 0, "never read as green"
    assert "could not read comments" in caplog.text and "403" in caplog.text
    assert d._labelled_panels_due == {(REPO, PR): REPO}, "it stays due"


def test_a_pr_the_startup_sweep_could_not_read_gets_its_panel_from_the_periodic_step(
    sweep_world,
):
    d, state = sweep_world
    state["gate"] = GREEN
    state["comment_error"] = httpx.HTTPStatusError(
        "403", request=httpx.Request("GET", "u"), response=httpx.Response(403)
    )
    summary = _run(d.resume_labelled_panels([REPO]))
    assert summary["unreadable"] == 1 and state["seated"] == []
    assert (REPO, PR) in d._labelled_panels_due

    # Still limited at the next periodic pass: nothing starts, it stays due.
    _run(d.retry_due_labelled_panels())
    assert state["seated"] == [] and (REPO, PR) in d._labelled_panels_due

    state["comment_error"] = None  # the limit clears; no restart happens
    _run(d.retry_due_labelled_panels())
    assert state["seated"] == ["lanius"], "the periodic step ran the panel"
    assert d._labelled_panels_due == {}


def test_a_pr_that_never_becomes_readable_is_retried_a_bounded_number_of_times(
    sweep_world, caplog
):
    d, state = sweep_world
    state["comment_error"] = RuntimeError("gone")
    _run(d.resume_labelled_panels([REPO]))
    for _ in range(d._LABELLED_RETRY_MAX_MISSES - 1):
        _run(d.retry_due_labelled_panels())
        assert (REPO, PR) in d._labelled_panels_due
    with caplog.at_level(logging.ERROR):
        summary = _run(d.retry_due_labelled_panels())
    assert summary["dropped"] == 1 and d._labelled_panels_due == {}
    assert "no longer retried" in caplog.text
    assert state["seated"] == [], "an unreadable PR never counts as green"


def test_the_periodic_retry_keeps_due_the_prs_its_cap_did_not_reach(
    sweep_world, monkeypatch
):
    d, state = sweep_world
    state["list_error"] = RuntimeError("limited")
    _run(d.resume_labelled_panels([REPO]))
    assert d._labelled_repos_due == {REPO.casefold(): REPO}
    state["list_error"] = None
    state["listed"] = [_labelled_pr(number=n) for n in range(1, 26)]
    driven = []

    async def handle_pr(self, trigger):
        driven.append(trigger.number)

    monkeypatch.setattr(sd.SwarmDispatcher, "_handle_pr", handle_pr)
    summary = _run(d.retry_due_labelled_panels())
    assert summary["capped"] is True and len(driven) == 20
    assert sorted(n for _, n in d._labelled_panels_due) == [21, 22, 23, 24, 25]


def test_the_periodic_retry_runs_on_the_existing_review_sweep_loop():
    import inspect

    import apis

    src = inspect.getsource(apis.main)
    loop = src[src.index("async def deferred_review_sweep"):]
    loop = loop[: loop.index("await asyncio.sleep(deferred_interval)")]
    assert "retry_due_labelled_panels()" in loop


def test_the_periodic_step_starts_no_second_panel_on_a_head_already_running(
    sweep_world, monkeypatch
):
    d, state = sweep_world
    state["gate"] = GREEN
    state["comment_error"] = RuntimeError("limited")
    _run(d.resume_labelled_panels([REPO]))
    assert (REPO, PR) in d._labelled_panels_due
    state["comment_error"] = None
    bodies, release = [], {}

    async def body(self, trigger):
        bodies.append(trigger.head_sha)
        await release["event"].wait()

    monkeypatch.setattr(sd.SwarmDispatcher, "_handle_pr_body", body)

    async def scenario():
        release["event"] = asyncio.Event()
        running = asyncio.create_task(
            d._handle_pr(d._pr_trigger_from_api(REPO, _labelled_pr()))
        )
        await asyncio.sleep(0)  # the webhook-driven panel is now in flight
        summary = await d.retry_due_labelled_panels()
        release["event"].set()
        await running
        return summary

    summary = _run(scenario())
    assert bodies == [HEAD], "only the panel already in flight ran"
    assert summary["running"] == 1 and summary["resumed"] == 0
    assert d._panels_running == set()


def test_the_periodic_step_starts_no_second_panel_on_a_head_already_finished(
    sweep_world,
):
    d, state = sweep_world
    state["gate"] = GREEN
    state["comment_error"] = RuntimeError("limited")
    _run(d.resume_labelled_panels([REPO]))
    state["comment_error"] = None
    state["comments"] = [_aggregation_comment()]  # the panel finished meanwhile
    summary = _run(d.retry_due_labelled_panels())
    assert state["seated"] == [] and state["gate_reads"] == 0
    assert summary["finished"] == 1 and d._labelled_panels_due == {}


def test_the_sweeps_github_requests_are_sequential_and_paced(sweep_world, monkeypatch):
    d, state = sweep_world
    state["listed"] = [_labelled_pr(number=n) for n in (1, 2, 3)]
    events, in_flight, peak = [], [0], [0]

    async def finished(self, trigger):
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        events.append(f"read {trigger.number}")
        await asyncio.sleep(0)  # yield: a concurrent sweep would overlap here
        in_flight[0] -= 1
        return True

    async def pause(self):
        events.append("pause")

    monkeypatch.setattr(sd.SwarmDispatcher, "_panel_finished_for_pr", finished)
    monkeypatch.setattr(sd.SwarmDispatcher, "_labelled_sweep_pause", pause)
    summary = _run(d.resume_labelled_panels([REPO]))
    assert summary["finished"] == 3
    assert peak[0] == 1, "never two PR reads in flight at once"
    assert events == ["read 1", "pause", "read 2", "pause", "read 3"]


def test_apis_startup_runs_the_sweep_as_a_gather_sibling():
    import inspect

    import apis

    src = inspect.getsource(apis.main)
    assert "resume_labelled_panels" in src
    gather = src[src.rfind("asyncio.gather("):]
    assert "labelled_panel_sweep()" in gather


# ── an admitted label add re-runs the review, a non-admitted one does not ──


def _labeled_payload(label):
    pr = {**_pr(), "body": "no parent here", "labels": [{"name": label}]}
    return {"action": "labeled", "label": {"name": label},
            "repository": {"full_name": REPO}, "pull_request": pr}


def test_an_admitted_label_add_on_a_green_pr_with_no_parent_issue_runs_the_panel(
    world, monkeypatch
):
    import github_gateway

    d, state = world
    d.config.auto_rereview_on_push = True
    d.config.narrow_rereview = False
    state["gate"] = GREEN
    state["pr"] = {**_pr(), "body": "no parent here"}
    monkeypatch.setattr(github_gateway, "_REQUIRE_LABEL", CANARY)

    class _Result:
        ok = True
        stdout = "GATE_INHERITANCE: blocked\nGATE_PENDING: pm"

    async def run_skill(agent, *args, **kwargs):
        if agent == "lanius":
            return _Result()
        state["seated"].append(agent)
        raise _Seated(agent)

    async def nothing(*a, **k):
        return None

    async def no_files(*a, **k):
        return []

    async def reprove(*a, **k):
        return set(), []

    async def head(self, t):
        return HEAD

    async def gates(*a, **k):
        return {}

    monkeypatch.setattr(sd, "run_skill", run_skill)
    monkeypatch.setattr(sd.SwarmDispatcher, "_changed_files", no_files)
    monkeypatch.setattr(sd.SwarmDispatcher, "_post_gate_block", nothing)
    monkeypatch.setattr(sd.SwarmDispatcher, "_supersede_stale_verdicts", nothing)
    monkeypatch.setattr(sd.SwarmDispatcher, "_reprove_signed_off_gates", reprove)
    monkeypatch.setattr(sd.SwarmDispatcher, "_pr_head_sha", head)
    monkeypatch.setattr(sd.SwarmDispatcher, "_live_gate_status", gates)

    rejected = github_gateway.parse_github_event(
        "pull_request", _labeled_payload("other"), "d1"
    )
    assert rejected is None, "a label event that is not admitted builds no trigger"

    trigger = github_gateway.parse_github_event(
        "pull_request", _labeled_payload(CANARY), "d2"
    )
    assert trigger is not None and trigger.kind == "pr_labeled"
    with pytest.raises(_Seated):
        _run(d._handle_pr(trigger))
    assert state["seated"], "the admitted label add seated the review panel"
    assert "lanius" not in state["seated"]


# ── PR #1616 review: a verdict counts only at the fixed position ──


@pytest.mark.parametrize("shape", ["prose_after_marker", "prose_after_header"])
def test_an_aggregation_that_only_quotes_a_bold_verdict_is_recovered(sweep_world, shape):
    d, state = sweep_world
    state["gate"] = GREEN
    prose = "An earlier round said **APPROVE**, but this run stopped before aggregating."
    lead = "" if shape == "prose_after_marker" else f"{_VANELLUS_HEADER}\n"
    state["comments"] = [
        {"id": 9, "created_at": "2026-11-06T00:00:09Z",
         "body": f"<!-- vanellus-aggregation commit={HEAD} -->\n{lead}{prose}"},
    ]
    summary = _run(d.resume_labelled_panels([REPO]))
    assert state["seated"] == ["lanius"], "a quoted verdict is not a finished panel"
    assert summary["finished"] == 0


def test_a_lens_that_only_quotes_a_bold_verdict_is_not_counted_finished(sweep_world):
    d, state = sweep_world
    state["gate"] = GREEN
    required = sorted(
        lens.lens for lens in sd.select_panel(set(), state["files"], d.config.panel_max)
    )
    first = required[0]
    agent = sd.lens_by_name(first).agent
    quoting = {
        "id": 1, "created_at": "2026-11-06T00:00:01Z",
        "body": (
            f"<!-- review:{first} commit={HEAD} -->\n"
            f"**🤖 {agent.capitalize()} — Ateles swarm, {first} lens**\n"
            "Still reading; the last round's verdict was **SIGNED_OFF**."
        ),
    }
    state["comments"] = [quoting] + [
        _lens_comment(lens, cid=i) for i, lens in enumerate(required[1:], 2)
    ]
    summary = _run(d.resume_labelled_panels([REPO]))
    assert state["seated"] == ["lanius"], "the quoting lens did not finish"
    assert summary["finished"] == 0


@pytest.mark.parametrize("shape", ["aggregation_single", "aggregation_fallback", "lens_fallback"])
def test_real_apis_comment_shapes_still_count_as_finished(sweep_world, shape):
    d, state = sweep_world
    state["gate"] = GREEN
    required = sorted(
        lens.lens for lens in sd.select_panel(set(), state["files"], d.config.panel_max)
    )
    if shape == "aggregation_single":
        state["comments"] = [_aggregation_comment()]
    elif shape == "aggregation_fallback":
        # The dispatcher wraps Vanellus's own reply, which repeats the marker
        # and header, so both appear twice before the verdict.
        reply = _aggregation_comment()["body"]
        state["comments"] = [{
            "id": 9, "created_at": "2026-11-06T00:00:09Z",
            "body": sd.compose_vanellus_fallback_comment(reply, commit_sha=HEAD),
        }]
    else:
        # A lens fallback adds a `review:<lens>` line and the dispatcher's
        # header ahead of the lens's own marker, header and verdict.
        state["comments"] = [
            {**_lens_comment(lens, cid=i),
             "body": sd.compose_fallback_comment(
                 lens, sd.lens_by_name(lens).agent, _lens_comment(lens)["body"], HEAD)}
            for i, lens in enumerate(required, 1)
        ]
    summary = _run(d.resume_labelled_panels([REPO]))
    assert state["seated"] == []
    assert summary["finished"] == 1
