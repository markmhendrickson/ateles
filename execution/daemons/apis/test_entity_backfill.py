"""
A gate op against an entity-less issue must recover, not dead-end (ateles#416).

## The failure

Gate state lives on the Neotoma issue entity. When that entity does not exist a
gate can be neither SIGNED nor WAIVED — both operate on an object that is not
there. The waive path said so and stopped:

    ⚠️ Gate waive could not be applied — no Neotoma issue entity was found for
    this issue, so there is no `gate_status` to waive. The gate pipeline will
    keep blocking until the issue is triaged

The remedy it names is real. Nothing was scheduled to perform it. Triage fires
on `issue.opened` only — one-shot, no sweep, no retry — so an issue whose triage
failed, or that predates the pipeline, stays entity-less permanently and every
PR closing it is unmergeable by every available path.

Observed on ateles#415, blocked because #414 had no entity. The message reads
like a status update while being a dead end.

## The shape it shares with #414 and #431

The recovery path keys on an artifact the failure prevented from existing:
a missing entity here, a missing marker on #414, a missing lens verdict on #431.

Run: pytest execution/daemons/apis/test_entity_backfill.py -v
"""

from __future__ import annotations

import pytest

import swarm_dispatch as sd


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, msg: str, priority=None, handler=None) -> None:  # noqa: ANN001
        self.sent.append(msg)


def _dispatcher() -> sd.SwarmDispatcher:
    return sd.SwarmDispatcher(notifier=_Notifier())


class _State:
    def __init__(self, found: bool) -> None:
        self.found = found


def _stub_store(monkeypatch, found_sequence: list[bool]) -> dict:
    """Make IssueGateStore.load return each value in turn; record call count."""
    calls = {"n": 0}

    async def fake_load(self, repo, issue_number):  # noqa: ANN001
        i = min(calls["n"], len(found_sequence) - 1)
        calls["n"] += 1
        return _State(found_sequence[i])

    monkeypatch.setattr(sd.IssueGateStore, "load", fake_load)
    return calls


@pytest.mark.asyncio
async def test_existing_entity_is_a_no_op(monkeypatch):
    """The fast path must not dispatch an agent for an entity that exists."""
    d = _dispatcher()
    _stub_store(monkeypatch, [True])
    dispatched: list[str] = []

    async def fake_run_skill(agent, *a, **k):  # noqa: ANN001
        dispatched.append(agent)

    monkeypatch.setattr(sd, "run_skill", fake_run_skill)

    assert await d.ensure_issue_entity("o/r", 414) is True
    assert dispatched == [], "no triage dispatch when the entity already exists"


@pytest.mark.asyncio
async def test_missing_entity_is_backfilled(monkeypatch):
    """The ateles#414 case: absent, then present after triage."""
    d = _dispatcher()
    _stub_store(monkeypatch, [False, True])
    dispatched: list[str] = []

    async def fake_fetch(self, repo, n):  # noqa: ANN001
        return {"title": "t", "body": "b", "user": {"login": "u"}, "html_url": ""}

    class _Ok:
        ok = True
        stdout = ""
        error = None
        returncode = 0

    async def fake_run_skill(agent, *a, **k):  # noqa: ANN001
        dispatched.append(agent)
        return _Ok()

    monkeypatch.setattr(sd.SwarmDispatcher, "_fetch_issue", fake_fetch)
    monkeypatch.setattr(sd, "run_skill", fake_run_skill)

    assert await d.ensure_issue_entity("o/r", 414) is True
    assert dispatched == ["lanius"]


@pytest.mark.asyncio
async def test_triage_reporting_ok_is_not_evidence_the_entity_landed(monkeypatch):
    """The #285 rule, applied here.

    An LLM turn asked to persist a mechanical mutation can silently no-op. If
    the re-read still finds nothing, this must return False — trusting the exit
    code would report a backfill that did not happen.
    """
    d = _dispatcher()
    _stub_store(monkeypatch, [False, False])  # still absent after triage

    async def fake_fetch(self, repo, n):  # noqa: ANN001
        return {"title": "t", "body": "", "user": {}, "html_url": ""}

    class _Ok:
        ok = True
        stdout = ""
        error = None
        returncode = 0

    monkeypatch.setattr(sd.SwarmDispatcher, "_fetch_issue", fake_fetch)
    monkeypatch.setattr(sd, "run_skill", lambda *a, **k: _async(_Ok()))

    assert await d.ensure_issue_entity("o/r", 414) is False


def _async(value):
    async def _inner():
        return value

    return _inner()


@pytest.mark.asyncio
async def test_unreadable_github_issue_does_not_dispatch(monkeypatch):
    """Never triage an issue we could not read — that invents content."""
    d = _dispatcher()
    _stub_store(monkeypatch, [False])
    dispatched: list[str] = []

    async def fake_fetch(self, repo, n):  # noqa: ANN001
        return None

    async def fake_run_skill(agent, *a, **k):  # noqa: ANN001
        dispatched.append(agent)

    monkeypatch.setattr(sd.SwarmDispatcher, "_fetch_issue", fake_fetch)
    monkeypatch.setattr(sd, "run_skill", fake_run_skill)

    assert await d.ensure_issue_entity("o/r", 414) is False
    assert dispatched == []


@pytest.mark.asyncio
async def test_failed_triage_returns_false(monkeypatch):
    d = _dispatcher()
    _stub_store(monkeypatch, [False])

    async def fake_fetch(self, repo, n):  # noqa: ANN001
        return {"title": "t", "body": "", "user": {}, "html_url": ""}

    class _Fail:
        ok = False
        stdout = ""
        error = "boom"
        returncode = 1

    monkeypatch.setattr(sd.SwarmDispatcher, "_fetch_issue", fake_fetch)
    monkeypatch.setattr(sd, "run_skill", lambda *a, **k: _async(_Fail()))

    assert await d.ensure_issue_entity("o/r", 414) is False


def test_the_waive_path_calls_the_backfill():
    """The dead end must be wired to the remedy, not merely near it.

    Loxia caught the inverse on #442 — a mechanism defined and invoked from
    nowhere. Asserting the call site means that omission fails a test.
    """
    from pathlib import Path

    source = Path(__file__).with_name("swarm_dispatch.py").read_text()
    assert "await self.ensure_issue_entity(" in source, (
        "ensure_issue_entity is never called — the waive dead end would remain"
    )
    # And the escalation must name a runnable command, not just describe a state.
    assert "trigger_swarm_pr.py issue" in source, (
        "the escalation must give the operator an actual command to run"
    )
