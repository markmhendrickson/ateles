#!/usr/bin/env python3
"""Register the ``generation_record`` Neotoma schema (ateles#1189).

Contract-first: the schema must be registered BEFORE the capability client
stores its first ``generation_record`` (otherwise undeclared fields are
silently routed to raw fragments). Field names come from
``lib.capabilities.records`` -- the same single source the client builds
records from -- so registration and writes cannot drift.

DRY RUN BY DEFAULT: with no flags this prints the exact request payload and
exits 0 without any network call. Pass ``--apply`` to POST it to the
operator's Neotoma instance and read the schema back. Registration is
operator-run; this script never runs on its own.

  python3 execution/scripts/register_generation_record_schema.py            # print
  python3 execution/scripts/register_generation_record_schema.py --apply     # write + verify

``--apply`` reads NEOTOMA_BEARER_TOKEN (and optional NEOTOMA_BASE_URL) from
this process's environment; it never prints either.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.capabilities import records  # noqa: E402

_USER_AGENT = "ateles-register-schema/1.0"  # Cloudflare 1010-blocks urllib's default


def build_payload() -> dict[str, Any]:
    return {
        "entity_type": records.GENERATION_RECORD_ENTITY_TYPE,
        "schema_version": records.GENERATION_RECORD_SCHEMA_VERSION,
        "schema_definition": records.schema_definition(),
        "reducer_config": records.reducer_config(),
        "user_specific": False,
        "activate": True,
    }


Requester = Callable[[str, str, "dict[str, Any] | None"], "dict[str, Any]"]


def _default_requester(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
    if not token:
        raise SystemExit("NEOTOMA_BEARER_TOKEN is not set; refusing to apply")
    base = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com").rstrip("/")
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} failed: HTTP {exc.code}") from None


def verify(schema: dict[str, Any]) -> list[str]:
    """Problems found reading the registered schema back (empty means good)."""
    problems = []
    fields = ((schema.get("schema_definition") or {}).get("fields")) or {}
    for name in records.GENERATION_RECORD_FIELDS:
        if name not in fields:
            problems.append(f"field {name!r} missing from the registered schema")
    if schema.get("active") is not True:
        problems.append("schema is not active")
    return problems


def apply(requester: Requester | None = None) -> int:
    request = requester or _default_requester
    request("POST", "/register_schema", build_payload())
    registered = request("GET", f"/schemas/{records.GENERATION_RECORD_ENTITY_TYPE}", None)
    problems = verify(registered)
    if problems:
        print("READ-BACK FAILED:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    print(f"registered and verified: {records.GENERATION_RECORD_ENTITY_TYPE} "
          f"v{registered.get('schema_version')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="POST the schema to Neotoma and verify (default: dry run)")
    args = parser.parse_args(argv)
    if not args.apply:
        print("DRY RUN (no network call). Payload for POST /register_schema:")
        print(json.dumps(build_payload(), indent=2, sort_keys=True))
        print("\nRe-run with --apply to register it.")
        return 0
    return apply()


if __name__ == "__main__":
    sys.exit(main())
