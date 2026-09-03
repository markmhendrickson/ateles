"""
unroutable_store.py — the Apis unroutable-ledger persistence backend, in Neotoma.

WHY THIS MODULE EXISTS
----------------------
`unroutable_ledger.py` holds the DECISION logic for no-owner escalations (dedup,
bounded re-assertion, aggregation). This module holds only its STORAGE, and it
was split out because the disk file the ledger used to write had already failed
in production — twice, in the same small file:

  1. `apis.py` and `skill_runner.py` each constructed their own
     `UnroutableLedger()` against one path. Each loaded once behind a `_loaded`
     latch and each `save()` serialized its whole in-memory view, so every write
     clobbered the other writer's records. Measured in prod: 2 of 4 role records
     and 1 of 2 unreadable records lost in ~11 minutes. One of the dropped
     records tracked that very bug.
  2. The fix — a shared instance plus merge-on-write — could not express a
     DELETE, so `clear_unreadable` never persisted and needed per-field
     tombstones on top.

Both are coordination bugs, and both exist only because durable state was living
on a filesystem with no concurrency primitives. Neotoma has append-only
observation history and server-side identity resolution, so neither bug has a
place to live here: two writers appending observations to one entity is the
normal case, not a race, and a delete is just the next observation.

THE ENTITY SHAPE, AND WHY IT IS ONE ROW
---------------------------------------
One `apis_unroutable_ledger` entity, keyed by `ledger_key` (a singleton), NOT one
entity per unroutable task.

Per-task entities are the more Neotoma-shaped choice in the abstract — each
record gets its own history and there is no hot row — and it was the first design
considered. It loses on the actual access pattern:

  * The dedup question is a WHOLE-SET membership test ("have I paged about this
    task before?"). Answering it per-task still needs the whole set present,
    because a MISS is only knowable after reading everything. So the per-entity
    shape does not become N cheap point-reads; it becomes one `/entities/query`
    scan that grows without bound as the backlog does.
  * This is consulted on `task.created` — a hot path. A point GET on one row
    measured ~0.5s against prod; a filtered scan is strictly more work for
    strictly less certainty (paging, `total` vs `limit`, deleted rows).
  * Escalation state is small and bounded in practice (tens of tasks), and the
    per-field history that matters — "when did this task last escalate" — is
    already carried inside the record.

The single row is cached in memory after first load and re-read only when it is
stale, so the steady-state cost of a dispatch is zero Neotoma reads.

THE FAILURE MODE IS THE WHOLE POINT
-----------------------------------
An unreadable ledger must NEVER be treated as an empty one. That is not a
detail — it is the defect this ledger exists to prevent, wearing a disguise. If
a failed read returned "no record", every unroutable task would look new and
escalate again, reproducing the 131-page flood the ledger cut to 4.

So `load()` raises `LedgerUnavailable` rather than returning `{}`, exactly as
`anthus/participation.py` does after ateles#584 (where a 404 read logged a
warning and dispatch proceeded blind). The caller holds the notification.

Suppressing a page is safe here and duplicating one is not: the task is still
unrouted, still `blocked`, and still seen on the next cycle. Silence for one
cycle costs a delay; a flood costs the operator's attention permanently. This
also matches the operator's standing decision that the swarm halts on an
unreachable Neotoma rather than proceeding on assumptions.

Note the asymmetry — reads fail CLOSED (hold the page), writes fail OPEN (log
and continue). A write that cannot land costs at most a duplicate page later; a
read treated as empty costs the flood immediately.

IDEMPOTENCY: LOGICAL STATE AND CLOCKS ARE SEPARATE WRITES
---------------------------------------------------------
Neotoma's idempotency contract keys the payload it receives: same key + same
payload is a replay; same key + different payload is a validation error. So a
key derived from a volatile-stripped view must never escort a payload that still
carries `last_escalated` / `reported`.

`save_field` therefore splits:

  1. Logical membership (`_keyable(value)`) under a digest of that stripped
     shape — replay-safe when only clocks moved.
  2. The full clock-bearing value under a *separate* key that digests the full
     payload — so re-assertions land new `last_escalated` / `reported` without
     reusing the logical key with a different body.

Timestamps stay load-bearing for bounded re-assertion; they just do not ride the
logical keyed call.

Environment:
  NEOTOMA_BASE_URL       Neotoma base URL.
  NEOTOMA_BEARER_TOKEN   Bearer token. Absent ⇒ reads raise, writes no-op.
  APIS_UNROUTABLE_LEDGER_KEY  Singleton key (default "apis-unroutable").
  APIS_UNROUTABLE_CACHE_SECONDS  Re-read the row after this long (default 60).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time

import httpx

log = logging.getLogger("apis.unroutable.store")

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)

ENTITY_TYPE = "apis_unroutable_ledger"

# The field Neotoma resolves identity on. Must match SCHEMA_DEFINITION and the
# registered prod schema — FakeNeotoma imports this so the fake cannot drift.
IDENTITY_FIELD = "ledger_key"

# In-repo register_schema artifact (ateles#697). Provision lists this type in
# CONTEXT_SCHEMAS; --commit will POST this body when live provisioning lands.
# Until then this is the reviewable contract — not "verified in prod" prose.
SCHEMA_DEFINITION = {
    "entity_type": ENTITY_TYPE,
    "schema_version": "1.0",
    "canonical_name_fields": [IDENTITY_FIELD],
    "fields": {
        "ledger_key": {
            "type": "string",
            "required": True,
            "description": (
                "Stable singleton key identifying this ledger row "
                "(e.g. 'apis-unroutable'). The identity field — exactly one "
                "entity per key."
            ),
        },
        "daemon": {
            "type": "string",
            "description": "Daemon that owns this ledger (e.g. 'apis').",
        },
        "tasks": {
            "type": "object",
            "description": (
                "Map of task entity_id -> {fp, last_escalated, count}. "
                "Dedup state for unroutable-task escalations."
            ),
        },
        "roles": {
            "type": "object",
            "description": (
                "Map of role name -> last escalation epoch seconds, for roles "
                "with no agent_definition."
            ),
        },
        "unreadable": {
            "type": "object",
            "description": (
                "Map of task entity_id -> {n, reported}: consecutive failed "
                "hydrations and when last reported."
            ),
        },
        "schema_note": {
            "type": "string",
            "description": (
                "Human note describing the ledger's purpose and owning module."
            ),
        },
    },
}

# The three state maps this ledger persists. Named once so a future field is
# added in one place and every read/write path picks it up.
FIELDS = ("tasks", "roles", "unreadable")

# How long a loaded row is trusted before re-reading. The daemon is the only
# writer of its own state, so this exists to pick up an out-of-band edit (an
# operator clearing the backlog), not to resolve races.
CACHE_SECONDS = max(0, int(os.environ.get("APIS_UNROUTABLE_CACHE_SECONDS", "60")))


def _ledger_key() -> str:
    return os.environ.get("APIS_UNROUTABLE_LEDGER_KEY", "apis-unroutable")


def _bearer() -> str:
    # Read at call time, not import time: the daemon loads ~/.config/neotoma/.env
    # after this module is imported, and a token captured at import would be the
    # empty string forever — reads would raise on every cycle and every page
    # would be held. That is the fail-closed direction, but permanently.
    return os.environ.get("NEOTOMA_BEARER_TOKEN", "")


class LedgerUnavailable(RuntimeError):
    """Raised when the ledger could not be READ from Neotoma.

    Deliberately distinct from "the read succeeded and the ledger is empty".
    Collapsing the two is the ateles#584 mistake, and here it is worse than
    usual: an empty ledger means every standing unroutable task is new, so the
    caller would re-page the entire backlog at once — the exact flood this
    ledger was built to stop.

    Callers must treat this as "do not notify", never as "no prior state".
    """


def _digest(value) -> str:
    """Content digest of a value, for idempotency keys.

    Excludes nothing by itself — callers pass the logical state they want keyed.
    Timestamps inside the state are excluded by `_keyable`, below.
    """
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


# Per-field keys whose values are wall-clock timestamps. They are real state and
# are written, but they must not reach the idempotency digest: a clock in the key
# makes every write unique, which defeats replay-safety and (per the prior sync
# incident) can freeze a row permanently.
_VOLATILE_KEYS = frozenset({"last_escalated", "reported"})


def _keyable(value):
    """Strip volatile timestamps so the idempotency key reflects logical state.

    `{"ent_a": {"fp": "…", "last_escalated": 1699999999, "count": 3}}` keys the
    same whether the last escalation was a second ago or an hour ago; it changes
    when the fingerprint or the membership changes, which is what a rewrite
    actually means.
    """
    if isinstance(value, dict):
        return {
            k: _keyable(v)
            for k, v in sorted(value.items())
            if k not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_keyable(v) for v in value]
    return value


def _contains_volatile(value) -> bool:
    """True when `value` carries nested `last_escalated` / `reported` keys."""
    if isinstance(value, dict):
        if _VOLATILE_KEYS & value.keys():
            return True
        return any(_contains_volatile(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_volatile(v) for v in value)
    return False


def _unwrap(row: dict) -> dict:
    """Neotoma returns snapshots at two or three nesting depths; flatten them."""
    snap = row.get("snapshot")
    if isinstance(snap, dict):
        inner = snap.get("snapshot")
        if isinstance(inner, dict):
            return inner
        return snap
    return row


class NeotomaLedgerStore:
    """Read/write the singleton ledger row.

    Constructor takes base_url/token explicitly so tests drive it without env
    juggling; production passes nothing and picks up the environment.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        ledger_key: str | None = None,
        cache_seconds: int | None = None,
    ) -> None:
        self.base_url = (base_url or NEOTOMA_BASE_URL).rstrip("/")
        self._token = token
        self.ledger_key = ledger_key or _ledger_key()
        self.cache_seconds = (
            CACHE_SECONDS if cache_seconds is None else max(0, cache_seconds)
        )
        self.entity_id: str | None = None
        self._cache: dict | None = None
        self._cached_at: float = 0.0

    @property
    def token(self) -> str:
        return self._token if self._token is not None else _bearer()

    # ── HTTP ───────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    # ── read ───────────────────────────────────────────────────────────────

    def load(self, now: float | None = None, force: bool = False) -> dict:
        """Return `{"tasks": {...}, "roles": {...}, "unreadable": {...}}`.

        Raises LedgerUnavailable when the row could not be read. Returns empty
        maps ONLY when the read succeeded and no ledger row exists yet — the
        genuine first-boot case, where escalating IS correct.
        """
        now = time.time() if now is None else now
        if (
            not force
            and self._cache is not None
            and (now - self._cached_at) < self.cache_seconds
        ):
            return self._cache

        if not self.token:
            raise LedgerUnavailable(
                "NEOTOMA_BEARER_TOKEN is not set — cannot read the unroutable "
                "ledger; holding escalation rather than assuming it is empty"
            )

        try:
            resp = httpx.post(
                f"{self.base_url}/entities/query",
                headers=self._headers(),
                json={
                    "entity_type": ENTITY_TYPE,
                    "limit": 50,
                    "include_snapshots": True,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — re-raised as the typed error
            log.error("[unroutable] could not read the ledger: %s", exc)
            raise LedgerUnavailable(
                f"could not read the unroutable ledger: {exc}"
            ) from exc

        rows = data.get("entities") or data.get("results") or []
        state = {f: {} for f in FIELDS}
        entity_id = None
        for row in rows:
            snap = _unwrap(row)
            if snap.get("ledger_key") != self.ledger_key:
                continue
            entity_id = row.get("entity_id") or row.get("id")
            for field in FIELDS:
                value = snap.get(field)
                if isinstance(value, dict):
                    state[field] = value
            break

        self.entity_id = entity_id
        self._cache = state
        self._cached_at = now
        log.info(
            "[unroutable] ledger loaded from Neotoma (%s): %d task(s), %d role(s), "
            "%d unreadable — these will not be re-paged",
            entity_id or "no row yet",
            len(state["tasks"]),
            len(state["roles"]),
            len(state["unreadable"]),
        )
        return state

    # ── write ──────────────────────────────────────────────────────────────

    def save_field(self, field: str, value: dict) -> bool:
        """Persist one state map. Fails OPEN — logs and returns False.

        A write that does not land costs at most one duplicate page after a
        restart. That is the cheap direction; refusing to dispatch because a
        bookkeeping write failed is not.
        """
        if field not in FIELDS:
            raise ValueError(f"unknown ledger field {field!r}")
        if not self.token:
            log.warning(
                "[unroutable] no bearer token — ledger field %r not persisted", field
            )
            return False

        # Keep the local cache truthful even if the write fails: the in-process
        # decision already treated this as recorded, and a cache that disagrees
        # would re-page within the same process.
        if self._cache is not None:
            self._cache[field] = value

        # Split logical vs volatile writes so the idempotency key always
        # matches the payload Neotoma receives (ingestion contract).
        logical = _keyable(value)
        logical_key = (
            f"apis-unroutable-{self.ledger_key}-{field}-{_digest(logical)}"
        )
        ok = self._persist(field, logical, logical_key)

        if _contains_volatile(value):
            # Clocks are load-bearing for re-assertion but must not reuse the
            # logical key with a different body. Key digests the full payload.
            full_key = (
                f"apis-unroutable-{self.ledger_key}-{field}-ts-{_digest(value)}"
            )
            ok = self._persist(field, value, full_key) and ok

        return ok

    def _persist(self, field: str, value: dict, idempotency_key: str) -> bool:
        """Write one field via correct (when known) or store (create-or-match)."""
        if self.entity_id:
            if self._correct(field, value, idempotency_key):
                return True
            # Fall through to store: a correct against a stale/absent entity_id
            # should not silently lose the write.
            log.warning(
                "[unroutable] correct failed for %r — retrying as a store", field
            )
        return self._store(field, value, idempotency_key)

    def _correct(self, field: str, value: dict, idempotency_key: str) -> bool:
        try:
            resp = httpx.post(
                f"{self.base_url}/correct",
                headers=self._headers(),
                json={
                    "entity_id": self.entity_id,
                    "entity_type": ENTITY_TYPE,
                    "field": field,
                    "value": value,
                    "idempotency_key": idempotency_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 — fail open
            log.warning("[unroutable] could not correct %r: %s", field, exc)
            return False

    def _store(self, field: str, value: dict, idempotency_key: str) -> bool:
        """Create-or-match the singleton row.

        Identity resolves server-side on `ledger_key` (the schema's
        canonical_name_fields), so concurrent writers converge on one row rather
        than racing to create two — which is the coordination the disk file had
        no way to express.
        """
        try:
            resp = httpx.post(
                f"{self.base_url}/store",
                headers=self._headers(),
                json={
                    "entities": [
                        {
                            "entity_type": ENTITY_TYPE,
                            "ledger_key": self.ledger_key,
                            "daemon": "apis",
                            field: value,
                        }
                    ],
                    "idempotency_key": idempotency_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            ents = data.get("entities") or []
            if ents:
                self.entity_id = ents[0].get("entity_id") or self.entity_id
            return True
        except Exception as exc:  # noqa: BLE001 — fail open
            log.warning("[unroutable] could not store %r: %s", field, exc)
            return False

    def invalidate(self) -> None:
        """Drop the cache so the next `load` re-reads."""
        self._cache = None
        self._cached_at = 0.0


def register_schema(
    *,
    base_url: str | None = None,
    token: str | None = None,
    activate: bool = True,
) -> dict:
    """POST SCHEMA_DEFINITION to Neotoma `/register_schema`.

    The in-repo provisioning artifact for `apis_unroutable_ledger`. Called by
    live `ateles provision --commit` once W3/W4 land; callable today for a
    fresh instance. Returns the response body.
    """
    url = (base_url or NEOTOMA_BASE_URL).rstrip("/") + "/register_schema"
    bearer = token if token is not None else _bearer()
    if not bearer:
        raise LedgerUnavailable(
            "NEOTOMA_BEARER_TOKEN is not set — cannot register "
            f"{ENTITY_TYPE} schema"
        )
    body = {
        "entity_type": SCHEMA_DEFINITION["entity_type"],
        "schema_version": SCHEMA_DEFINITION["schema_version"],
        "schema_definition": {
            "canonical_name_fields": list(
                SCHEMA_DEFINITION["canonical_name_fields"]
            ),
            "fields": dict(SCHEMA_DEFINITION["fields"]),
        },
        "activate": activate,
        "force": True,  # "ledger" pluralization false-positive (see #671)
    }
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
