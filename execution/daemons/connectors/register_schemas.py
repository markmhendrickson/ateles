#!/usr/bin/env python3
"""Register connector schemas against Neotoma — read-back gated.

    python3 execution/daemons/connectors/register_schemas.py
    python3 execution/daemons/connectors/register_schemas.py --json

Exit 0 only when both ``connector_status`` (v2.0) and ``deployment_observation``
are verified by read-back. ``success: true`` from registration alone is never
enough.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

from lib.connectors.schema_registration import (  # noqa: E402
    register_connector_schemas,
)
from lib.connectors.store import ConnectorStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable RegistrationSummary JSON",
    )
    args = parser.parse_args(argv)

    store = ConnectorStore()
    if not store.configured:
        msg = {
            "ok": False,
            "error": "preflight: NEOTOMA_BEARER_TOKEN missing — no schema write attempted",
            "schemas": [],
        }
        if args.json:
            print(json.dumps(msg))
        else:
            print(msg["error"], file=sys.stderr)
        return 1

    summary = register_connector_schemas(store)
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    else:
        for verdict in summary.verdicts:
            mark = "verified" if verdict.verified else "FAILED"
            print(
                f"{verdict.entity_type}: {verdict.action} [{mark}] "
                f"identity={verdict.identity} {verdict.mutable} "
                f"reducer={verdict.reducer} version={verdict.schema_version} "
                f"read_back_at={verdict.read_back_at}"
            )
            for problem in verdict.problems:
                print(f"  - {problem}")
        if summary.empty_records_note:
            print(summary.empty_records_note)
        if summary.error:
            print(summary.error, file=sys.stderr)
        elif summary.ok:
            print("both connector schemas verified by read-back")
    return 0 if summary.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
