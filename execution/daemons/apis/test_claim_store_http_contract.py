"""HTTP contract for apis._build_claim_store → Neotoma /store + /entities/query.

Unit/daemon suites inject FakeNeotoma or monkeypatch `_claims`, so a wrong
query/filter body can fail-closed all dispatch while CI stays green. This
test drives the live adapter through httpx.post and locks the prod-accepted
snapshot_filters shape (`{op, value}` per gate_waive.py).

Eval / QA id companion: apis_claim_lease_fail_closed_and_release (see
test_apis_claim_lease_eval.py). Run:

  pytest execution/daemons/apis/test_claim_store_http_contract.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from lib.daemon_runtime.task_claim import CLAIM_HARNESS  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status={self.status_code}",
                request=httpx.Request("POST", "https://neotoma.test/"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict:
        return self._payload


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _claim_entity(*, holder: str, task_id: str = "ent_task") -> dict:
    """Holder/task_id only in raw_fragments — exercises the live merge path."""
    return {
        "entity_id": "ent_claim_1",
        "snapshot": {
            "harness": CLAIM_HARNESS,
            "native_session_id": f"task:{task_id}",
            "status": "running",
            "last_activity_at": _iso_now(),
            "kind": "claim",
            "title": f"claim {task_id}",
        },
        "raw_fragments": {
            "holder": holder,
            "task_id": task_id,
        },
    }


def test_build_claim_store_posts_store_and_queries_claim_by_composite_key(monkeypatch):
    """_build_claim_store must POST /store + composite-key /entities/query.

    Locks Authorization, idempotency_key, entity fields, and the
    `{op,value}` snapshot_filters shape used by gate_waive (not bare strings).
    """
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.test")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "tok-test")

    calls: list[dict[str, Any]] = []
    # Scripted order for runner-A acquire: pre-read empty → store → verify
    # with holder only in raw_fragments. Then runner-B pre-read sees A live.
    scripted = [
        {"entities": []},
        {"entities": [{"entity_id": "ent_claim_1", "action": "created"}]},
        {"entities": [_claim_entity(holder="runner-A")]},
        {"entities": [_claim_entity(holder="runner-A")]},
    ]

    def fake_post(url, *, headers=None, json=None, timeout=None):  # noqa: ANN001
        calls.append({"url": url, "headers": dict(headers or {}), "json": json})
        i = min(len(calls) - 1, len(scripted) - 1)
        return _FakeResponse(scripted[i])

    monkeypatch.setattr(httpx, "post", fake_post)

    store = apis._build_claim_store()
    assert store is not None, "token + base URL must yield a ClaimStore"

    a = store.acquire("ent_task", "runner-A")
    assert a.held is True, f"verify-read raw_fragments holder must win: {a}"
    assert a.reason == "acquired"

    b = store.acquire("ent_task", "runner-B")
    assert b.held is False
    assert b.reason == "held_by_other"
    assert b.holder == "runner-A"

    # ── request-shape hard locks ───────────────────────────────────────────
    for call in calls:
        assert call["headers"].get("Authorization") == "Bearer tok-test"

    store_calls = [c for c in calls if c["url"] == "https://neotoma.test/store"]
    assert store_calls, "acquire must POST /store"
    body = store_calls[0]["json"]
    assert body["idempotency_key"] == "claim-ent_task-runner-A"
    ent0 = body["entities"][0]
    assert ent0["entity_type"] == "agent_session"
    assert ent0["harness"] == CLAIM_HARNESS
    assert ent0["native_session_id"] == "task:ent_task"
    assert ent0["holder"] == "runner-A"

    query_calls = [
        c for c in calls if c["url"] == "https://neotoma.test/entities/query"
    ]
    assert len(query_calls) >= 2, "pre-read + verify-read (and B pre-read)"
    for q in query_calls:
        payload = q["json"]
        assert payload["entity_type"] == "agent_session"
        assert payload["limit"] == 1
        filters = payload.get("snapshot_filters") or {}
        assert "harness" in filters and "native_session_id" in filters
        # Prod-accepted shape (gate_waive.py / test_gate_store_lookup.py) —
        # do not soften to bare strings if this goes red; fix the adapter.
        assert filters["harness"] == {"op": "eq", "value": CLAIM_HARNESS}
        assert filters["native_session_id"] == {
            "op": "eq",
            "value": "task:ent_task",
        }
