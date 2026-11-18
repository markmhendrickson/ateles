"""Tests for the shared session-finalize routine (/end convergence)."""

from __future__ import annotations

import pytest
import threading
import time
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lib.daemon_runtime import session_finalize as sf


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {}


@pytest.fixture(autouse=True)
def _client_delegates_to_existing_fakes(monkeypatch):
    class FakeClient:
        def post(self, *args, **kwargs):
            return sf.httpx.post(*args, **kwargs)

        def get(self, *args, **kwargs):
            return sf.httpx.get(*args, **kwargs)

        def close(self):
            pass

    monkeypatch.setattr(sf, "_request_client_factory", FakeClient, raising=False)


# ── pure payload builder ─────────────────────────────────────────────────────


def test_build_payload_new_conversation_with_learning():
    b = sf.build_finalize_payload(
        trigger_text="task X due", outcome_text="did X", handler="apis",
        plan_id="ent_plan", task_id="ent_task", learning="X needs retries",
    )
    types = [e["entity_type"] for e in b["entities"]]
    assert types.count("agent_message") == 2
    assert "conversation" in types
    assert "learning" in types
    roles = {e.get("role") for e in b["entities"] if e["entity_type"] == "agent_message"}
    assert roles == {"user", "assistant"}
    conv_refs = {
        r.get("target_entity_id") for r in b["relationships"]
        if r.get("source_index") == 0 and r["relationship_type"] == "REFERS_TO"
    }
    assert conv_refs == {"ent_task"}
    assert not any(
        r.get("source_index") == 0 and r["relationship_type"] == "PART_OF"
        for r in b["relationships"]
    )
    learning_index = types.index("learning")
    membership = [
        r for r in b["relationships"]
        if r.get("source_index") == learning_index and r["relationship_type"] == "PART_OF"
    ]
    assert [r["target_entity_id"] for r in membership] == ["ent_task"]
    assert b.get("idempotency_key")


def test_build_payload_existing_conversation_no_dup():
    b = sf.build_finalize_payload(
        trigger_text="t", outcome_text="o", handler="formica",
        conversation_id="ent_conv", task_id="ent_task2",
    )
    types = [e["entity_type"] for e in b["entities"]]
    assert "conversation" not in types  # do not recreate the conversation
    assert "learning" not in types       # none supplied
    msg_links = [
        r for r in b["relationships"]
        if r["relationship_type"] == "PART_OF" and r.get("target_entity_id") == "ent_conv"
    ]
    assert len(msg_links) == 2  # both messages link to the existing conversation


# ── I/O contract ─────────────────────────────────────────────────────────────


def test_finalize_session_posts_to_store(monkeypatch):
    calls: list[tuple] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, json, headers))
        return _Resp()

    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", fake_post)
    ok = sf.finalize_session(
        trigger_text="t", outcome_text="o", handler="apis", task_id="ent_task",
    )
    assert ok is True
    assert len(calls) == 1
    url, body, headers = calls[0]
    assert url.endswith("/store")
    assert "entities" in body and "relationships" in body
    assert headers["Authorization"] == "Bearer tok"


def test_finalize_session_fail_open_without_token(monkeypatch):
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "")
    assert sf.finalize_session(trigger_text="t", outcome_text="o", handler="apis") is False


def test_finalize_session_fail_open_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", boom)
    assert sf.finalize_session(trigger_text="t", outcome_text="o", handler="apis") is False


def test_load_end_skill_parses_content(monkeypatch):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"snapshot": {"snapshot": {"content": "# end\nbody"}}}

    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "get", lambda *a, **k: _R())
    assert sf.load_end_skill() == "# end\nbody"


def test_load_end_skill_fail_open(monkeypatch):
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "")
    assert sf.load_end_skill() is None


# ── E1: conversation-per-execution-run ───────────────────────────────────────


def test_build_run_conversation_refers_to_task_and_uses_task_ascent():
    b = sf.build_run_conversation_payload(
        task_id="ent_task", plan_id="ent_plan", agent="cicada", run_key="created-0",
    )
    assert [e["entity_type"] for e in b["entities"]] == ["conversation"]
    refs = {
        r["target_entity_id"] for r in b["relationships"]
        if r["relationship_type"] == "REFERS_TO" and r["source_index"] == 0
    }
    assert refs == {"ent_task"}
    assert not any(r["relationship_type"] == "PART_OF" for r in b["relationships"])
    # run_key in the idempotency key → SSE replay reuses, retry (new key) opens new
    assert b["idempotency_key"] == "run-conv-ent_task-created-0"


