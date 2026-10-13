#!/usr/bin/env python3
"""Register the four measurement entity types in the operator's Neotoma.

  python3 execution/scripts/metrics/register_schemas.py            # dry run: print payloads
  python3 execution/scripts/metrics/register_schemas.py --apply    # register + read back

``--apply`` needs NEOTOMA_BASE_URL and NEOTOMA_BEARER_TOKEN in this process
(source them without printing); it prints the target host, never the token.
Refuses to register a type that already has an active schema with different
fields (reuse or extend through ``update_schema_incremental`` instead -- a second
``register_schema`` risks dual-active rows).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from execution.scripts.metrics import neotoma_io, schemas  # noqa: E402
from lib.capabilities import neotoma_http  # noqa: E402


def existing_schema(entity_type: str):
    try:
        return neotoma_io.request("GET", f"/schemas/{entity_type}")
    except neotoma_http.NeotomaRequestError as exc:
        if exc.status == 404:
            return None
        raise


def read_back_problems(entity_type: str, registered: dict) -> list[str]:
    fields, identity = schemas.SCHEMAS[entity_type]
    definition = registered.get("schema_definition") or {}
    declared = definition.get("fields") or {}
    problems = []
    for name, (ftype, _desc) in fields.items():
        if name not in declared:
            problems.append(f"{entity_type}: field {name!r} missing from registered schema")
        elif declared[name].get("type") != ftype:
            problems.append(f"{entity_type}: field {name!r} type {declared[name].get('type')!r} != {ftype!r}")
    if definition.get("canonical_name_fields") != identity:
        problems.append(f"{entity_type}: canonical_name_fields {definition.get('canonical_name_fields')!r}")
    if registered.get("active") is not True:
        problems.append(f"{entity_type}: schema not active")
    return problems


def apply() -> int:
    bad = 0
    for entity_type in schemas.SCHEMAS:
        current = existing_schema(entity_type)
        if current is not None:
            problems = read_back_problems(entity_type, current)
            if problems:
                print(f"REFUSING {entity_type}: an active schema already exists and differs:", *problems,
                      sep="\n  ", file=sys.stderr)
                bad += 1
            else:
                print(f"{entity_type}: already registered with the expected fields; nothing to do")
            continue
        neotoma_io.request("POST", "/register_schema", schemas.register_payload(entity_type))
        problems = read_back_problems(entity_type, neotoma_io.request("GET", f"/schemas/{entity_type}"))
        if problems:
            print("READ-BACK FAILED:", *problems, sep="\n  ", file=sys.stderr)
            bad += 1
        else:
            print(f"{entity_type}: registered and verified (v{schemas.SCHEMA_VERSION})")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="register for real (default: dry run)")
    args = parser.parse_args(argv)
    if not args.apply:
        for entity_type in schemas.SCHEMAS:
            print(f"# POST /register_schema ({entity_type})")
            print(json.dumps(schemas.register_payload(entity_type), indent=2, sort_keys=True))
        return 0
    try:
        print(f"Registering on {neotoma_http.base_url()} ...")
        neotoma_http.bearer_token()
    except neotoma_http.NeotomaConfigError as exc:
        print(f"Refusing to apply: {exc}", file=sys.stderr)
        return 2
    return apply()


if __name__ == "__main__":
    sys.exit(main())
