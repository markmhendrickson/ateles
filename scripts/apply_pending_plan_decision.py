#!/usr/bin/env python3
"""
Apply scripts/pending_plan_decision.json to the Ateles plan via Neotoma REST.

The autonomous-generalization build session could not write this plan
`decisions` merge because Neotoma's MCP proxy kept dropping the write session
("session unknown on this API instance"). The REST `/correct` endpoint —
the same one the daemons use directly — is unaffected, so this applies it from
any host that has the bearer token.

    NEOTOMA_BEARER_TOKEN=... python3 scripts/apply_pending_plan_decision.py
    NEOTOMA_BEARER_TOKEN=... python3 scripts/apply_pending_plan_decision.py --dry-run

Idempotent: the correction carries a fixed idempotency_key, so re-running it
applies exactly once. `decisions` is stored as a JSON string, so the map under
`value` is serialized before sending.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")

PAYLOAD_PATH = Path(__file__).resolve().parent / "pending_plan_decision.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Print the request without sending.")
    args = ap.parse_args()

    spec = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    value = spec["value"]
    # `decisions` is a JSON-string field; serialize the map if needed.
    if not isinstance(value, str):
        value = json.dumps(value)

    body = {
        "entity_id": spec["entity_id"],
        "entity_type": spec["entity_type"],
        "field": spec["field"],
        "value": value,
        "idempotency_key": spec["idempotency_key"],
    }

    if args.dry_run:
        print(json.dumps({**body, "value": f"<{len(value)} chars of JSON>"}, indent=2))
        return 0

    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN")
    if not bearer:
        print("NEOTOMA_BEARER_TOKEN not set.", file=sys.stderr)
        return 2

    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/correct",
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
        return 1
    print(f"Applied plan decisions merge: {resp.text[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
