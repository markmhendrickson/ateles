"""
Regression tests for ateles#584: Anthus participation state could never load.

Two distinct defects, one per test class below:

1. `load_state_for` POSTed to `/retrieve_entities`, which does not exist on the
   prod HTTP surface (404). The entity-read route is `POST /entities/query`.

2. That 404 was swallowed as a WARNING and `load_state_for` returned `{}` —
   indistinguishable from "this work entity has no prior gates". The caller
   therefore hydrated nothing and dispatched every gate as though it had never
   run, risking re-dispatch of completed work, while Anthus looked healthy.

Run with: pytest execution/daemons/anthus/test_participation_state.py -v
"""

from __future__ import annotations

import asyncio

import anthus
import httpx
import participation
import pytest


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "Not Found" if status_code == 404 else "ok"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Client error '{self.status_code}' for url '...'",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )

    def json(self):
        return self._payload


class _RecordingClient:
    """Stands in for httpx.AsyncClient, recording the paths it is POSTed to.

    Mirrors the real prod surface: /entities/query answers, /retrieve_entities
    404s (which is exactly the behaviour verified against the hosted instance).
    """

    def __init__(self, paths: list[str], payload: dict):
        self._paths = paths
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        self._paths.append(url)
        if url.endswith("/entities/query"):
            return _FakeResponse(200, self._payload)
        return _FakeResponse(404)


# ── Defect 1: the endpoint ───────────────────────────────────────────────────


def test_load_state_for_uses_entities_query_not_retrieve_entities(monkeypatch):
    """The state read must go to POST /entities/query.

    On origin/main this fails: the call goes to /retrieve_entities, which the
    fake (like prod) answers with 404, so no state is returned.
    """
    paths: list[str] = []
    payload = {
        "entities": [
            {
                "entity_id": "ent_pr1",
                "snapshot": {
                    "work_entity_id": "ent_work1",
                    "gate_name": "pm",
                    "status": "satisfied",
                    "dispatched_at": "2026-08-30T10:00:00+00:00",
                    "satisfied_at": "2026-08-30T11:00:00+00:00",
                    "artifact_refs": ["ent_artifact1"],
                },
            }
        ],
        "total": 1,
        "limit": 200,
        "offset": 0,
    }

    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(participation, "NEOTOMA_BASE_URL", "https://neotoma.example")
    monkeypatch.setattr(
        participation.httpx,
        "AsyncClient",
        lambda **kw: _RecordingClient(paths, payload),
    )

    state = asyncio.run(participation.load_state_for("ent_work1"))

    assert paths == ["https://neotoma.example/entities/query"], (
        f"state read must POST to /entities/query, got {paths}"
    )
    assert "pm" in state, "a satisfied pm gate must be visible to the orchestrator"
    assert state["pm"]["status"] == "satisfied"
    assert state["pm"]["artifact_refs"] == ["ent_artifact1"]


def test_load_state_for_returns_empty_dict_when_no_records_exist(monkeypatch):
    """A successful read with zero records is a genuine clean slate, not a failure."""
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(participation, "NEOTOMA_BASE_URL", "https://neotoma.example")
    monkeypatch.setattr(
        participation.httpx,
        "AsyncClient",
        lambda **kw: _RecordingClient([], {"entities": [], "total": 0}),
    )

    assert asyncio.run(participation.load_state_for("ent_work1")) == {}


# ── Defect 2: a failed state load must not present as a healthy dispatch ─────


def test_failed_state_load_raises_rather_than_looking_empty(monkeypatch):
    """A read failure must be distinguishable from "no prior state".

    On origin/main this fails: the 404 is swallowed and `{}` is returned, so
    the caller cannot tell a blind read from a fresh work entity.
    """

    class _AlwaysFails:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, **kwargs):
            return _FakeResponse(404)

    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(participation, "NEOTOMA_BASE_URL", "https://neotoma.example")
    monkeypatch.setattr(participation.httpx, "AsyncClient", lambda **kw: _AlwaysFails())

    with pytest.raises(participation.ParticipationStateUnavailable):
        asyncio.run(participation.load_state_for("ent_work1"))


def test_dispatch_is_held_when_participation_state_cannot_be_read(monkeypatch):
    """The headline guarantee of ateles#584.

    If participation state cannot be read, NO gate may be dispatched and no
    agent may be spawned — dispatching a gate whose prior state is unknown
    risks re-running completed work.

    On origin/main this fails: load_state_for swallows the error and returns
    {}, so orchestration proceeds and gates dispatch against empty state.
    """
    dispatched: list[str] = []
    spawned: list[str] = []

    class _Gate:
        gate_name = "pm"
        owner_agent = "pavo"
        precondition = None

    class _Workflow:
        entity_id = "ent_wf1"
        gates = [_Gate()]
        fast_paths: list = []

    async def fake_fetch_workflow_definitions(project):
        return [_Workflow()]

    def fake_select_workflow(snap, workflows):
        return workflows[0]

    async def fake_resolve_unmet(wf, project):
        return set()

    async def fake_load_state_for(work_entity_id):
        raise participation.ParticipationStateUnavailable("404 Not Found")

    async def fake_record_dispatched(**kwargs):
        dispatched.append(kwargs.get("gate_name", "?"))

    async def fake_spawn_agent(**kwargs):
        spawned.append(kwargs.get("gate_name", "?"))

    async def fake_fetch_comments(snap):
        return []

    async def fake_harvest(comments):
        return None

    monkeypatch.setattr(participation, "load_state_for", fake_load_state_for)
    monkeypatch.setattr(participation, "record_dispatched", fake_record_dispatched)
    monkeypatch.setattr(anthus, "_spawn_agent", fake_spawn_agent)
    monkeypatch.setattr(anthus, "_fetch_comments", fake_fetch_comments)
    monkeypatch.setattr(anthus, "_harvest_drift_signals", fake_harvest)

    import orchestrator

    monkeypatch.setattr(
        orchestrator, "fetch_workflow_definitions", fake_fetch_workflow_definitions
    )
    monkeypatch.setattr(orchestrator, "select_workflow", fake_select_workflow)
    monkeypatch.setattr(orchestrator, "resolve_unmet_preconditions", fake_resolve_unmet)

    # Ensure no stale in-process state short-circuits the hydration path.
    anthus._gate_states.pop("ent_work_held", None)

    ev = anthus.NeotomaEvent(
        entity_type="issue",
        entity_id="ent_work_held",
        action="created",
        snapshot={"repo": "markmhendrickson/ateles", "github_number": 584},
    )
    asyncio.run(anthus._orchestrate_workflow_for(ev))

    assert dispatched == [], (
        "no participation_record may be written when prior state is unknown; "
        f"got dispatches for {dispatched}"
    )
    assert spawned == [], (
        f"no agent may be spawned when prior state is unknown; got spawns for {spawned}"
    )
