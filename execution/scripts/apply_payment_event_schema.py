#!/usr/bin/env python3
"""Apply the versioned ``payment_event`` Neotoma schema (declared identity).

The definition lives in ``execution/schemas/payment_event.json``. Its identity
is ``approval_id + payment_date``, falling back to ``primary_session_key +
payment_date``, with ``canonical_name_strict`` so a write that matches neither
rule is refused instead of falling back to a guessed name (the guess landed on
``btc_address`` and merged two payments to one address into one entity).

DRY RUN BY DEFAULT: reads the live schema and prints a field-level diff (field
names and types, plus the identity rule; never record values). ``--apply``
posts the definition through ``POST /register_schema`` (a full registration:
``update_schema_incremental`` preserves ``identity_opt_out`` and so cannot
remove it), then reads the live schema back and exits non-zero unless every
declared field and the identity rule match the file.

  python3 execution/scripts/apply_payment_event_schema.py           # diff only
  python3 execution/scripts/apply_payment_event_schema.py --apply   # write + verify

Safety:
  * ``--apply`` refuses when the diff would change an existing field's type or
    drop an existing field: those are not part of this change.
  * Idempotent: when the live schema already matches the file, nothing is
    posted. ``/register_schema`` takes no idempotency key and inserts a new
    registry row on every call, so the pinned ``schema_version`` is the
    idempotency key here: a live schema at the file's version that does NOT
    match is refused rather than registered a second time.
  * Existing ``payment_event`` rows are not re-resolved by a schema change;
    the rule applies to later stores only (see the PR for the Neotoma paths).

Credentials come from NEOTOMA_BASE_URL and NEOTOMA_BEARER_TOKEN in this
process's environment only; the host is printed, the token never is.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.capabilities import neotoma_http  # noqa: E402

DEFINITION_PATH = _REPO_ROOT / "execution" / "schemas" / "payment_event.json"

EPILOG = """\
environment (needed for both the dry run and --apply, which read the live schema):
  NEOTOMA_BASE_URL      https URL of the Neotoma instance (no default)
  NEOTOMA_BEARER_TOKEN  bearer token for that instance (never printed)

