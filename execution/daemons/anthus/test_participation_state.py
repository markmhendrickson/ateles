"""
Regression tests for ateles#584: Anthus participation state could never load.

Three distinct defects, one per section below:

1. `load_state_for` POSTed to `/retrieve_entities`, which does not exist on the
   prod HTTP surface (404). The entity-read route is `POST /entities/query`.

2. That 404 was swallowed as a WARNING and `load_state_for` returned `{}` —
   indistinguishable from "this work entity has no prior gates". The caller
   therefore hydrated nothing and dispatched every gate as though it had never
   run, risking re-dispatch of completed work, while Anthus looked healthy.

3. `orchestrator.resolve_unmet_preconditions` carried the same dead route, and
   fails CLOSED into `unmet` — so the 404 silently marked every
   precondition-gated gate unmet and skipped it indefinitely.

Also covers acceptance criterion 2 of #584 (signed at the pm gate): a
gate already `satisfied` in loaded/seeded participation state must not be
redispatched — see `test_known_satisfied_gate_is_not_redispatched`.

Run with: pytest execution/daemons/anthus/test_participation_state.py -v
"""

from __future__ import annotations

import asyncio

import anthus
import httpx
import orchestrator
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
                    # The schema declares the SINGULAR `artifact_ref`. This
                    # fixture previously used `artifact_refs`, which is not a
                    # declared field and therefore can never appear in a real
                    # snapshot — it encoded the ateles#682 write bug as the
                    # expected shape.
                    "artifact_ref": "ent_artifact1",
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


def test_known_satisfied_gate_is_not_redispatched(monkeypatch):
    """Acceptance criterion 2 of ateles#584 (the pm gate, signed at #584):

    a seeded/persisted state is honored — a gate already `satisfied` in the
    loaded participation state must NOT be redispatched, and the orchestrator
    must move on to computing readiness for the next gate instead.

    This is the positive counterpart to
    `test_dispatch_is_held_when_participation_state_cannot_be_read`: there the
    read fails and nothing may dispatch; here the read succeeds and returns a
    gate that already ran, so THAT gate specifically must not be redispatched
    or re-spawned. Reuses the same harness, with `fake_load_state_for`
    returning a `satisfied` record instead of raising, and builds the
    workflow from the real `orchestrator.WorkflowDefinition`/`Gate`
    dataclasses (as `_precondition_workflow()` does below) rather than a
    stub, so a field rename can't leave this passing against a shape that no
    longer exists.
    """
    dispatched: list[str] = []
    spawned: list[str] = []

    workflow = orchestrator.WorkflowDefinition(
        entity_id="ent_wf1",
        project="ateles",
        workflow_type="feature",
        description="satisfied-gate fixture",
        gates=[
            orchestrator.Gate(
                phase=1,
                gate_name="pm",
                owner_agent="pavo",
                parallel_group=None,
                join_gate=None,
                required=True,
                precondition=None,
            )
        ],
        fast_paths=[],
        legal_required=False,
    )

    async def fake_fetch_workflow_definitions(project):
        return [workflow]

    def fake_select_workflow(snap, workflows):
        return workflows[0]

    async def fake_resolve_unmet(wf, project):
        return set()

    async def fake_load_state_for(work_entity_id):
        # A successful read, returning a gate that already ran to completion.
        return {
            "pm": {
                "status": "satisfied",
                "dispatched_at": "2026-08-30T10:00:00+00:00",
                "satisfied_at": "2026-08-30T11:00:00+00:00",
                "artifact_refs": ["ent_artifact1"],
            }
        }

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

    monkeypatch.setattr(
        orchestrator, "fetch_workflow_definitions", fake_fetch_workflow_definitions
    )
    monkeypatch.setattr(orchestrator, "select_workflow", fake_select_workflow)
    monkeypatch.setattr(orchestrator, "resolve_unmet_preconditions", fake_resolve_unmet)

    # Ensure no stale in-process state short-circuits the hydration path, and
    # that this test's seeded state can't leak from/into the held-dispatch test.
    anthus._gate_states.pop("ent_work_satisfied", None)

    ev = anthus.NeotomaEvent(
        entity_type="issue",
        entity_id="ent_work_satisfied",
        action="created",
        snapshot={"repo": "markmhendrickson/ateles", "github_number": 584},
    )
    asyncio.run(anthus._orchestrate_workflow_for(ev))

    assert dispatched == [], (
        "a gate already satisfied in persisted state must not be re-recorded "
        f"as dispatched; got dispatches for {dispatched}"
    )
    assert spawned == [], (
        "a gate already satisfied in persisted state must not spawn its agent "
        f"again; got spawns for {spawned}"
    )
    assert anthus._gate_states["ent_work_satisfied"]["pm"].status == "satisfied", (
        "the hydrated satisfied status must be preserved in in-memory state, "
        "not reset to pending"
    )