def test_build_run_conversation_task_only():
    b = sf.build_run_conversation_payload(task_id="ent_task", agent="cicada", run_key="r1")
    assert len(b["relationships"]) == 1
    assert b["relationships"][0]["target_entity_id"] == "ent_task"
    assert b["relationships"][0]["relationship_type"] == "REFERS_TO"


def test_build_run_session_persists_runtime_identity_and_provenance():
    b = sf.build_run_session_payload(
        task_id="ent_task", plan_id="ent_plan", agent="cicada", run_key="created-0",
    )
    assert [e["entity_type"] for e in b["entities"]] == ["conversation", "agent_session"]
    conversation, session = b["entities"]
    assert conversation["conversation_id"] == session["native_session_id"]
    assert conversation["session_id"] == session["native_session_id"]
    assert session["harness"] == "ateles-swarm"
    assert session["kind"] == "autonomous"
    assert session["trigger_ref"] == "ent_task"
    session_refs = [
        r for r in b["relationships"]
        if r.get("source_index") == 1 and r["relationship_type"] == "REFERS_TO"
    ]
    assert [r["target_entity_id"] for r in session_refs] == ["ent_task"]
    assert "ent_plan" not in {r.get("target_entity_id") for r in b["relationships"]}


def test_build_turn_payload_appends_to_conversation():
    b = sf.build_turn_payload(
        conversation_id="ent_conv", role="user", content="reply", sender_kind="operator",
    )
    assert [e["entity_type"] for e in b["entities"]] == ["agent_message"]
    assert b["entities"][0]["sender_kind"] == "operator"
    rel = b["relationships"][0]
    assert rel["target_entity_id"] == "ent_conv" and rel["relationship_type"] == "PART_OF"


def test_create_run_conversation_returns_id(monkeypatch):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"entities": [{"entity_type": "conversation", "entity_id": "ent_conv99"}]}

    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    class _Get:
        def raise_for_status(self): pass
        def json(self):
            return {"entity_id": "ent_conv99", "entity_type": "conversation",
                    "snapshot": {"conversation_id": "ent_task:created-0",
                                 "session_id": "ent_task:created-0"}}

    monkeypatch.setattr(sf.httpx, "post", lambda *a, **k: _R())
    monkeypatch.setattr(sf.httpx, "get", lambda *a, **k: _Get())
    cid = sf.create_run_conversation(task_id="ent_task", agent="cicada", run_key="created-0")
    assert cid == "ent_conv99"


def test_create_run_conversation_rejects_unreadable_write(monkeypatch):
    class _Post:
        def raise_for_status(self): pass
        def json(self):
            return {"entities": [{"entity_type": "conversation", "entity_id": "ent_conv99"}]}
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", lambda *a, **k: _Post())
    monkeypatch.setattr(sf.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert sf.create_run_conversation(task_id="ent_task", agent="cicada", run_key="created-0") is None


def test_create_run_session_reads_back_both_records_and_task_link(monkeypatch):
    class _Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data
    def fake_get(url, **_kwargs):
        if url.endswith("/entities/ent_conv99"):
            return _Response({"entity_id": "ent_conv99", "entity_type": "conversation",
                              "snapshot": {"conversation_id": "ent_task:created-0",
                                           "session_id": "ent_task:created-0"}})
        if url.endswith("/entities/ent_session99"):
            return _Response({"entity_id": "ent_session99", "entity_type": "agent_session",
                              "snapshot": {"harness": "ateles-swarm",
                                           "native_session_id": "ent_task:created-0"}})
        source_entity_id = url.split("/entities/", 1)[1].split("/", 1)[0]
        targets = {
            "ent_conv99": ["ent_task", "ent_session99"],
            "ent_session99": ["ent_task"],
        }
        return _Response({"relationships": [
            {
                "source_entity_id": source_entity_id,
                "target_entity_id": target_entity_id,
                "relationship_type": "REFERS_TO",
            }
            for target_entity_id in targets[source_entity_id]
        ]})
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", lambda *a, **k: _Response({"entities": [
        {"entity_type": "conversation", "entity_id": "ent_conv99"},
        {"entity_type": "agent_session", "entity_id": "ent_session99"},
    ]}))
    monkeypatch.setattr(sf.httpx, "get", fake_get)
    run = sf.create_run_session(task_id="ent_task", plan_id="ent_plan",
                                agent="cicada", run_key="created-0")
    assert run == sf.RunSession("ent_conv99", "ent_session99", "ent_task:created-0")


