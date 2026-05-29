#!/usr/bin/env python3
"""
Register the `strategy_drift_signal` schema in Neotoma.

This entity type is referenced across the codebase (strategy_revision_proposal.
drift_signal_refs, strategy_evaluation_report.drift_signals_found, and every
agent's prompt convention) but was never registered. The autonomous
generalization loop (lib/daemon_runtime/generalizer.py) persists one entity per
emitted drift signal so evidence can accumulate across work entities and time.

Run this once (against Neotoma prod) to create the schema. It is additive —
registering a brand-new entity type cannot affect existing rows.

    NEOTOMA_BEARER_TOKEN=... python3 scripts/register_drift_signal_schema.py

If the REST route differs in your deployment, the printed `schema_definition`
and `reducer_config` can be handed to the Neotoma MCP `register_schema` tool
(entity_type="strategy_drift_signal") verbatim.
"""

from __future__ import annotations

import json
import os
import sys

import httpx

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")

ENTITY_TYPE = "strategy_drift_signal"
SCHEMA_VERSION = "1.0"

SCHEMA_DEFINITION = {
    "fields": {
        "agent_sub": {"type": "string", "required": True},
        "signal_text": {"type": "string", "required": True},
        "emitted_at": {"type": "string", "required": True},
        "source_ref": {"type": "string"},
        "theme_key": {"type": "string"},
        "status": {"type": "string"},  # open | consumed | dismissed
    },
    "canonical_name_fields": ["agent_sub", "signal_text", "emitted_at"],
    "description": (
        "One emitted strategy_drift_signal: an agent flagging that observed "
        "reality diverges from its encoded guidance. Accumulated and clustered "
        "by the generalizer to drive agent-local policy learning."
    ),
}

# Scalar fields take the most recent write; status likewise (open -> consumed).
REDUCER_CONFIG = {
    "merge_policies": {
        "agent_sub": "last_write_wins",
        "signal_text": "last_write_wins",
        "emitted_at": "first_write_wins",
        "source_ref": "last_write_wins",
        "theme_key": "last_write_wins",
        "status": "last_write_wins",
    }
}


def main() -> int:
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN")
    if not bearer:
        print("NEOTOMA_BEARER_TOKEN not set.", file=sys.stderr)
        print("\nschema_definition:\n" + json.dumps(SCHEMA_DEFINITION, indent=2))
        print("\nreducer_config:\n" + json.dumps(REDUCER_CONFIG, indent=2))
        return 2

    body = {
        "entity_type": ENTITY_TYPE,
        "schema_version": SCHEMA_VERSION,
        "schema_definition": SCHEMA_DEFINITION,
        "reducer_config": REDUCER_CONFIG,
        "activate": True,
    }
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/register_schema",
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1

    if resp.status_code >= 400:
        print(f"HTTP {resp.status_code}: {resp.text[:400]}", file=sys.stderr)
        print(
            "\nIf /register_schema is not a REST route here, register via the "
            "Neotoma MCP `register_schema` tool with the params above.",
            file=sys.stderr,
        )
        return 1

    print(f"Registered {ENTITY_TYPE} v{SCHEMA_VERSION}: {resp.text[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
