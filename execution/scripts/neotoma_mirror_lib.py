"""neotoma_mirror_lib.py — shared plumbing for Neotoma-to-repo-file projection
scripts (render_plan_docs.py, render_agent_docs.py, render_positioning_docs.py).

All three follow the same contract: Neotoma is canonical, the repo file is a
generated mirror, and `--check` (wired into scripts/lint.sh) fails the build
if disk drifts from Neotoma. This module holds the parts that must behave
identically across all of them so the contract does not drift script-to-script:

- Neotoma HTTP fetch/env loading (`_load_env`, `_request` in each script,
  unified here).
- The **observation-id stamp**. Every mirror in this repo previously stamped
  only the entity id, so a mirror could go stale against a corrected field
  and the file itself had no way to say so — the only way to find out was to
  re-run `--check`. `retrieve_entity_snapshot`'s `provenance` map already
  carries a per-field observation id (the exact id of the observation that
  produced the field's current value); this module reads that map and
  formats it as a stable, sorted `observation_ids` block so every generator
  that touches an entity snapshot can stamp it the same way.

Usage in a generator:

    from neotoma_mirror_lib import load_env, request, unwrap_snapshot, \
        observation_ids_block

    base_url, token = load_env()
    entity = request(f"{base_url}/entities/{entity_id}", token)
    snapshot, provenance = unwrap_snapshot(entity)
    stamp_lines = observation_ids_block(provenance, fields=(...))
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def load_env() -> tuple[str, str]:
    """Load NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN from env, falling back to
    ~/.config/neotoma/.env. Identical across all three callers before this
    module existed — kept byte-for-byte so behavior does not shift under any
    of them."""
    base_url = os.environ.get("NEOTOMA_BASE_URL", "")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    env_path = Path.home() / ".config" / "neotoma" / ".env"
    if (not base_url or not token) and env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if key == "NEOTOMA_BASE_URL" and not base_url:
                base_url = value
            elif key == "NEOTOMA_BEARER_TOKEN" and not token:
                token = value
    if not base_url:
        sys.exit("NEOTOMA_BASE_URL not set (env or ~/.config/neotoma/.env)")
    return base_url.rstrip("/"), token


def request(url: str, token: str, payload: dict | None = None, retries: int = 5) -> dict:
    """GET/POST against the Neotoma REST API, retrying transient network
    errors. Matches render_agent_docs.py's retry behavior (5 attempts, 2s
    backoff) since that is the more resilient of the two prior
    implementations and there is no reason for a positioning render to be
    more fragile than an agent-doc render."""
    last = None
    for _attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "ateles-neotoma-sync/1.0")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            if payload is not None:
                req.add_header("Content-Type", "application/json")
                req.data = json.dumps(payload).encode()
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, ConnectionError) as exc:
            last = exc
            time.sleep(2)
    raise SystemExit(f"Neotoma unreachable after {retries} tries: {last}")


def unwrap_snapshot(entity: dict) -> tuple[dict, dict]:
    """Unwrap the nested snapshot shape returned by /entities/{id} and
    retrieve_entity_snapshot, returning (snapshot_fields, provenance_map).

    provenance_map is field_name -> observation_id for the observation that
    produced that field's current value (per retrieve_entity_snapshot's
    documented shape). Returns {} for provenance if the entity payload does
    not carry one (e.g. an older API response shape) rather than raising —
    the observation-id stamp degrades to "unavailable" instead of the whole
    render failing.
    """
    snapshot = entity.get("snapshot", entity)
    if isinstance(snapshot.get("snapshot"), dict):
        # /entities/{id} nests once more than retrieve_entity_snapshot.
        provenance = snapshot.get("provenance") or entity.get("provenance") or {}
        snapshot = snapshot["snapshot"]
    else:
        provenance = entity.get("provenance") or {}
    return snapshot, provenance


def observation_ids_block(provenance: dict, fields: list[str] | tuple[str, ...]) -> list[str]:
    """Render a deterministic `observation_ids:` YAML block (as a list of
    already-indented lines, ready to append into a frontmatter block) for the
    given fields, using the entity's provenance map.

    Multiple fields sharing one observation id (the common case — a single
    correction usually touches several fields in the same write) are still
    each listed explicitly rather than deduplicated: the reader should be
    able to look up any one field's freshness without cross-referencing a
    dedup key, and this is what makes staleness visible IN THE FILE per-field
    rather than only at the entity level.

    A field with no provenance entry (absent field, or a snapshot fetched
    without provenance) is stamped `unknown` rather than silently omitted —
    per the repo's fail-closed discipline, an unstamped field must read as
    "freshness not verifiable," never as "this field is fine."
    """
    lines = ["observation_ids:"]
    for field in fields:
        obs_id = provenance.get(field)
        lines.append(f"  {field}: {obs_id if obs_id else 'unknown'}")
    return lines


def yaml_scalar(v) -> str:
    """Quote a YAML scalar when it contains characters that would break
    parsing. Shared verbatim from render_agent_docs.py's _yaml_scalar."""
    sv = str(v)
    if sv == "":
        return '""'
    if any(c in sv for c in ":#") or sv[0] in "!&*?{}[]|>@`\"'%,-" or sv != sv.strip():
        return '"' + sv.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return sv
