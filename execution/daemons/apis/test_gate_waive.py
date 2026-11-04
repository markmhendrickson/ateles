"""Tests for gate_waive.py's IssueGateStore — the Neotoma-backed read/write/verify
path behind the dispatcher-side gate waive (ateles#285).

test_swarm_dispatch.py exercises the dispatcher's use of IssueGateStore through
`_FakeGateStore`, a hand-rolled stand-in that reimplements the waive/verify
loop rather than calling the real store. These tests drive `IssueGateStore`
itself — `load()` and `_post()` — against a mocked httpx.AsyncClient, so the
real Neotoma request/response handling (entity matching, gate_status string
vs. dict round-trip, missing-token skip, `waive()`'s two-write + re-read
sequence) is actually covered.
"""

import asyncio

import httpx

from gate_waive import IssueGateStore

PRE_IMPL_GATES = ("pm", "ux", "arch")

_BASE_URL = "https://neotoma.example.com"
_TOKEN = "test-bearer-token"


class _FakeNeotomaClient:
    """httpx.AsyncClient stub for the Neotoma REST surface IssueGateStore uses.

    ``query_responses`` is a queue of ``entities/query`` response payloads
    consumed in order, so a test can script the pre-write read and the
    post-write verification re-read independently. ``correct_calls`` records
    every ``correct`` POST body for assertion.
    """

    def __init__(self, query_responses):
        self._query_responses = list(query_responses)
        self.correct_calls: list[dict] = []
        self.posted_paths: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, json=None, headers=None, **kwargs):
        self.posted_paths.append(url)
        if url.endswith("/entities/query"):
            payload = (
                self._query_responses.pop(0)
                if self._query_responses
                else {"entities": []}
            )
            return _FakeResp(payload)
        if url.endswith("/correct"):
            self.correct_calls.append(json or {})
            return _FakeResp({})
        raise AssertionError(f"unexpected POST to {url}")


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    @property
    def content(self):
        return b"1"

    def json(self):
        return self._payload


def _entity(entity_id, repo, issue_number, gate_status, owner_history=None):
    return {
        "entity_id": entity_id,
        "snapshot": {
            "repo": repo,
            "issue_number": issue_number,
            "gate_status": gate_status,
            "owner_history": owner_history or [],
        },
    }


# ── load() ───────────────────────────────────────────────────────────────


def test_load_finds_matching_entity_by_repo_and_issue_number(monkeypatch):
    client = _FakeNeotomaClient(
        query_responses=[
            {
                "entities": [
                    _entity("ent_1", "owner/other", 1, {"pm": "signed_off"}),
                    _entity(
                        "ent_2",
                        "owner/repo",
                        42,
                        {"pm": "signed_off", "ux": "pending"},
                    ),
                ]
            }
        ]
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)

    store = IssueGateStore(_BASE_URL, _TOKEN)
    state = asyncio.run(store.load("owner/repo", 42))

    assert state.found
    assert state.entity_id == "ent_2"
    assert state.gate_status == {"pm": "signed_off", "ux": "pending"}
    assert client.posted_paths == [f"{_BASE_URL}/entities/query"], (
        "load() must read via POST /entities/query, not /retrieve_entities"
    )


def test_load_matches_via_repository_and_github_number_aliases(monkeypatch):
    """The prod entity carries repo/repository + issue_number/github_number aliases."""
    client = _FakeNeotomaClient(
        query_responses=[
            {
                "entities": [
                    {
                        "entity_id": "ent_9",
                        "snapshot": {
                            "repository": "owner/repo",
                            "github_number": "42",
                            "gate_status": {"pm": "pending"},
                        },
                    }
                ]
            }
        ]
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)

    store = IssueGateStore(_BASE_URL, _TOKEN)
    state = asyncio.run(store.load("owner/repo", 42))

    assert state.found
    assert state.entity_id == "ent_9"


def test_load_parses_json_string_gate_status_and_marks_representation(monkeypatch):
    client = _FakeNeotomaClient(
        query_responses=[
            {
                "entities": [
                    _entity(
                        "ent_3",
                        "owner/repo",
                        42,
                        '{"pm": "signed_off", "arch": "pending"}',
                    )
                ]
            }
        ]
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)

    store = IssueGateStore(_BASE_URL, _TOKEN)
    state = asyncio.run(store.load("owner/repo", 42))

    assert state.found
    assert state.gate_status == {"pm": "signed_off", "arch": "pending"}
    assert state.gate_status_was_string, (
        "a JSON-string gate_status must be flagged so the write-back "
        "preserves the stored representation"
    )


def test_load_returns_not_found_when_no_entity_matches(monkeypatch):
    client = _FakeNeotomaClient(
        query_responses=[
            {"entities": [_entity("ent_1", "owner/other-repo", 1, {})]}
        ]
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)

    store = IssueGateStore(_BASE_URL, _TOKEN)
    state = asyncio.run(store.load("owner/repo", 42))

    assert not state.found


