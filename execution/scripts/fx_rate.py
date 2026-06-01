#!/usr/bin/env python3
"""CLI: ECB FX via Frankfurter (same backend as import scripts).

Examples:
  python execution/scripts/fx_rate.py USD EUR 2025-12-31
  python execution/scripts/fx_rate.py EUR USD --latest
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.frankfurter_fx import fetch_frankfurter_rate


def main() -> None:
    p = argparse.ArgumentParser(description="Print Frankfurter (ECB) exchange rate.")
    p.add_argument("from_currency", help="ISO 4217 base, e.g. USD")
    p.add_argument("to_currency", help="ISO 4217 quote, e.g. EUR")
    p.add_argument(
        "date",
        nargs="?",
        help="Historical date YYYY-MM-DD (default: Frankfurter /latest)",
    )
    p.add_argument(
        "--latest", action="store_true", help="Use latest rate (ignore date)"
    )
    args = p.parse_args()
    d = None if args.latest else (args.date or None)
    rate = fetch_frankfurter_rate(args.from_currency, args.to_currency, date=d)
    if rate is None:
        sys.exit("Frankfurter returned no rate (check currencies and date).")
    print(rate)


if __name__ == "__main__":
    main()
