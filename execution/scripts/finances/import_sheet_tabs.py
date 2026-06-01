#!/usr/bin/env python3
"""
Import Google Sheets finance tabs into Neotoma as structured entities.

Handles: Loans, Fixed costs, Earnings, Liabilities.
Each tab maps to a specific entity_type with stable idempotency keys.

Usage:
  python3 import_sheet_tabs.py --tab loans --file "~/Documents/data/imports/google sheets finances/Loans-Table 1.csv"
  python3 import_sheet_tabs.py --tab fixed_costs --file "~/Documents/data/imports/google sheets finances/Fixed costs-Table 1.csv"
  python3 import_sheet_tabs.py --tab earnings --file "~/Documents/data/imports/google sheets finances/Earnings-Table 1.csv"
  python3 import_sheet_tabs.py --tab all  # imports all known tabs from default directory

  --store   Write entities JSON to temp file and call neotoma store with --file-path for provenance.

Prints JSON entities array to stdout. Pipe to neotoma store --file (or use --store).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SHEETS_DIR = Path.home() / "Documents" / "data" / "imports" / "google sheets finances"


def parse_currency(s: str) -> float | None:
    if not s or not s.strip():
        return None
    cleaned = re.sub(r"[€$US\s\t,]", "", s.strip())
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_percent(s: str) -> float | None:
    if not s or not s.strip():
        return None
    cleaned = s.strip().rstrip("%")
    try:
        return float(cleaned) / 100.0
    except ValueError:
        return None


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower().strip()).strip("_")


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def read_csv(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    reader = csv.DictReader(text.splitlines())
    return [row for row in reader]


def build_loans(path: Path) -> list[dict]:
    rows = read_csv(path)
    sha = file_sha(path)
    entities = []
    for row in rows:
        lender = (row.get("Lender") or "").strip()
        reason = (row.get("Reason") or "").strip()
        if not lender and not reason:
            continue
        loan_number = (row.get("Loan number") or "").strip()
        canonical = f"{lender} - {reason}".strip(" -")
        idem_key = (
            f"loan-{slug(canonical)}-{sha}"
            if not loan_number
            else f"loan-{slug(loan_number)}-{sha}"
        )

        entities.append(
            {
                "entity_type": "loan",
                "canonical_name": canonical,
                "lender": lender,
                "reason": reason,
                "location": (row.get("Location") or "").strip(),
                "loan_number": loan_number or None,
                "origination_date": (row.get("Date") or "").strip() or None,
                "original_eur": parse_currency(row.get("Original €", "")),
                "original_usd": parse_currency(row.get("Original $", "")),
                "outstanding_eur": parse_currency(
                    row.get("Outstanding € (2024-01)", "")
                ),
                "outstanding_usd": parse_currency(
                    row.get("Outstanding $ (2024-01)", "")
                ),
                "outstanding_percent": parse_percent(row.get("Outstanding %", "")),
                "apr": parse_percent(row.get("APR", "")),
                "monthly_payment_usd": parse_currency(row.get("Monthly payment $", "")),
                "monthly_payment_eur": parse_currency(row.get("Monthly payment €", "")),
                "maturity": (row.get("Maturity") or "").strip() or None,
                "notes": (row.get("Notes") or "").strip() or None,
                "observation_kind": "sheet_import",
                "source_file": path.name,
                "_idempotency_key": idem_key,
            }
        )
    return entities


def build_fixed_costs(path: Path) -> list[dict]:
    rows = read_csv(path)
    sha = file_sha(path)
    entities = []
    for row in rows:
        merchant = (row.get("Merchant") or "").strip()
        expense = (row.get("Expense") or "").strip()
        if not merchant and not expense:
            continue
        canonical = f"{merchant} - {expense}".strip(" -")
        idem_key = f"recurring-expense-{slug(canonical)}-{sha}"

        times_raw = (row.get("Times per year") or "").strip()
        try:
            times_per_year = int(times_raw)
        except (ValueError, TypeError):
            times_per_year = None

        freq_map = {1: "yearly", 4: "quarterly", 12: "monthly", 52: "weekly"}
        billing_frequency = freq_map.get(times_per_year) if times_per_year else None

        entities.append(
            {
                "entity_type": "recurring_expense",
                "canonical_name": canonical,
                "merchant": merchant,
                "expense_description": expense,
                "location": (row.get("Location") or "").strip() or None,
                "expense_type": (row.get("Type") or "").strip() or None,
                "billing_frequency": billing_frequency,
                "occurrences_per_year": times_per_year,
                "payment_eur": parse_currency(row.get("Payment €", "")),
                "payment_usd": parse_currency(row.get("Payment $", "")),
                "yearly_eur": parse_currency(row.get("Yearly €", "")),
                "monthly_eur": parse_currency(row.get("Monthly €", "")),
                "monthly_usd": parse_currency(row.get("Monthly $", "")),
                "payment_method": (row.get("Payment method") or "").strip() or None,
                "started": (row.get("Started") or "").strip() or None,
                "ended": (row.get("Ended") or "").strip() or None,
                "renews": (row.get("Renews") or "").strip() or None,
                "notes": (row.get("Notes") or "").strip() or None,
                "observation_kind": "sheet_import",
                "source_file": path.name,
                "_idempotency_key": idem_key,
            }
        )
    return entities


def build_earnings(path: Path) -> list[dict]:
    rows = read_csv(path)
    sha = file_sha(path)
    entities = []
    for i, row in enumerate(rows):
        source = (row.get("Source") or "").strip()
        year = (row.get("Year") or "").strip()
        quarter = (row.get("Quarter") or "").strip()
        if not source:
            continue
        canonical = f"{source} {quarter or year}".strip()
        idem_key = f"income-{slug(source)}-{slug(quarter or year)}-{sha}"

        entities.append(
            {
                "entity_type": "income",
                "canonical_name": canonical,
                "source": source,
                "year": year or None,
                "quarter": quarter or None,
                "definite": (row.get("Definite") or "").strip() or None,
                "executed": (row.get("Executed") or "").strip() or None,
                "receipt_date": (row.get("Receipt date") or "").strip() or None,
                "amount_usd": parse_currency(row.get("$ US", "")),
                "amount_eur": parse_currency(row.get("€ Spain", "")),
                "tax_percent": parse_percent(row.get("% Tax", "")),
                "earnings_net_tax_usd": parse_currency(
                    row.get("$ Earnings net tax", "")
                ),
                "earnings_net_tax_eur": parse_currency(
                    row.get("€ Earnings net tax", "")
                ),
                "asset_type": (row.get("Asset type") or "").strip() or None,
                "earnings_type": (row.get("Earnings type") or "").strip() or None,
                "denomination": (row.get("Denomination") or "").strip() or None,
                "notes": (row.get("Notes") or "").strip() or None,
                "observation_kind": "sheet_import",
                "source_file": path.name,
                "_idempotency_key": idem_key,
            }
        )
    return entities


TAB_BUILDERS: dict[str, tuple[str, callable]] = {
    "loans": ("Loans-Table 1.csv", build_loans),
    "fixed_costs": ("Fixed costs-Table 1.csv", build_fixed_costs),
    "earnings": ("Earnings-Table 1.csv", build_earnings),
}


def main():
    parser = argparse.ArgumentParser(
        description="Import sheet tabs to Neotoma entities JSON"
    )
    parser.add_argument("--tab", required=True, choices=[*TAB_BUILDERS.keys(), "all"])
    parser.add_argument(
        "--file", help="Override file path (otherwise uses default directory)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Print but do not store")
    parser.add_argument(
        "--store",
        action="store_true",
        help="Write JSON to temp file and call neotoma store with --file-path for source provenance",
    )
    args = parser.parse_args()

    all_entities = []
    source_paths: list[Path] = []

    tabs_to_process = list(TAB_BUILDERS.keys()) if args.tab == "all" else [args.tab]

    for tab in tabs_to_process:
        default_filename, builder = TAB_BUILDERS[tab]
        if args.file and args.tab != "all":
            path = Path(args.file).expanduser().resolve()
        else:
            path = SHEETS_DIR / default_filename

        if not path.exists():
            print(f"warning: {path} not found, skipping {tab}", file=sys.stderr)
            continue

        entities = builder(path)
        print(
            f"info: {tab}: {len(entities)} entities from {path.name}", file=sys.stderr
        )
        all_entities.extend(entities)
        source_paths.append(path)

    idem_keys = {}
    clean = []
    for e in all_entities:
        key = e.pop("_idempotency_key", None)
        if key:
            idem_keys[e.get("canonical_name", "")] = key
        clean.append(e)

    combined_sha = hashlib.sha256(
        json.dumps(clean, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    if args.store and not args.dry_run and clean:
        idem_key = f"sheet-import-batch-{combined_sha}"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, prefix="sheet-import-"
        ) as f:
            json.dump(clean, f, indent=2, default=str)
            tmp_path = f.name

        cmd = [
            "neotoma",
            "--servers=start",
            "store",
            "--file",
            tmp_path,
            "--idempotency-key",
            idem_key,
            "--api-only",
        ]
        if source_paths:
            cmd.extend(["--file-path", str(source_paths[0])])
        print(f"info: running: {' '.join(cmd)}", file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(
                f"error: neotoma store failed: {result.stderr or result.stdout}",
                file=sys.stderr,
            )
            return 1
        print(result.stdout, file=sys.stderr)
        print(
            f"info: stored {len(clean)} entities with idempotency_key={idem_key}",
            file=sys.stderr,
        )
        Path(tmp_path).unlink(missing_ok=True)
    else:
        json.dump(clean, sys.stdout, indent=2, default=str)
        print(file=sys.stdout)
        if idem_keys:
            print(
                f"info: combined idempotency key suggestion: sheet-import-batch-{combined_sha}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
