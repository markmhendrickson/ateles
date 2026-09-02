"""
fake_neotoma.py — an in-memory stand-in for the Neotoma REST surface, for tests.

WHY A SERVER FAKE RATHER THAN A MOCKED METHOD
---------------------------------------------
PR #666's unit tests passed while the bug was live in production. They asserted
on in-memory state, so the two writers each looked correct in isolation and the
defect — one writer's save clobbering the other's records — lived entirely in
what reached storage. It was only caught by replaying a real 344-event trace with
both writers interleaved across restarts.

So this fake implements the STORAGE SEMANTICS, not a canned response:

  * `/store` resolves identity server-side on the schema's canonical field
    (`ledger_key`), so two writers converge on one row exactly as prod does. A
    fake that always created a new row would hide a singleton bug; one that
    always matched would hide the opposite.
  * `/correct` writes one field and leaves the others alone — the property that
    makes the clobbering bug impossible now, so a test can actually assert it.
  * State survives "restarts", because the fake outlives the client objects that
    talk to it. A restart in these tests is a NEW store and a NEW ledger against
    the SAME fake, which is the only arrangement that can catch state that looks
    persisted and is not.

Failure injection is first-class: `fail_reads` / `fail_writes` toggle transport
errors so the fail-closed read path and fail-open write path are exercised
through the real `httpx` call sites rather than by patching them out.
"""

from __future__ import annotations

import json
from typing import Any

ENTITY_TYPE = "apis_unroutable_ledger"

# The field Neotoma resolves identity on, per the registered schema's
# canonical_name_fields. Named here so the fake's identity rule and the real
# schema cannot drift apart silently.
IDENTITY_FIELD = "ledger_key"


class _Resp:
    """Minimal httpx.Response stand-in."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeNeotoma:
    """An in-memory Neotoma with real identity-resolution and per-field writes."""

    def __init__(self) -> None:
        # entity_id -> snapshot dict
        self.rows: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self.fail_reads = False
        self.fail_writes = False
        # Every request, for assertions about idempotency keys and call counts.
        self.calls: list[tuple[str, dict]] = []
        self.idempotency_keys: list[str] = []

    # ── identity ───────────────────────────────────────────────────────────

    def _find(self, key: str) -> str | None:
        for entity_id, snap in self.rows.items():
            if snap.get(IDENTITY_FIELD) == key:
                return entity_id
        return None

    def row_for(self, key: str) -> dict[str, Any]:
        """The snapshot for a ledger key, as storage holds it."""
        entity_id = self._find(key)
        return dict(self.rows[entity_id]) if entity_id else {}

    # ── transport ──────────────────────────────────────────────────────────

    def post(self, url: str, headers=None, json=None, timeout=None):  # noqa: A002
        payload = json or {}
        path = url.rsplit("/", 1)[-1]
        self.calls.append((path, payload))

        if path == "query":
            if self.fail_reads:
                raise RuntimeError("simulated Neotoma read failure")
            return self._query(payload)

        if self.fail_writes:
            raise RuntimeError("simulated Neotoma write failure")
        if payload.get("idempotency_key"):
            self.idempotency_keys.append(payload["idempotency_key"])
        if path == "store":
            return self._store(payload)
        if path == "correct":
            return self._correct(payload)
        raise AssertionError(f"unexpected path {path!r}")

    def get(self, url: str, headers=None, timeout=None):
        if self.fail_reads:
            raise RuntimeError("simulated Neotoma read failure")
        entity_id = url.rsplit("/", 1)[-1]
        snap = self.rows.get(entity_id)
        if snap is None:
            return _Resp({"error": "not found"}, status=404)
        return _Resp({"entity_id": entity_id, "snapshot": dict(snap)})

    # ── routes ─────────────────────────────────────────────────────────────

    def _query(self, payload: dict) -> _Resp:
        rows = [
            {"entity_id": eid, "snapshot": dict(snap)}
            for eid, snap in self.rows.items()
            if snap.get("entity_type", ENTITY_TYPE) == payload.get("entity_type")
            or payload.get("entity_type") == ENTITY_TYPE
        ]
        return _Resp({"entities": rows, "total": len(rows)})

    def _store(self, payload: dict) -> _Resp:
        out = []
        for ent in payload.get("entities", []):
            key = ent.get(IDENTITY_FIELD)
            entity_id = self._find(key) if key else None
            action = "matched_existing"
            if entity_id is None:
                entity_id = f"ent_fake{self._next_id:06d}"
                self._next_id += 1
                self.rows[entity_id] = {}
                action = "created"
            # Merge the supplied fields onto the row; untouched fields survive.
            # This is what makes a per-field write non-destructive, and it is
            # the behaviour the disk version could not provide.
            for k, v in ent.items():
                if k == "entity_type":
                    continue
                self.rows[entity_id][k] = v
            out.append({"entity_id": entity_id, "action": action})
        return _Resp({"success": True, "entities": out})

    def _correct(self, payload: dict) -> _Resp:
        entity_id = payload.get("entity_id")
        if entity_id not in self.rows:
            return _Resp({"error": "no such entity"}, status=404)
        self.rows[entity_id][payload["field"]] = payload["value"]
        return _Resp({"success": True, "entity_id": entity_id})
