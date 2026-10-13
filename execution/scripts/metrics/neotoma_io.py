"""Neotoma REST helpers for the measurement scripts: store, read back, query.

Built on ``lib.capabilities.neotoma_http`` (no hardcoded host, bearer token from
the calling process only, redirects never followed). Rules this module adds:

* a write is never reported as done on a 2xx: :func:`store_and_verify` reads the
  entity back and compares every field it wrote (undeclared fields are dropped
  silently by Neotoma, so a success code proves nothing);
* idempotency keys carry a hash of the payload, so a replay of the same content
  is a no-op and changed content is a new write;
* reads are sequential (hosted Neotoma is fragile; at most two concurrent).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.capabilities import neotoma_http  # noqa: E402

Requester = Callable[..., "dict[str, Any]"]

_RETRY_STATUSES = {429, 500, 502, 503, 504}
BATCH_SIZE = 8
REPAIR_ROUNDS = 2  # re-store only what the read-back could not find or match


def request(method: str, path: str, body: dict[str, Any] | None = None, *, retries: int = 3,
            timeout: float = 60.0) -> dict[str, Any]:
    """neotoma_http.request_json with backoff on throttling and gateway errors."""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            return neotoma_http.request_json(method, path, body, timeout=timeout)
        except neotoma_http.NeotomaRequestError as exc:
            if exc.status not in _RETRY_STATUSES or attempt == retries:
                raise
        except OSError:
            if attempt == retries:
                raise
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def payload_hash(entities: list[dict[str, Any]]) -> str:
    blob = json.dumps(entities, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def idempotency_key(label: str, entities: list[dict[str, Any]]) -> str:
    return f"metrics-{label}-{payload_hash(entities)}"


def snapshot_fields(snapshot_row: dict[str, Any]) -> dict[str, Any]:
    """The field map of an entity as returned by /entities/query or /entities/:id."""
    snap = snapshot_row.get("snapshot")
    if isinstance(snap, dict):
        return snap
    return snapshot_row


def mismatches(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Fields of ``expected`` (minus entity_type) absent from or unequal in ``actual``."""
    problems = []
    for key, want in expected.items():
        if key == "entity_type":
            continue
        got = actual.get(key, None)
        if isinstance(want, float) and isinstance(got, (int, float)):
            same = abs(want - got) < 1e-9
        else:
            same = got == want
        if not same:
            problems.append(f"{key}: wrote {want!r}, read back {got!r}")
    return problems


def store_entities(entities: list[dict[str, Any]], label: str, *,
                   observation_source: str = "import",
                   requester: Requester = request) -> dict[str, Any]:
    return requester("POST", "/store", {
        "entities": entities,
        "observation_source": observation_source,
        "idempotency_key": idempotency_key(label, entities),
    })


def query_all(entity_type: str, *, search: str | None = None, page: int = 100,
              requester: Requester = request) -> list[dict[str, Any]]:
    """All entities of a type with snapshots, paged sequentially."""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        body: dict[str, Any] = {"entity_type": entity_type, "include_snapshots": True,
                                "limit": page, "offset": offset}
        if search:
            body["search"] = search
        res = requester("POST", "/entities/query", body)
        batch = res.get("entities") or []
        rows.extend(batch)
        total = res.get("total")
        offset += page
        if not batch or (total is not None and offset >= total):
            return rows


def verify_detail(entities: list[dict[str, Any]], identity_field: str, *,
                  requester: Requester = request) -> list[tuple[str, str]]:
    """Read every entity of the batch's type back; (identity, problem) for each mismatch.

    Looks entities up by their identity field. An empty list means every field
    written was read back with the value written.
    """
    entity_type = entities[0]["entity_type"]
    stored = {}
    for row in query_all(entity_type, requester=requester):
        fields = snapshot_fields(row)
        if fields.get(identity_field) is not None:
            stored[fields[identity_field]] = fields
    problems = []
    for ent in entities:
        ident = ent[identity_field]
        fields = stored.get(ident)
        if fields is None:
            problems.append((ident, f"{entity_type} {ident}: not found on read-back"))
            continue
        problems += [(ident, f"{entity_type} {ident}: {p}") for p in mismatches(ent, fields)]
    return problems


