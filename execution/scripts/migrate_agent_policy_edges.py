#!/usr/bin/env python3
"""
Migrate `agent_policy` rows from `scope=agent`/`agent_sub` to a `GOVERNS`
graph edge to their agent's `agent_definition` (operator ruling 2026-09-25,
decision 114, master plan `decisions.agent_policy_binds_agent_by_graph_edge`;
`docs/foundation/conformance.md` row 114; `docs/foundation/migration.md` G34).

What this does, per row with `scope == "agent"` and a non-empty `agent_sub`:
  1. Resolve `agent_sub` (e.g. "corvus@ateles-swarm") to its `agent_definition`
     entity id via `lib.daemon_runtime.agent_loader.resolve_agent_definition_id`
     — the SAME resolver `AgentLoader.load_active_policies` and
     `policy_skill_renderer._session_scope_ok` use at read time, so a row this
     script cannot resolve is a row neither reader could have resolved either.
  2. Create a `GOVERNS` edge: source = the `agent_policy` entity, target =
     the resolved `agent_definition` entity.
  3. Read the edge back via `/list_relationships` and confirm it is present
     with the expected source/target before counting the row migrated
     (CLAUDE.md: "a write that reports success has not necessarily
     happened — read it back").

This script does NOT touch `scope` or `agent_sub` on the row itself — the
ruling supersedes those fields for binding purposes but does not retire them
from the schema, and rewriting them here would be a second, unreviewed
migration bundled into this one. A row with an edge is bound by the edge
regardless of what `scope`/`agent_sub` still say (`policy_binds_agent_by_edge`
in `lib/daemon_runtime/agent_loader.py`).

A `scope: swarm` or `scope: global` row is left untouched — the ruling states
"swarm-wide rules carry no edge," so there is nothing for this script to do
to one; it is not a bug that the summary counts it as "skipped (swarm-wide)."

COORDINATION: three agent_policy rows carry scope values OUTSIDE the closed
vocabulary (cadence, copy, strategy) and are being fixed to scope=agent by a
separate task (Neotoma task ent_9e08e683714f006eee043539) before this script
should touch them — this script only ever selects rows already carrying
`scope == "agent"`, so it naturally does not race that task's writes; it
simply has nothing to migrate until that task's corrections land. Re-run
this script (idempotently — see below) once that task reports done.

REGISTRATION BLOCKER: the `GOVERNS` relationship type must be registered
before `--apply` can create ANY edge (`register_relationship_type` is a
governance action gated on an admitted `agent_grant` with the
`register_relationship_type` capability — see `AGENT_POLICY_GOVERNS_EDGE`'s
docstring in `agent_loader.py`). This script's own bearer-token auth cannot
register the type; `--apply` will fail every row with
`unregistered_relationship_type` until an admitted identity (the operator,
or an agent holding that capability) runs `register_relationship_type` for
`GOVERNS` (see this file's module docstring in agent_loader.py for the exact
registration call this script expects to already have succeeded). `--dry-run`
does not depend on registration — it never writes — so it is meaningful to
run and read before that blocker clears.

Usage:
    python execution/scripts/migrate_agent_policy_edges.py             # dry-run (default)
    python execution/scripts/migrate_agent_policy_edges.py --dry-run   # explicit, same as above
    python execution/scripts/migrate_agent_policy_edges.py --apply     # write edges + read back

Exit codes:
    0 — completed (a per-row failure under --apply is reported, not a
        nonzero exit, so one unresolvable row does not abort the batch)
    1 — usage / config error (e.g. NEOTOMA_BEARER_TOKEN not set)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_LIB_DIR = _REPO_ROOT / "lib"
for _p in (_REPO_ROOT, _LIB_DIR, _LIB_DIR / "daemon_runtime"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import httpx  # noqa: E402

import agent_loader as al  # noqa: E402

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _fetch_agent_scoped_rows(base_url: str) -> list[dict]:
    """Every live `agent_policy` row with `scope == "agent"`. Reuses
    `agent_loader`'s query body and unwrap step — the ONE reader of
    `agent_policy`'s response shape (CLAUDE.md: extend the mechanism that
    already generalizes, never copy a set of values into code).
    """
    resp = httpx.post(
        f"{base_url}/entities/query",
        json=al.POLICY_QUERY_BODY,
        headers=_auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    rows = al.unwrap_policy_entities(resp.json())
    return [r for r in rows if str(r.get("scope") or "").strip().lower() == "agent"]


def _already_governed(entity_id: str, governs: dict[str, frozenset[str]]) -> bool:
    return bool(governs.get(entity_id))


def _create_governs_edge(base_url: str, source_id: str, target_id: str) -> dict:
    resp = httpx.post(
        f"{base_url}/create_relationship",
        json={
            "relationship_type": al.AGENT_POLICY_GOVERNS_EDGE,
            "source_entity_id": source_id,
            "target_entity_id": target_id,
        },
        headers=_auth_headers(),
        timeout=15,
    )
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:  # noqa: BLE001
            detail = {"raw": resp.text}
        raise RuntimeError(f"create_relationship failed ({resp.status_code}): {detail}")
    return resp.json()


def _read_back_edge(base_url: str, source_id: str, target_id: str) -> bool:
    """Confirm the edge this script just created is actually live — a 2xx
    from create_relationship is not evidence the write landed (CLAUDE.md:
    "a write that reports success has not necessarily happened. Read it
    back.").
    """
    resp = httpx.post(
        f"{base_url}/list_relationships",
        json={
            "relationship_type": al.AGENT_POLICY_GOVERNS_EDGE,
            "source_entity_id": source_id,
        },
        headers=_auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    for rel in data.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        if (
            str(rel.get("source_entity_id") or "") == source_id
            and str(rel.get("target_entity_id") or "") == target_id
        ):
            return True
    return False


def migrate(base_url: str, apply: bool) -> int:
    if not os.environ.get("NEOTOMA_BEARER_TOKEN"):
        print("NEOTOMA_BEARER_TOKEN not set — refusing to run.", file=sys.stderr)
        return 1

    rows = _fetch_agent_scoped_rows(base_url)
    governs = al.fetch_governs_edges(base_url)

    to_migrate: list[tuple[dict, str]] = []  # (row, resolved_agent_definition_id)
    already_edged: list[dict] = []
    unresolvable: list[dict] = []

    for row in rows:
        entity_id = str(row.get("_entity_id") or "")
        if _already_governed(entity_id, governs):
            already_edged.append(row)
            continue
        agent_sub = str(row.get("agent_sub") or "").strip()
        if not agent_sub:
            unresolvable.append(row)
            continue
        definition_id = al.resolve_agent_definition_id(agent_sub, base_url)
        if not definition_id:
            unresolvable.append(row)
            continue
        to_migrate.append((row, definition_id))

    print(f"agent_policy rows with scope=agent: {len(rows)}")
    print(f"  already carry a GOVERNS edge (skipped): {len(already_edged)}")
    print(f"  no agent_sub or unresolvable agent_definition (skipped, needs manual fix): {len(unresolvable)}")
    for row in unresolvable:
        print(
            f"    - {row.get('_entity_id')!r}: agent_sub={row.get('agent_sub')!r} "
            f"domain={row.get('domain')!r}"
        )
    print(f"  candidates to migrate: {len(to_migrate)}")
    for row, definition_id in to_migrate:
        print(
            f"    - {row.get('_entity_id')!r} (agent_sub={row.get('agent_sub')!r}) "
            f"-> GOVERNS -> {definition_id!r}"
        )

    if not apply:
        print("\nDRY RUN — no writes made. Re-run with --apply to create these edges.")
        return 0

    migrated = 0
    failed = 0
    for row, definition_id in to_migrate:
        entity_id = str(row.get("_entity_id") or "")
        try:
            _create_governs_edge(base_url, entity_id, definition_id)
            if not _read_back_edge(base_url, entity_id, definition_id):
                print(
                    f"    ! {entity_id}: create_relationship returned success but "
                    "the edge is not readable back — NOT counted as migrated",
                    file=sys.stderr,
                )
                failed += 1
                continue
            migrated += 1
            print(f"    + {entity_id}: edge created and read back")
        except Exception as exc:  # noqa: BLE001 — report and continue the batch
            print(f"    ! {entity_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1

    print(f"\nMigrated {migrated}/{len(to_migrate)} row(s); {failed} failure(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually create GOVERNS edges. Default is dry-run (report only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run (default behavior; provided for clarity in call sites).",
    )
    parser.add_argument(
        "--base-url",
        default=NEOTOMA_BASE_URL,
        help="Neotoma base URL (default: NEOTOMA_BASE_URL env or the hosted instance).",
    )
    args = parser.parse_args()
    apply = args.apply and not args.dry_run
    return migrate(args.base_url, apply)


if __name__ == "__main__":
    sys.exit(main())