# ── Defect 3: the precondition read carried the same dead route ───────────────


def _precondition_workflow() -> orchestrator.WorkflowDefinition:
    """Two gates: `pm` declares no precondition, `release` declares one.

    Built from the real `orchestrator` dataclasses rather than stubs, so a field
    rename cannot let these tests keep passing against a shape that no longer
    exists.
    """
    return orchestrator.WorkflowDefinition(
        entity_id="ent_wf_precond",
        project="ateles",
        workflow_type="feature",
        description="precondition fixture",
        gates=[
            orchestrator.Gate(
                phase=1,
                gate_name="pm",
                owner_agent="pavo",
                parallel_group=None,
                join_gate=None,
                required=True,
                precondition=None,
            ),
            orchestrator.Gate(
                phase=5,
                gate_name="release",
                owner_agent="struthio",
                parallel_group=None,
                join_gate=None,
                required=True,
                precondition={
                    "entity_type": "release_criteria",
                    "scope_field": "project",
                },
            ),
        ],
        fast_paths=[],
        legal_required=False,
    )


def test_resolve_unmet_preconditions_uses_entities_query(monkeypatch):
    """The precondition read must go to POST /entities/query.

    On origin/main this fails: the call goes to /retrieve_entities, the fake
    (like prod) 404s it, the `except` fails closed, and `release` lands in
    `unmet` — so a gate whose precondition IS satisfied gets skipped forever.

    Both assertions are load-bearing. `"release" not in unmet` is what a route
    regression breaks; the `paths` assertion guards against that check passing
    vacuously, since a missing bearer returns an empty set before any request
    is issued.
    """
    paths: list[str] = []
    payload = {"entities": [{"snapshot": {"project": "ateles"}}], "total": 1}

    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(orchestrator, "NEOTOMA_BASE_URL", "https://neotoma.example")
    monkeypatch.setattr(
        orchestrator.httpx,
        "AsyncClient",
        lambda **kw: _RecordingClient(paths, payload),
    )

    unmet = asyncio.run(
        orchestrator.resolve_unmet_preconditions(_precondition_workflow(), "ateles")
    )

    assert "release" not in unmet, (
        "a gate whose precondition is satisfied must not be marked unmet"
    )
    assert "pm" not in unmet, (
        "a gate that declares no precondition must never be skipped for one"
    )
    assert paths == ["https://neotoma.example/entities/query"], (
        f"precondition read must POST to /entities/query, got {paths}"
    )


def test_resolve_unmet_preconditions_fails_closed_when_read_fails(monkeypatch):
    """A failed precondition read must yield `unmet`, not an empty set.

    This asymmetry with `load_state_for` is deliberate and worth pinning:
    `participation.load_state_for` now fails LOUD (raises), but this path fails
    CLOSED. A reader generalising the new fail-loud style into fail-open here
    would dispatch release gates whose criteria were never checked.
    """

    class _AlwaysFails:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, **kwargs):
            return _FakeResponse(404)

    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(orchestrator, "NEOTOMA_BASE_URL", "https://neotoma.example")
    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", lambda **kw: _AlwaysFails())

    unmet = asyncio.run(
        orchestrator.resolve_unmet_preconditions(_precondition_workflow(), "ateles")
    )

    assert unmet == {"release"}


def test_resolve_unmet_preconditions_unmet_when_project_does_not_match(monkeypatch):
    """A healthy read that matches nothing must also yield `unmet`.

    Without this, a dead route and a live route with no matching entity are
    indistinguishable — both produce `unmet` — so the two tests above could not
    tell them apart. This one pins the `scope_field` comparison and its
    case normalisation rather than the transport.
    """
    paths: list[str] = []
    payload = {"entities": [{"snapshot": {"project": "neotoma"}}], "total": 1}

    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(orchestrator, "NEOTOMA_BASE_URL", "https://neotoma.example")
    monkeypatch.setattr(
        orchestrator.httpx,
        "AsyncClient",
        lambda **kw: _RecordingClient(paths, payload),
    )

    unmet = asyncio.run(
        orchestrator.resolve_unmet_preconditions(_precondition_workflow(), "ateles")
    )

    assert paths == ["https://neotoma.example/entities/query"]
    assert unmet == {"release"}