def test_update_run_session_status_reads_back_terminal_state(monkeypatch):
    run = sf.RunSession("ent_conv99", "ent_session99", "ent_task:created-0")
    posted = []
    class _Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data
    def fake_post(*_args, **kwargs):
        posted.append(kwargs["json"])
        return _Response({"entities": [{"entity_type": "agent_session",
                                         "entity_id": "ent_session99"}]})
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", fake_post)
    monkeypatch.setattr(sf.httpx, "get", lambda *a, **k: _Response({
        "entity_id": "ent_session99", "entity_type": "agent_session",
        "snapshot": {"harness": "ateles-swarm", "native_session_id": "ent_task:created-0",
                     "status": "completed"}}))
    assert sf.update_run_session_status(run, status="completed") is True
    assert posted[0]["entities"][0]["status"] == "completed"


def test_recover_run_session_finds_exact_existing_run_and_verifies_links(monkeypatch):
    native_session_id = "ent_task:approved-2"

    class _Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    def fake_post(url, **kwargs):
        assert url.endswith("/entities/query")
        entity_type = kwargs["json"]["entity_type"]
        filters = kwargs["json"]["snapshot_filters"]
        if entity_type == "agent_session":
            assert filters == {
                "native_session_id": {"op": "eq", "value": native_session_id}
            }
            return _Response({"entities": [{
                "entity_id": "ent_session99",
                "snapshot": {
                    "harness": "ateles-swarm",
                    "native_session_id": native_session_id,
                    "status": "active",
                },
            }]})
        assert filters == {
            "conversation_id": {"op": "eq", "value": native_session_id}
        }
        return _Response({"entities": [{
            "entity_id": "ent_conv99",
            "snapshot": {
                "conversation_id": native_session_id,
                "session_id": native_session_id,
            },
        }]})

    def fake_get(url, **_kwargs):
        if url.endswith("/entities/ent_conv99"):
            return _Response({
                "entity_id": "ent_conv99",
                "entity_type": "conversation",
                "snapshot": {
                    "conversation_id": native_session_id,
                    "session_id": native_session_id,
                },
            })
        if url.endswith("/entities/ent_session99"):
            return _Response({
                "entity_id": "ent_session99",
                "entity_type": "agent_session",
                "snapshot": {
                    "harness": "ateles-swarm",
                    "native_session_id": native_session_id,
                },
            })
        source_entity_id = url.split("/entities/", 1)[1].split("/", 1)[0]
        targets = {
            "ent_conv99": ["ent_task", "ent_session99"],
            "ent_session99": ["ent_task"],
        }
        return _Response({"relationships": [
            {
                "source_entity_id": source_entity_id,
                "target_entity_id": target,
                "relationship_type": "REFERS_TO",
            }
            for target in targets[source_entity_id]
        ]})

    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", fake_post)
    monkeypatch.setattr(sf.httpx, "get", fake_get)

    assert sf.recover_run_session(
        task_id="ent_task",
        native_session_id=native_session_id,
    ) == sf.RunSession("ent_conv99", "ent_session99", native_session_id)


def test_recover_run_session_rejects_identity_from_another_task(monkeypatch):
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    assert sf.recover_run_session(
        task_id="ent_task",
        native_session_id="ent_other:approved-2",
    ) is None


def test_create_run_conversation_fail_open(monkeypatch):
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "")
    assert sf.create_run_conversation(task_id="ent_task", agent="cicada", run_key="r") is None


def test_append_turn_posts_and_fails_open(monkeypatch):
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", lambda *a, **k: _Resp())
    assert sf.append_turn(conversation_id="ent_conv", role="assistant", content="progress") is True
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "")
    assert sf.append_turn(conversation_id="ent_conv", role="assistant", content="x") is False



def _capture_post_timeout(monkeypatch, **kwargs):
    seen = {}

    def fake_post(url, **kw):
        seen["timeout"] = kw.get("timeout")
        resp = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"ok": True}})()
        return resp

    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "synthetic-token")
    monkeypatch.setattr(sf.httpx, "post", fake_post)
    sf._post_store({"idempotency_key": "k"}, **kwargs)
    return seen["timeout"]


def test_provenance_store_attempt_allows_a_slow_hosted_write(monkeypatch):
    """The store Apis requires before spawning gets 60s per attempt; a fixed 20s refused every build
    while the hosted instance was slow."""
    assert _capture_post_timeout(monkeypatch, retry_transient=True) == 60.0


def test_other_stores_keep_the_short_limit(monkeypatch):
    assert _capture_post_timeout(monkeypatch) == 20.0