Safe to re-run: a live schema that already matches the file is left alone.
"""

Requester = Callable[[str, str, "dict[str, Any] | None"], Any]


class UnreadableLiveSchemaError(ValueError):
    """A successful schema response cannot be safely compared with the file."""


def load_definition(path: Path = DEFINITION_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def build_payload(definition: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_type": definition["entity_type"],
        "schema_version": definition["schema_version"],
        "schema_definition": definition["schema_definition"],
        "reducer_config": definition["reducer_config"],
        "user_specific": False,
        "activate": True,
    }


def _field_types(schema_definition: dict[str, Any]) -> dict[str, Any]:
    fields = schema_definition.get("fields") or {}
    return {
        name: (spec.get("type") if isinstance(spec, dict) else None)
        for name, spec in fields.items()
    }


def _identity(schema_definition: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_name_fields": schema_definition.get("canonical_name_fields"),
        "canonical_name_strict": schema_definition.get("canonical_name_strict"),
        "identity_opt_out": schema_definition.get("identity_opt_out"),
    }


def diff(live: dict[str, Any] | None, definition: dict[str, Any]) -> dict[str, Any]:
    """Field-level diff of the live schema against the file (names and types only)."""
    want_def = definition["schema_definition"]
    live_def = (live or {}).get("schema_definition") or {}
    want, have = _field_types(want_def), _field_types(live_def)
    want_id, have_id = _identity(want_def), _identity(live_def)
    return {
        "live_version": (live or {}).get("schema_version"),
        "file_version": definition["schema_version"],
        "added": sorted(set(want) - set(have)),
        "removed": sorted(set(have) - set(want)),
        "type_changed": {
            n: {"live": have[n], "file": want[n]}
            for n in sorted(set(want) & set(have))
            if have[n] != want[n]
        },
        "identity": (
            {} if want_id == have_id else {"live": have_id, "file": want_id}
        ),
    }


def is_empty(d: dict[str, Any]) -> bool:
    return not (d["added"] or d["removed"] or d["type_changed"] or d["identity"]
                or d["live_version"] != d["file_version"])


def verify(live: dict[str, Any] | None, definition: dict[str, Any]) -> list[str]:
    """Problems found reading the registered schema back (empty means good)."""
    if not live:
        return ["no live schema read back"]
    d = diff(live, definition)
    problems = [f"field {n!r} missing from the live schema" for n in d["added"]]
    problems += [f"field {n!r} is present live but absent from the file"
                 for n in d["removed"]]
    problems += [
        f"field {n!r} has type {t['live']!r}, expected {t['file']!r}"
        for n, t in d["type_changed"].items()
    ]
    if d["identity"]:
        problems.append("identity rule differs from the file")
    if d["live_version"] != d["file_version"]:
        problems.append(f"schema version {d['live_version']!r} differs from the file version "
                        f"{d['file_version']!r}")
    if live.get("active") is not True:
        problems.append("schema is not active")
    return problems


def fetch_live(request: Requester, entity_type: str) -> dict[str, Any] | None:
    try:
        live = request("GET", f"/schemas/{entity_type}", None)
    except neotoma_http.NeotomaRequestError as exc:
        if exc.status == 404:
            return None
        raise
    if not isinstance(live, dict):
        raise UnreadableLiveSchemaError("response body is not an object")
    if "schema" in live:
        if not isinstance(live["schema"], dict):
            raise UnreadableLiveSchemaError("response schema is not an object")
        live = live["schema"]
    if not isinstance(live.get("schema_definition"), dict):
        raise UnreadableLiveSchemaError("schema definition is not an object")
    if not isinstance(live["schema_definition"].get("fields"), dict):
        raise UnreadableLiveSchemaError("schema fields are not an object")
    return live


def print_diff(d: dict[str, Any]) -> None:
    print(f"live version: {d['live_version']!r}  file version: {d['file_version']!r}")
    for name in d["added"]:
        print(f"  + {name}")
    for name in d["removed"]:
        print(f"  - {name}")
    for name, t in d["type_changed"].items():
        print(f"  ~ {name}: {t['live']} -> {t['file']}")
    if d["identity"]:
        print("  identity:")
        print(f"    live: {json.dumps(d['identity']['live'], sort_keys=True)}")
        print(f"    file: {json.dumps(d['identity']['file'], sort_keys=True)}")
    if is_empty(d):
        print("  (no difference)")


def run(apply_change: bool, request: Requester | None = None,
        definition: dict[str, Any] | None = None) -> int:
    request = request or neotoma_http.request_json
    definition = definition or load_definition()
    entity_type = definition["entity_type"]
    try:
        live = fetch_live(request, entity_type)
    except UnreadableLiveSchemaError as exc:
        print(f"REFUSED reading the live schema: {exc}.", file=sys.stderr)
        return 1
    except neotoma_http.NeotomaRequestError as exc:
        print(f"FAILED reading the live schema: HTTP {exc.status}. Server said: {exc.body}",
              file=sys.stderr)
        return 1
    d = diff(live, definition)
    print_diff(d)
    if not apply_change:
        print("\nDRY RUN: nothing was written. Re-run with --apply to register it.")
        return 0
    if live is not None and is_empty(d):
        if live.get("active") is True:
            print("Live schema already matches the file; nothing posted.")
            return 0
        print("REFUSED: the live schema matches the file but is not active; activate "
              "that registered version rather than registering it again.", file=sys.stderr)
        return 1
    if d["removed"] or d["type_changed"]:
        print("REFUSED: the change would drop a field or change a field's type; "
              "update the file to keep the live types first.", file=sys.stderr)
        return 1
    if live is not None and d["live_version"] == d["file_version"]:
        print(f"REFUSED: live schema is already at version {d['file_version']!r} but does "
              "not match the file; bump schema_version in the file rather than "
              "registering the same version twice.", file=sys.stderr)
        return 1
    try:
        request("POST", "/register_schema", build_payload(definition))
        back = fetch_live(request, entity_type)
    except UnreadableLiveSchemaError as exc:
        print(f"READ-BACK FAILED: {exc}.", file=sys.stderr)
        return 1
    except neotoma_http.NeotomaRequestError as exc:
        print(f"FAILED: HTTP {exc.status}. Server said: {exc.body}", file=sys.stderr)
        return 1
    problems = verify(back, definition)
    if problems:
        print("READ-BACK FAILED:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    print(f"registered and verified: {entity_type} v{back.get('schema_version')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the payment_event Neotoma schema (dry-run diff unless --apply).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true",
                        help="register the schema and verify it by read-back (default: diff only)")
    args = parser.parse_args(argv)
    try:
        host = neotoma_http.base_url()
        neotoma_http.bearer_token()
    except neotoma_http.NeotomaConfigError as exc:
        print(f"Refusing to run: {exc}.", file=sys.stderr)
        print("Set NEOTOMA_BASE_URL and NEOTOMA_BEARER_TOKEN in this process "
              "(source them without printing), then re-run.", file=sys.stderr)
        return 2
    print(f"payment_event schema on {host}:")
    return run(args.apply)


if __name__ == "__main__":
    sys.exit(main())
