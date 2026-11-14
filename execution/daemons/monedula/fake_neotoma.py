"""In-memory fake Neotoma REST server (POST /correct, GET /entities/<id>)."""

from __future__ import annotations

import json

import httpx

BASE = "https://neotoma.test"


class FakeNeotoma:
    def __init__(self, entities: dict[str, dict] | None = None, *, fail_correct=False,
                 drop_writes=False, fail_get=False, shape="plain", normalise_due=False):
        self.entities = entities or {}
        self.posts: list[dict] = []
        self.fail_correct = fail_correct
        self.drop_writes = drop_writes  # 200 but value not stored -> read-back mismatch
        self.fail_get = fail_get
        self.shape = shape  # plain | entity | nested | unknown
        self.normalise_due = normalise_due  # GET returns due_date with a time suffix

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/correct":
            body = json.loads(request.content)
            self.posts.append(body)
            if self.fail_correct:
                return httpx.Response(500, json={"error": "boom"})
            if not self.drop_writes:
                self.entities.setdefault(body["entity_id"], {})[body["field"]] = body["value"]
            return httpx.Response(200, json={"success": True})
        if request.method == "GET" and path.startswith("/entities/"):
            if self.fail_get:
                return httpx.Response(500, json={"error": "boom"})
            eid = path.rsplit("/", 1)[1]
            fields = self.entities.get(eid)
            snap = {} if fields is None else {"entity_type": "task", **fields}
            if self.normalise_due and snap.get("due_date"):
                snap["due_date"] = snap["due_date"] + "T00:00:00Z"
            if self.shape == "entity":
                return httpx.Response(200, json={"entity": {"snapshot": snap}})
            if self.shape == "nested":
                return httpx.Response(200, json={"snapshot": {"snapshot": snap}})
            if self.shape == "unknown":
                return httpx.Response(200, json={"result": {"data": snap}})
            return httpx.Response(200, json={"snapshot": snap})
        return httpx.Response(404, json={"error": "no route " + path})

    def connection(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler)), BASE

    def writes(self, field: str) -> list[dict]:
        return [p for p in self.posts if p["field"] == field]