def _run_session_fakes(monkeypatch, post):
    """The same readback fakes as the success test above, with a caller-supplied POST."""
    class _Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    def fake_get(url, **_kwargs):
        if url.endswith("/entities/ent_conv99"):
            return _Response({"entity_id": "ent_conv99", "entity_type": "conversation",
                              "snapshot": {"conversation_id": "ent_task:created-0",
                                           "session_id": "ent_task:created-0"}})
        if url.endswith("/entities/ent_session99"):
            return _Response({"entity_id": "ent_session99", "entity_type": "agent_session",
                              "snapshot": {"harness": "ateles-swarm",
                                           "native_session_id": "ent_task:created-0"}})
        source = url.split("/entities/", 1)[1].split("/", 1)[0]
        targets = {"ent_conv99": ["ent_task", "ent_session99"], "ent_session99": ["ent_task"]}
        return _Response({"relationships": [
            {"source_entity_id": source, "target_entity_id": target, "relationship_type": "REFERS_TO"}
            for target in targets[source]]})

    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", post)
    monkeypatch.setattr(sf.httpx, "get", fake_get)
    if hasattr(sf, "_RequestScope"):
        monkeypatch.setattr(sf._RequestScope, "wait", lambda _self, _seconds, _deadline=None: None)
    monkeypatch.setattr("lib.daemon_runtime.neotoma_transient._sleep", lambda _s: None)
    return _Response


def test_a_healthy_but_slow_provenance_write_still_opens_the_run_session(monkeypatch):
    """End to end through create_run_session (what Apis calls before it spawns): a write that
    needs 20 to 60s is rejected by a 20s limit and accepted at 60s, and then passes the
    record and relationship readbacks, so the run session opens and the spawn path is reached."""
    calls = []

    def slow_post(*_a, **kwargs):
        calls.append(kwargs.get("timeout"))
        if kwargs.get("timeout", 0) < 30:  # the server needs about 30s to accept this write
            raise sf.httpx.ReadTimeout("write not accepted within the limit")
        return response({"entities": [
            {"entity_type": "conversation", "entity_id": "ent_conv99"},
            {"entity_type": "agent_session", "entity_id": "ent_session99"}]})

    response = _run_session_fakes(monkeypatch, slow_post)
    run = sf.create_run_session(task_id="ent_task", plan_id="ent_plan", agent="cicada", run_key="created-0")
    assert run == sf.RunSession("ent_conv99", "ent_session99", "ent_task:created-0")
    assert calls[0] == 60.0 and len(calls) == 1  # one attempt, at the provenance limit


def test_the_refusal_stays_when_the_provenance_write_never_succeeds(monkeypatch):
    """The requirement is unchanged: if the store keeps timing out the run session is NOT opened
    (Apis then refuses to spawn). The bound is three attempts."""
    calls = []

    def always_slow(*_a, **kwargs):
        calls.append(kwargs.get("timeout"))
        raise sf.httpx.ReadTimeout("write not accepted within the limit")

    _run_session_fakes(monkeypatch, always_slow)
    run = sf.create_run_session(task_id="ent_task", plan_id="ent_plan", agent="cicada", run_key="created-0")
    assert run is None
    assert calls == [60.0, 60.0, 60.0]



