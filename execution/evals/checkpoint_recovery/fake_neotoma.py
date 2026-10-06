"""A stateful, HTTP-level stand-in for the record, for the recovery scenario.

It enforces the server rules the recovery depends on, so the scenario fails for
the reasons production failed rather than for what a mock was told to return:

* a signed (AAuth-admitted) store is judged against the producer's grant, which
  covers entity types and no relationships: any relationship in a signed store,
  or a signed relationship request, is refused (403 capability_denied);
* an idempotency key reused with different content is a 400;
* a checkpoint's identity is resolved from its title, so equal titles are ONE
  entity;
* an observation's provenance records whether the request was signed.

Every relationship request on ANY identity is recorded in ``relationship_requests``
so a scenario can assert none was made.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

CHECKPOINT = "checkpoint_" + "brief"


class FakeNeotoma:
    def __init__(self, scenario: dict, producer_jkt: str):
        self.tenant = scenario["tenant"]
        self.page_size = scenario["page_size"]
        self.producer_jkt = producer_jkt
        self.entities: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, tuple[str, str]] = {}
        self.by_title: dict[str, str] = {}
        self.relationship_requests: list[dict] = []
        self.write_log: list[str] = []
        self.fail_store = False
        self.fail_query = False
        self.fail_query_types: set[str] = set()
        self.fail_correct_ids: set[str] = set()
        self._obs = 0
        for task in scenario["tasks"]:
            self._put(
                task["id"],
                "task",
                {
                    "title": task["title"],
                    "status": task["status"],
                    "assigned_to": "cicada",
                    "action_type": "local_edit",
                    "confidence": 0.3,
                },
                signed=False,
            )
            for legacy_id in task.get("legacy_checkpoints", []):
                self._put(
                    legacy_id,
                    CHECKPOINT,
                    {
                        "title": f"PLAN checkpoint: {task['title']}",
                        "task_entity_id": task["id"],
                        "status": "awaiting_operator",
                    },
                    signed=False,
                )

    # -- state ---------------------------------------------------------------
    def _put(self, entity_id, entity_type, fields, *, signed, agent=None):
        self._obs += 1
        obs_id = f"obs-{self._obs}"
        entity = self.entities.setdefault(
            entity_id,
            {
                "type": entity_type,
                "fields": {},
                "provenance": {},
                "observations": [],
            },
        )
        entity["fields"].update(fields)
        for field in fields:
            entity["provenance"][field] = obs_id
        provenance = (
            {
                "agent_sub": "apis@ateles-swarm",
                "agent_thumbprint": self.producer_jkt,
                "attribution_tier": "software",
            }
            if signed
            else {}
        )
        entity["observations"].append(
            {
                "id": obs_id,
                "fields": dict(fields),
                "user_id": self.tenant,
                "provenance": provenance,
            }
        )
        return entity_id

    def touch_task(self, task_id: str, **fields) -> None:
        """The task changed after a checkpoint was filed (a new revision)."""
        self._put(task_id, "task", fields, signed=False)

    def pending_checkpoints(self, task_id: str) -> list[str]:
        return [
            entity_id
            for entity_id, e in self.entities.items()
            if e["type"] == CHECKPOINT
            and e["fields"].get("task_entity_id") == task_id
            and e["fields"].get("status") == "awaiting_operator"
        ]

    def record(self, entity_id: str) -> dict | None:
        e = self.entities.get(entity_id)
        if e is None:
            return None
        return {
            "entity_id": entity_id,
            "entity_type": e["type"],
            "snapshot": dict(e["fields"]),
            "provenance": dict(e["provenance"]),
            "observation_count": len(e["observations"]),
            "last_observation_at": f"2026-10-06T00:00:{len(e['observations']):02d}Z",
            "user_id": self.tenant,
        }

    # -- transport -----------------------------------------------------------
    @staticmethod
    def _response(status: int, payload: dict, url: str = "https://neotoma.test/x"):
        return httpx.Response(status, json=payload, request=httpx.Request("POST", url))

    def get(self, url, **kwargs):
        path = url.split("neotoma.test", 1)[-1] if "neotoma.test" in url else url
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "entities":
            entity_id = parts[1]
            e = self.entities.get(entity_id)
            if e is None:
                return self._response(404, {"error_code": "NOT_FOUND"}, url)
            if len(parts) == 3 and parts[2] == "observations":
                return self._response(
                    200, {"observations": list(e["observations"])}, url
                )
            return self._response(200, self.record(entity_id), url)
        return self._response(404, {"error_code": "NOT_FOUND"}, url)

    def post(self, url, **kwargs):
        headers = kwargs.get("headers") or {}
        signed = "signature" in headers
        body = (
            json.loads(kwargs["content"])
            if "content" in kwargs
            else kwargs.get("json", {})
        )
        if url.endswith("/create_relationship") or url.endswith(
            "/create_relationships"
        ):
            self.relationship_requests.append({"signed": signed, "body": body})
            if signed:
                return self._response(403, {"error_code": "capability_denied"}, url)
            return self._response(200, {"success": True}, url)
        if url.endswith("/entities/query"):
            return self._query(body, url)
        if url.endswith("/correct"):
            return self._correct(body, url)
        if url.endswith("/store"):
            return self._store(body, signed, url)
        return self._response(404, {"error_code": "NOT_FOUND"}, url)

    def _query(self, body, url):
        entity_type = body["entity_type"]
        if self.fail_query or entity_type in self.fail_query_types:
            return self._response(500, {"error_code": "DB_QUERY_FAILED"}, url)
        filters = body.get("snapshot_filters") or {}
        matching = sorted(
            entity_id
            for entity_id, e in self.entities.items()
            if e["type"] == entity_type
            and all(
                e["fields"].get(field) == spec["value"]
                for field, spec in filters.items()
            )
        )
        start = int(body.get("cursor") or 0)
        page = matching[start : start + self.page_size]
        end = start + self.page_size
        return self._response(
            200,
            {
                "entities": [
                    {
                        "entity_id": i,
                        "entity_type": entity_type,
                        "snapshot": dict(self.entities[i]["fields"]),
                    }
                    for i in page
                ],
                "next_cursor": str(end) if end < len(matching) else None,
            },
            url,
        )

    def _correct(self, body, url):
        entity_id = body["entity_id"]
        if entity_id in self.fail_correct_ids or entity_id not in self.entities:
            return self._response(500, {"error_code": "DB_QUERY_FAILED"}, url)
        self.write_log.append(f"correct {entity_id}.{body['field']}={body['value']}")
        self._put(
            entity_id,
            self.entities[entity_id]["type"],
            {body["field"]: body["value"]},
            signed=False,
        )
        return self._response(200, {"success": True}, url)

    def _store(self, body, signed, url):
        if self.fail_store:
            return self._response(500, {"error_code": "DB_QUERY_FAILED"}, url)
        if signed and body.get("relationships"):
            return self._response(
                403,
                {
                    "error_code": "capability_denied",
                    "message": "relationship outside the producer's grant",
                },
                url,
            )
        entity = dict(body["entities"][0])
        key = body["idempotency_key"]
        digest = json.dumps(body["entities"], sort_keys=True, separators=(",", ":"))
        if key in self.by_key:
            prior_id, prior_digest = self.by_key[key]
            if prior_digest != digest:
                return self._response(
                    400,
                    {
                        "error_code": "ERR_STORE_RESOLUTION_FAILED",
                        "message": "ERR_IDEMPOTENCY_MISMATCH",
                    },
                    url,
                )
            return self._response(
                200, {"replayed": True, "entities": [{"entity_id": prior_id}]}, url
            )
        entity_type = entity.pop("entity_type")
        title = entity["title"]
        entity_id = self.by_title.get(title) or f"ent_cp_{len(self.by_title) + 1}"
        self.by_title[title] = entity_id
        self.by_key[key] = (entity_id, digest)
        self.write_log.append(f"store {entity_id}")
        self._put(entity_id, entity_type, entity, signed=signed)
        return self._response(200, {"entities": [{"entity_id": entity_id}]}, url)
