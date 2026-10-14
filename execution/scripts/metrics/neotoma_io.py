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
import os
import sys
import tempfile
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
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
PAGE_PAUSE = 0.3  # seconds between sequential pages: hosted Neotoma is fragile
MAX_PAGES = 400  # a read that needs more pages than this is a fault, not a big corpus
# Repair-round keys carry this per-process nonce, so an immediate rerun of an
# unchanged payload cannot replay a repair key that an earlier run already spent.
_RUN_NONCE = uuid.uuid4().hex[:8]


class ReadLimitError(RuntimeError):
    """A paged read hit the page cap without reaching the end of the result."""


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
              snapshot_filters: dict[str, Any] | None = None, max_pages: int | None = None,
              pause: float | None = None, requester: Requester = request) -> list[dict[str, Any]]:
    """All entities of a type with snapshots, paged sequentially.

    Advances by the rows actually returned (a short page never skips rows),
    pauses between pages, stops when the server reports no ``total`` (it cannot
    be paged safely then, and looping on a server that ignores ``offset`` never
    ends), and raises :class:`ReadLimitError` after ``max_pages`` pages.
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    limit_pages = MAX_PAGES if max_pages is None else max_pages
    for _ in range(limit_pages):
        body: dict[str, Any] = {"entity_type": entity_type, "include_snapshots": True,
                                "limit": page, "offset": offset}
        if search:
            body["search"] = search
        if snapshot_filters:
            body["snapshot_filters"] = snapshot_filters
        res = requester("POST", "/entities/query", body)
        batch = res.get("entities") or []
        rows.extend(batch)
        total = res.get("total")
        offset += len(batch)
        if not batch or total is None or offset >= total:
            return rows
        time.sleep(PAGE_PAUSE if pause is None else pause)
    raise ReadLimitError(f"{entity_type}: more than {limit_pages} pages of {page}; refusing to keep reading")


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
        suffix = "" if round_no == 0 else f"-repair{round_no}-{_RUN_NONCE}"
        size = batch_size if round_no == 0 else max(1, batch_size // 4)
        problems = []
        for i in range(0, len(pending), size):
            if i:
                time.sleep(PAGE_PAUSE)  # back-to-back /store calls strain the hosted instance
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
        try:
            detail = verify_detail(entities, identity_field, requester=requester)
        except ReadLimitError as exc:
            return problems + [f"read-back incomplete: {exc}"]
        if not detail:
            # Earlier rounds' partial-write notes are history: the read-back is the proof.
            return [p for p in problems if "unknown" in p]
        bad = {ident for ident, _ in detail}
        pending = [e for e in entities if e[identity_field] in bad]
        problems += [p for _, p in detail]
    return problems


def store_command(records: list[dict[str, Any]], label: str, identity_field: str, *,
                  dry_run: bool = False) -> int:
    """The shared tail of every ``store`` subcommand: name the target, then write and read back.

    The target host is printed before any write (never the token), so an
    ambient dev or client instance in the shell cannot be written to silently.
    ``dry_run`` prints the target and counts and writes nothing.
    """
    try:
        target = neotoma_http.base_url()
        neotoma_http.bearer_token()
    except neotoma_http.NeotomaConfigError as exc:
        print(f"Refusing to store: {exc}", file=sys.stderr)
        return 2
    if dry_run:
        print(f"dry run: would store {len(records)} {records[0]['entity_type'] if records else 'record'} "
              f"records on {target}; nothing written")
        return 0
    print(f"Storing {len(records)} records on {target} ...")
    problems = store_and_verify(records, label, identity_field)
    if problems:
        print("PROBLEMS:", *problems[:40], sep="\n  ", file=sys.stderr)
        return 1
    print(f"stored and read back {len(records)} records")
    return 0


# --- cached, sequential observation streams (derived reads) -----------------

def default_cache_dir() -> Path:
    import os
    return Path(os.environ.get("METRICS_CACHE_DIR", str(Path.home() / ".ateles-work" / "metrics-cache")))


def trim(fields: dict[str, Any], keep: tuple[str, ...]) -> dict[str, Any]:
    return {k: fields[k] for k in keep if k in fields}


def fetch_entity_observations(entity_id: str, keep: tuple[str, ...], *, page: int = 500,
                              requester: Requester = request, pause: float | None = None,
                              max_pages: int | None = None) -> list[dict[str, Any]]:
    """Every observation of one entity (newest first), trimmed to ``keep`` fields."""
    out: list[dict[str, Any]] = []
    offset = 0
    quoted = urllib.parse.quote(str(entity_id), safe="")
    limit_pages = MAX_PAGES if max_pages is None else max_pages
    for _ in range(limit_pages):
        res = requester("GET", f"/entities/{quoted}/observations?limit={page}&offset={offset}")
        batch = res.get("observations") or []
        for ob in batch:
            out.append({"entity_id": entity_id, "observed_at": ob.get("observed_at"),
                        "fields": trim(ob.get("fields") or {}, keep)})
        offset += len(batch)
        if not batch or offset >= (res.get("total") or 0):
            return out
        time.sleep(PAGE_PAUSE if pause is None else pause)
    raise ReadLimitError(f"entity {entity_id}: more than {limit_pages} pages of observations")


def fetch_observation_stream(entity_type: str, keep: tuple[str, ...], *, page: int = 200,
                             requester: Requester = request, pause: float | None = None,
                             max_pages: int | None = None) -> list[dict[str, Any]]:
    """Every observation of an entity type via the flat /observations/query stream."""
    out: list[dict[str, Any]] = []
    offset = 0
    limit_pages = MAX_PAGES if max_pages is None else max_pages
    for _ in range(limit_pages):
        res = requester("POST", "/observations/query",
                        {"entity_type": entity_type, "limit": page, "offset": offset})
        batch = res.get("observations") or []
        for ob in batch:
            out.append({"entity_id": ob.get("entity_id"), "observed_at": ob.get("observed_at"),
                        "fields": trim(ob.get("fields") or {}, keep)})
        offset += len(batch)
        if not batch or offset >= (res.get("total") or 0):
            return out
        time.sleep(PAGE_PAUSE if pause is None else pause)
    raise ReadLimitError(f"{entity_type}: more than {limit_pages} pages of observations")


def read_cache(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


META_NAME = "meta.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_cache(path: Path, rows: list[dict[str, Any]], *, fetched_at: str | None = None) -> None:
    """Write one cache file (mode 0600, directory 0700, unique temp name) and record its fetch time.

    The sidecar ``meta.json`` in the same directory holds, per cache name, when
    the rows were fetched and how many there were, so a later run can refuse a
    stale cache and stamp readings with the fetch time instead of its own clock.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    meta = read_meta(path.parent)
    stamps = [r.get("observed_at") for r in rows if r.get("observed_at")]
    meta[path.stem] = {"fetched_at": fetched_at or _now_iso(), "rows": len(rows),
                       "newest_observed_at": max(stamps) if stamps else None}
    write_meta(path.parent, meta)


def read_meta(cache_dir: Path) -> dict[str, Any]:
    path = cache_dir / META_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(cache_dir: Path, meta: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=cache_dir, prefix=META_NAME + ".", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(meta, fh, sort_keys=True, indent=1)
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, cache_dir / META_NAME)