def test_post_skips_and_returns_none_without_token(monkeypatch):
    """No bearer token configured → _post short-circuits, never calls httpx."""
    calls = []

    class _ExplodingClient:
        def __init__(self, **kw):
            calls.append("constructed")

        async def __aenter__(self):
            raise AssertionError("httpx.AsyncClient must not be entered without a token")

    monkeypatch.setattr(httpx, "AsyncClient", _ExplodingClient)

    store = IssueGateStore(_BASE_URL, "")
    result = asyncio.run(store._post("entities/query", {}))

    assert result is None
    assert not calls, "AsyncClient must not even be constructed when token is empty"


# ── waive() — the full read → write → re-read verify loop ─────────────────


def test_waive_writes_gate_status_and_owner_history_then_verifies(monkeypatch):
    pre_write_entity = _entity(
        "ent_5", "owner/repo", 42, {"pm": "pending", "ux": "pending", "arch": "pending"}
    )
    post_write_entity = _entity(
        "ent_5",
        "owner/repo",
        42,
        {"pm": "waived", "ux": "waived", "arch": "waived"},
    )
    client = _FakeNeotomaClient(
        query_responses=[
            {"entities": [pre_write_entity]},   # initial load()
            {"entities": [post_write_entity]},  # post-write verification re-read
        ]
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)

    store = IssueGateStore(_BASE_URL, _TOKEN)
    outcome = asyncio.run(store.waive("owner/repo", 42, PRE_IMPL_GATES))

    assert outcome.entity_found
    assert outcome.verified
    assert not outcome.failed
    assert sorted(outcome.waived) == ["arch", "pm", "ux"]

    # Exactly two `correct` calls: gate_status, then owner_history.
    correct_fields = [c["field"] for c in client.correct_calls]
    assert correct_fields == ["gate_status", "owner_history"]
    gate_status_call = client.correct_calls[0]
    assert gate_status_call["entity_id"] == "ent_5"
    assert gate_status_call["value"] == {
        "pm": "waived",
        "ux": "waived",
        "arch": "waived",
    }


def test_waive_reports_verification_failure_when_write_did_not_persist(monkeypatch):
    """If the re-read still shows a gate unsigned, outcome.failed must say so."""
    pre_write_entity = _entity("ent_6", "owner/repo", 42, {"pm": "pending"})
    # Simulate a write that silently did not persist: re-read shows unchanged state.
    unchanged_entity = _entity("ent_6", "owner/repo", 42, {"pm": "pending"})
    client = _FakeNeotomaClient(
        query_responses=[
            {"entities": [pre_write_entity]},
            {"entities": [unchanged_entity]},
        ]
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)

    store = IssueGateStore(_BASE_URL, _TOKEN)
    outcome = asyncio.run(store.waive("owner/repo", 42, ("pm",)))

    assert outcome.entity_found
    assert not outcome.verified
    assert outcome.failed == ["pm"], (
        "an unpersisted write must surface as a verification failure, never "
        "be reported as a successful waive (ateles#285 core bug)"
    )


def test_waive_on_missing_entity_reports_not_found_without_writing(monkeypatch):
    client = _FakeNeotomaClient(query_responses=[{"entities": []}])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)

    store = IssueGateStore(_BASE_URL, _TOKEN)
    outcome = asyncio.run(store.waive("owner/repo", 999, PRE_IMPL_GATES))

    assert not outcome.entity_found
    assert not outcome.waived
    assert not client.correct_calls, "must never write when no entity was found"


def test_waive_preserves_json_string_representation_on_write_back(monkeypatch):
    """A gate_status stored as a JSON string must be written back as a string."""
    pre_write_entity = _entity(
        "ent_7", "owner/repo", 42, '{"pm": "pending"}'
    )
    post_write_entity = _entity(
        "ent_7", "owner/repo", 42, '{"pm": "waived"}'
    )
    client = _FakeNeotomaClient(
        query_responses=[
            {"entities": [pre_write_entity]},
            {"entities": [post_write_entity]},
        ]
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)

    store = IssueGateStore(_BASE_URL, _TOKEN)
    outcome = asyncio.run(store.waive("owner/repo", 42, ("pm",)))

    assert outcome.verified
    gate_status_call = client.correct_calls[0]
    assert isinstance(gate_status_call["value"], str), (
        "gate_status must round-trip as a JSON string when that's how it "
        "was stored, not silently switch representation"
    )


def test_waive_preserves_prior_owner_history_entries(monkeypatch):
    prior_history = [{"gate": "design", "action": "signed_off", "actor": "waxwing"}]
    pre_write_entity = _entity(
        "ent_8", "owner/repo", 42, {"pm": "pending"}, owner_history=prior_history
    )
    post_write_entity = _entity(
        "ent_8", "owner/repo", 42, {"pm": "waived"}, owner_history=prior_history
    )
    client = _FakeNeotomaClient(
        query_responses=[
            {"entities": [pre_write_entity]},
            {"entities": [post_write_entity]},
        ]
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)

    store = IssueGateStore(_BASE_URL, _TOKEN)
    asyncio.run(store.waive("owner/repo", 42, ("pm",)))

    owner_history_call = next(
        c for c in client.correct_calls if c["field"] == "owner_history"
    )
    assert prior_history[0] in owner_history_call["value"], (
        "the prior owner_history entry must be preserved, not overwritten"
    )
    assert len(owner_history_call["value"]) == 2, (
        "expected the prior entry plus exactly one new waive entry"
    )