def test_a_peer_that_keeps_making_progress_cannot_hold_the_provenance_write_open(monkeypatch):
    """httpx's timeout is per network operation, so a peer that keeps sending bytes never trips it.
    The provenance write has a wall-clock deadline of its own: it must give up in bounded time,
    start no attempt after the total is spent, and leave no run session (Apis then refuses)."""
    import threading
    import time as _time

    release = threading.Event()
    started = []

    def never_finishing(*_a, **kwargs):
        started.append(kwargs.get("timeout"))
        release.wait(30)  # "progress" without ever completing; ended by the test below
        raise sf.httpx.ReadTimeout("released")

    _run_session_fakes(monkeypatch, never_finishing)
    monkeypatch.setattr(sf, "PROVENANCE_STORE_ATTEMPT_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(sf, "PROVENANCE_STORE_TOTAL_SECONDS", 0.8)
    monkeypatch.setattr(sf, "RUN_SESSION_TOTAL_SECONDS", 0.8)
    t0 = _time.monotonic()
    try:
        run = sf.create_run_session(task_id="ent_task", plan_id="ent_plan", agent="cicada", run_key="created-0")
        elapsed = _time.monotonic() - t0
    finally:
        release.set()
    assert run is None
    assert elapsed < 3.0, f"the wait was not bounded: {elapsed:.1f}s"
    assert 1 <= len(started) <= 3


@contextmanager
def _trickling_server(*, trickle_post: bool):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _trickle(self):
            with self.server.count_lock:
                self.server.calls += 1
            self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            try:
                while True:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if trickle_post:
                self._trickle()
            else:
                self._json({"entities": [
                    {"entity_type": "conversation", "entity_id": "ent_conv99"},
                    {"entity_type": "agent_session", "entity_id": "ent_session99"},
                ]})

        def do_GET(self):
            self._trickle()

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("loopback socket bind is denied in this sandbox")
    server.daemon_threads = True
    server.count_lock = threading.Lock()
    server.calls = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _live_provenance_workers():
    return [t for t in threading.enumerate() if t.name.startswith("provenance-")]


def _wait_for_workers_to_end():
    until = time.monotonic() + 2.0
    while _live_provenance_workers() and time.monotonic() < until:
        time.sleep(0.02)
    assert not _live_provenance_workers()


def test_provenance_store_deadline_workers_are_bounded_and_reclaimed(monkeypatch):
    fake_factory = sf._request_client_factory
    monkeypatch.setattr(sf, "_request_client_factory", sf.httpx.Client)
    monkeypatch.setattr(sf, "PROVENANCE_STORE_ATTEMPT_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(sf, "RUN_SESSION_TOTAL_SECONDS", 0.7)
    callers = []
    results = []
    with _trickling_server(trickle_post=True) as (server, url):
        monkeypatch.setattr(sf, "NEOTOMA_BASE_URL", url)
        for i in range(sf.PROVENANCE_WORKERS):
            caller = threading.Thread(target=lambda n=i: results.append(sf.create_run_session(
                task_id="ent_task", agent="cicada", run_key=f"created-{n}")))
            caller.start()
            callers.append(caller)
        until = time.monotonic() + 0.5
        while server.calls < sf.PROVENANCE_WORKERS and time.monotonic() < until:
            time.sleep(0.01)
        assert server.calls == sf.PROVENANCE_WORKERS
        started = time.monotonic()
        assert sf.create_run_session(task_id="ent_task", agent="cicada", run_key="busy") is None
        assert time.monotonic() - started < 0.2
        assert server.calls == sf.PROVENANCE_WORKERS
        assert len(_live_provenance_workers()) <= sf.PROVENANCE_WORKERS
        for caller in callers:
            caller.join(timeout=2)
        assert all(not caller.is_alive() for caller in callers)
        assert results == [None] * sf.PROVENANCE_WORKERS
    _wait_for_workers_to_end()  # no request is released by this test
    monkeypatch.setattr(sf, "_request_client_factory", fake_factory)
    response = _run_session_fakes(monkeypatch, lambda *_a, **_k: response({"entities": [
        {"entity_type": "conversation", "entity_id": "ent_conv99"},
        {"entity_type": "agent_session", "entity_id": "ent_session99"},
    ]}))
    assert sf.create_run_session(task_id="ent_task", agent="cicada", run_key="created-0") is not None


def test_expired_scopes_close_clients_and_reclaim_all_permits(monkeypatch):
    capacity = 4
    clients = []
    class BlockingClient:
        def __init__(self):
            self.closed = threading.Event()
            clients.append(self)

        def post(self, *_args, **_kwargs):
            self.closed.wait(3)
            raise sf.httpx.ReadTimeout("closed on expiry")

        def close(self):
            self.closed.set()

    monkeypatch.setattr(sf, "_request_client_factory", BlockingClient, raising=False)
    def old_uninterruptible_post(*_args, **_kwargs):
        time.sleep(3)
        raise sf.httpx.ReadTimeout("old request timed out")
    monkeypatch.setattr(sf.httpx, "post", old_uninterruptible_post)
    monkeypatch.setattr("lib.daemon_runtime.neotoma_transient._sleep", lambda _s: None)
    monkeypatch.setattr(sf, "PROVENANCE_STORE_ATTEMPT_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(sf, "PROVENANCE_STORE_TOTAL_SECONDS", 0.4)
    monkeypatch.setattr(sf, "RUN_SESSION_TOTAL_SECONDS", 0.4, raising=False)
    for wave in range(3):
        results = []
        callers = [threading.Thread(target=lambda n=i: results.append(sf.create_run_session(
            task_id="ent_task", agent="cicada", run_key=f"wave-{wave}-{n}")))
                   for i in range(capacity)]
        for caller in callers:
            caller.start()
        until = time.monotonic() + 0.2
        while len(_live_provenance_workers()) < capacity and time.monotonic() < until:
            time.sleep(0.01)
        assert len(_live_provenance_workers()) <= capacity
        assert sf.create_run_session(task_id="ent_task", agent="cicada", run_key="busy") is None
        for caller in callers:
            caller.join(timeout=2)
        assert results == [None] * capacity
        _wait_for_workers_to_end()
    assert all(client.closed.is_set() for client in clients)
