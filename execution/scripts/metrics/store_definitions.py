#!/usr/bin/env python3
"""Store the v1 metric_definition records (idempotent) and read each one back.

  python3 execution/scripts/metrics/store_definitions.py            # dry run: list what would be stored
  python3 execution/scripts/metrics/store_definitions.py --apply    # store + read back every record

The store is keyed by metric_id (the schema's identity), so re-running updates
the same entities; the idempotency key carries a hash of the payload, so an
unchanged catalog is a no-op and an edited one is a fresh write. Needs
NEOTOMA_BASE_URL and NEOTOMA_BEARER_TOKEN in the process (never printed).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from execution.scripts.metrics import definitions, neotoma_io  # noqa: E402
from lib.capabilities import neotoma_http  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="store for real (default: dry run)")
    args = parser.parse_args(argv)
    records = definitions.build_definitions()
    if not args.apply:
        for rec in records:
            print(f"{rec['metric_id']:42s} {rec['baseline_status']:16s} kinds={rec['default_for_kinds']}")
        print(f"\n{len(records)} definitions (dry run; pass --apply to store)")
        return 0
    try:
        print(f"Storing {len(records)} definitions on {neotoma_http.base_url()} ...")
        neotoma_http.bearer_token()
    except neotoma_http.NeotomaConfigError as exc:
        print(f"Refusing to apply: {exc}", file=sys.stderr)
        return 2
    problems = neotoma_io.store_and_verify(records, "metric-definitions-v1", "metric_id")
    if problems:
        print("PROBLEMS:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    print(f"stored and read back {len(records)} metric_definition records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