def verify_batch(entities: list[dict[str, Any]], identity_field: str, *,
                 requester: Requester = request) -> list[str]:
    return [p for _, p in verify_detail(entities, identity_field, requester=requester)]


def store_and_verify(entities: list[dict[str, Any]], label: str, identity_field: str, *,
                     observation_source: str = "import", batch_size: int = BATCH_SIZE,
                     requester: Requester = request, verify: bool = True) -> list[str]:
    """Store in small batches, check each response, then read everything back.

    Returns problems (empty = good). Large batches were observed to report
    ``success: true`` while writing only some observations (24 entities in,
    10 observations out), so batches are small, every non-replayed response is
    checked for one observation per entity, and the read-back is the proof.
    A replayed idempotency key returns a bare acknowledgement; the read-back
    is then the only evidence, and it is enough. Entities the read-back cannot
    find are re-stored under a fresh key (a replayed key would not repair a
    partial write), for up to REPAIR_ROUNDS rounds.
    """
    problems: list[str] = []
    pending = entities
    for round_no in range(REPAIR_ROUNDS + 1):
        suffix = "" if round_no == 0 else f"-repair{round_no}"
        size = batch_size if round_no == 0 else max(1, batch_size // 4)
        problems = []
        for i in range(0, len(pending), size):
            chunk = pending[i:i + size]
            res = store_entities(chunk, label + suffix, observation_source=observation_source,
                                 requester=requester)
            if res.get("success") is False:
                problems.append(f"store reported success=false: {str(res)[:200]}")
            if res.get("unknown_fields_count"):
                problems.append(f"store reported unknown_fields_count={res['unknown_fields_count']}")
            if not res.get("replayed"):
                written = res.get("observations_created")
                if written is not None and written < len(chunk):
                    problems.append(f"batch {i}: {written} observations created for {len(chunk)} entities")
            for ent in res.get("entities") or []:
                if isinstance(ent, dict) and (ent.get("unknown_fields_count") or ent.get("unknown_fields")):
                    problems.append(f"{ent.get('canonical_name')}: unknown fields {ent.get('unknown_fields')}")
        if not verify:
            return problems
        detail = verify_detail(entities, identity_field, requester=requester)
        if not detail:
            # Earlier rounds' partial-write notes are history: the read-back is the proof.
            return [p for p in problems if "unknown" in p]
        bad = {ident for ident, _ in detail}
        pending = [e for e in entities if e[identity_field] in bad]
        problems += [p for _, p in detail]
    return problems


# --- cached, sequential observation streams (derived reads) -----------------

def default_cache_dir() -> Path:
    import os
    return Path(os.environ.get("METRICS_CACHE_DIR", str(Path.home() / ".ateles-work" / "metrics-cache")))


def trim(fields: dict[str, Any], keep: tuple[str, ...]) -> dict[str, Any]:
    return {k: fields[k] for k in keep if k in fields}


def fetch_entity_observations(entity_id: str, keep: tuple[str, ...], *, page: int = 500,
                              requester: Requester = request, pause: float = 0.3) -> list[dict[str, Any]]:
    """Every observation of one entity (newest first), trimmed to ``keep`` fields."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        res = requester("GET", f"/entities/{entity_id}/observations?limit={page}&offset={offset}")
        batch = res.get("observations") or []
        for ob in batch:
            out.append({"entity_id": entity_id, "observed_at": ob.get("observed_at"),
                        "fields": trim(ob.get("fields") or {}, keep)})
        offset += page
        if not batch or offset >= (res.get("total") or 0):
            return out
        time.sleep(pause)


def fetch_observation_stream(entity_type: str, keep: tuple[str, ...], *, page: int = 200,
                             requester: Requester = request, pause: float = 0.3) -> list[dict[str, Any]]:
    """Every observation of an entity type via the flat /observations/query stream."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        res = requester("POST", "/observations/query",
                        {"entity_type": entity_type, "limit": page, "offset": offset})
        batch = res.get("observations") or []
        for ob in batch:
            out.append({"entity_id": ob.get("entity_id"), "observed_at": ob.get("observed_at"),
                        "fields": trim(ob.get("fields") or {}, keep)})
        offset += page
        if not batch or offset >= (res.get("total") or 0):
            return out
        time.sleep(pause)


def read_cache(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_cache(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")
    tmp.replace(path)
