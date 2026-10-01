"""Effect tests for the issue-pipeline GitHub write readiness gate.

The issue pipeline must prove its two required GitHub mutations with their
natural request shapes before launching an agent. A failure is both loud and
durably represented in Neotoma without copying raw responses, prompts, or
credential material into the audit row.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import swarm_dispatch
from swarm_dispatch import (
    DispatchConfig,
    IssuePipelineWriteError,
    SwarmDispatcher,
)
from test_swarm_dispatch import _StubNotifier, _issue_trigger


class _Response:
    def __init__(self, status: int, payload, *, secret_body: str = ""):
        self.status_code = status
        self._payload = payload
        self.content = secret_body.encode() or b"x"
        self.request = httpx.Request("POST", "https://api.github.test/redacted")
        self._response = httpx.Response(
            status,
            request=self.request,
            content=self.content,
        )

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "write rejected",
                request=self.request,
                response=self._response,
            )


def _dispatcher() -> SwarmDispatcher:
    return SwarmDispatcher(
        _StubNotifier(),
        DispatchConfig(
            neotoma_token="test-neotoma-token",
            github_token="test-github-token",
            max_concurrent_issue_pipelines=1,
        ),
    )


def _install_durable_failure_store(monkeypatch, captured: list[dict]) -> None:
    async def store(self, entities, idempotency_key):
        captured.extend(entities)
        return {"entities": [{"entity_id": "ent_failure"}]}

    async def query(self, path, payload):
        assert path == "entities/query"
        return {
            "entities": [
                {
                    "entity_id": "ent_failure",
                    "snapshot": {
                        key: value
                        for key, value in captured[-1].items()
                        if key != "entity_type"
                    },
                }
            ]
        }

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", store)
    monkeypatch.setattr(SwarmDispatcher, "_neotoma_post", query)


def test_marker_403_refuses_agents_and_persists_sanitized_failure(monkeypatch):
    planted_payload = "credential-and-response-material-must-not-survive"
    stored: list[dict] = []
    launched: list[int] = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            return _Response(403, {}, secret_body=planted_payload)

    async def pipeline(self, trigger):
        launched.append(trigger.number)

    monkeypatch.setattr(swarm_dispatch.httpx, "AsyncClient", Client)
    monkeypatch.setattr(SwarmDispatcher, "_run_issue_spec_pipeline", pipeline)
    _install_durable_failure_store(monkeypatch, stored)

    dispatcher = _dispatcher()
    asyncio.run(dispatcher._handle_issue_opened(_issue_trigger()))

    assert launched == []
    assert len(stored) == 1
    assert stored[0]["event_type"] == "github.issue_pipeline_failed"
    assert stored[0]["summary"] == (
        "issue pipeline refused at github_marker_write (HTTP 403)"
    )
    assert planted_payload not in repr(stored)
    assert any("required GitHub write failed" in msg for msg in dispatcher.notifier.sent)


def test_readiness_uses_real_comment_post_and_issue_body_patch(monkeypatch):
    calls: list[tuple[str, str, object]] = []
    launched: list[int] = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, **kwargs):
            calls.append(("POST", url, json))
            return _Response(201, {})

        async def get(self, url, **kwargs):
            calls.append(("GET", url, None))
            if url.endswith("/comments"):
                return _Response(200, [])
            return _Response(200, {"body": "current issue body"})

        async def patch(self, url, json=None, **kwargs):
            calls.append(("PATCH", url, json))
            return _Response(200, {})

    async def pipeline(self, trigger):
        launched.append(trigger.number)

    monkeypatch.setattr(swarm_dispatch.httpx, "AsyncClient", Client)
    monkeypatch.setattr(SwarmDispatcher, "_run_issue_spec_pipeline", pipeline)

    asyncio.run(_dispatcher()._handle_issue_opened(_issue_trigger()))

    assert launched == [_issue_trigger().number]
    assert calls[0][0] == "POST" and calls[0][1].endswith("/comments")
    assert ("PATCH", calls[2][1], {"body": "current issue body"}) == calls[2]


def test_issue_body_patch_403_is_a_bounded_write_error(monkeypatch):
    planted_payload = "raw-github-error-body"

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            return _Response(201, {})

        async def get(self, url, **kwargs):
            return _Response(200, {"body": "current body"})

        async def patch(self, url, **kwargs):
            return _Response(403, {}, secret_body=planted_payload)

    monkeypatch.setattr(swarm_dispatch.httpx, "AsyncClient", Client)

    with pytest.raises(IssuePipelineWriteError) as raised:
        asyncio.run(
            _dispatcher()._mark_pipeline_inflight(
                _issue_trigger(), stage="inflight"
            )
        )

    assert raised.value.stage == "github_issue_body_write"
    assert raised.value.status_code == 403
    assert planted_payload not in str(raised.value)


def test_issue_body_patch_403_refuses_agents_and_records_failure(monkeypatch):
    stored: list[dict] = []
    launched: list[int] = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            return _Response(201, {})

        async def get(self, url, **kwargs):
            return _Response(200, {"body": "current body"})

        async def patch(self, url, **kwargs):
            return _Response(403, {})

    async def pipeline(self, trigger):
        launched.append(trigger.number)

    monkeypatch.setattr(swarm_dispatch.httpx, "AsyncClient", Client)
    monkeypatch.setattr(SwarmDispatcher, "_run_issue_spec_pipeline", pipeline)
    _install_durable_failure_store(monkeypatch, stored)

    asyncio.run(_dispatcher()._handle_issue_opened(_issue_trigger()))

    assert launched == []
    assert stored[0]["summary"] == (
        "issue pipeline refused at github_issue_body_write (HTTP 403)"
    )
