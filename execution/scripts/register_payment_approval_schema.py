#!/usr/bin/env python3
"""Register the ``payment_approval`` Neotoma schema (Monedula session payments).

Contract-first: the schema must be registered BEFORE Monedula raises its first
``payment_approval`` record (otherwise undeclared fields are silently routed to
raw fragments, the read-back fails and every payment is held). Field names come
from ``execution/daemons/monedula/payment_approval.py`` -- the same module that
builds the records (``FIELD_SPECS``, which covers ``BOUND_FIELDS`` plus the
resolver-written ``status`` / ``operator_answer``) -- so registration and writes
cannot drift.

DRY RUN BY DEFAULT: with no flags this prints the exact request payload and
exits 0 without any network call. Pass ``--apply`` to POST it to the operator's
Neotoma instance and read the schema back.

  python3 execution/scripts/register_payment_approval_schema.py            # print
  python3 execution/scripts/register_payment_approval_schema.py --apply     # write + verify

``--apply`` needs NEOTOMA_BASE_URL and NEOTOMA_BEARER_TOKEN in this process's
environment (no default host); it prints the target host and never the token.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (_REPO_ROOT, _REPO_ROOT / "execution" / "daemons" / "monedula"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import payment_approval as contract  # noqa: E402
from lib.capabilities import neotoma_http  # noqa: E402

EPILOG = """\
environment (needed only for --apply):
  NEOTOMA_BASE_URL      https URL of the Neotoma instance to write to (no default)
  NEOTOMA_BEARER_TOKEN  bearer token for that instance (never printed)

Safe to re-run: registering the same schema version again is idempotent, and a
failed read-back leaves nothing half-applied that a second run cannot repair.
"""


def schema_definition() -> dict[str, Any]:
    return {
        "canonical_name_fields": list(contract.CANONICAL_NAME_FIELDS),
        "fields": {
            name: {"type": ftype, "description": desc}
            for name, (ftype, desc) in contract.FIELD_SPECS.items()
        },
    }


def reducer_config() -> dict[str, Any]:
    return {
        "merge_policies": {
            name: {"strategy": "last_write"} for name in contract.FIELD_SPECS
        }
    }


def build_payload() -> dict[str, Any]:
    return {
        "entity_type": contract.ENTITY_TYPE,
        "schema_version": contract.ENTITY_SCHEMA_VERSION,
        "schema_definition": schema_definition(),
        "reducer_config": reducer_config(),
        "user_specific": False,
        "activate": True,
    }


Requester = Callable[[str, str, "dict[str, Any] | None"], "dict[str, Any]"]


def verify(schema: dict[str, Any]) -> list[str]:
    """Problems found reading the registered schema back (empty means good)."""
    problems = []
    definition = schema.get("schema_definition") or {}
    fields = definition.get("fields") or {}
    for name, (ftype, _desc) in contract.FIELD_SPECS.items():
        if name not in fields:
            problems.append(f"field {name!r} missing from the registered schema")
        elif isinstance(fields[name], dict) and fields[name].get("type") not in (None, ftype):
            problems.append(
                f"field {name!r} has type {fields[name].get('type')!r}, expected {ftype!r}"
            )
    if definition.get("canonical_name_fields") not in (None, contract.CANONICAL_NAME_FIELDS):
        problems.append("canonical_name_fields differ from the expected identity")
    if schema.get("active") is not True:
        problems.append("schema is not active")
    return problems


def apply(requester: Requester | None = None) -> int:
    request = requester or neotoma_http.request_json
    try:
        request("POST", "/register_schema", build_payload())
        registered = request("GET", f"/schemas/{contract.ENTITY_TYPE}", None)
    except neotoma_http.NeotomaRequestError as exc:
        print(f"FAILED: HTTP {exc.status}. Server said: {exc.body}", file=sys.stderr)
        print("Nothing else was written. Fix the cause above and re-run; re-running is safe.", file=sys.stderr)
        return 1
    problems = verify(registered)
    if problems:
        print("READ-BACK FAILED:", *problems, sep="\n  ", file=sys.stderr)
        print("Re-running is safe.", file=sys.stderr)
        return 1
    print(f"registered and verified: {contract.ENTITY_TYPE} v{registered.get('schema_version')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Register the payment_approval Neotoma schema (dry run unless --apply).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true",
                        help="POST the schema to Neotoma and verify (default: dry run)")
    args = parser.parse_args(argv)
    if not args.apply:
        print("DRY RUN (no network call). Payload for POST /register_schema:")
        print(json.dumps(build_payload(), indent=2, sort_keys=True))
        print("\nRe-run with --apply to register it.")
        return 0
    try:
        host = neotoma_http.base_url()
        neotoma_http.bearer_token()
    except neotoma_http.NeotomaConfigError as exc:
        print(f"Refusing to apply: {exc}.", file=sys.stderr)
        print("Set NEOTOMA_BASE_URL to your Neotoma instance URL and NEOTOMA_BEARER_TOKEN to "
              "its token in this process (source them without printing), then re-run.",
              file=sys.stderr)
        return 2
    print(f"Registering {contract.ENTITY_TYPE} on {host} ...")
    return apply()


if __name__ == "__main__":
    sys.exit(main())
