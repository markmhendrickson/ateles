#!/usr/bin/env python3
"""
Backfill transactions from legacy transactions.parquet into Neotoma.

Uses the same idempotency keys as live flows where possible:
  - Wise API transfers: wise-transfer-{wise_transfer_id}
  - CSV-import-style rows: import-tx-{transaction_id}

Re-running is safe. Rows without transaction_id get backfill-parquet-{stable_hash}.

Usage:
  python backfill_transactions_parquet_to_neotoma.py [--dry-run] [--limit N] [--offset N]
  python backfill_transactions_parquet_to_neotoma.py --parquet /path/to/transactions.parquet

Requires: neotoma on PATH, pandas, pyarrow.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date
from pathlib import Path

import pandas as pd

_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from neotoma_transaction_helpers import neotoma_store_transaction_entity

from scripts.config import DATA_DIR

DEFAULT_PARQUET = DATA_DIR / "transactions" / "transactions.parquet"


def _extract_wise_id(import_source_file: str) -> str | None:
    s = str(import_source_file or "").strip()
    if s.startswith("wise_transfer_"):
        return s.replace("wise_transfer_", "", 1) or None
    return None


def _date_str(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return date.today().isoformat()
    if isinstance(val, pd.Timestamp):
        return val.date().isoformat()
    if hasattr(val, "date") and callable(getattr(val, "date")):
        try:
            return val.date().isoformat()
        except Exception:
            pass
    s = str(val).strip()
    return s[:10] if len(s) >= 10 else date.today().isoformat()


def _float(val, default: float = 0.0) -> float:
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _str(val, default: str = "") -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    return str(val).strip()


def row_to_entity_and_key(row: pd.Series) -> tuple[dict, str] | None:
    """Build Neotoma entity + idempotency key. Skip invalid rows."""
    tid = _str(row.get("transaction_id"))
    src = _str(row.get("import_source_file"))
    desc = _str(row.get("description"), "Transaction")[:500]

    wise_id = _extract_wise_id(src)
    if wise_id:
        wid = wise_id
        idem = f"wise-transfer-{wid}"
        entity = {
            "entity_type": "transaction",
            "title": desc[:500],
            "provider": "wise",
            "wise_transfer_id": str(wid),
            "reference": "",
            "recipient_iban": "",
            "recipient_name": "",
            "target_amount": abs(_float(row.get("amount_original"))),
            "target_currency": _str(row.get("currency_original"), "EUR"),
            "source_amount": None,
            "source_currency": _str(row.get("currency_original"), "EUR"),
            "fee": None,
            "payment_status": "COMPLETED",
            "wise_balance_transaction_id": "",
            "wise_transfer_status": "",
            "transaction_date": _date_str(row.get("transaction_date")),
            "direction": "outbound",
            "category": _str(row.get("category"), "transfer"),
            "external_transaction_id": tid or idem,
            "amount_usd": _float(row.get("amount_usd")),
            "amount_original": _float(row.get("amount_original")),
            "currency_original": _str(row.get("currency_original"), "EUR"),
            "description": desc,
            "bank_provider": "wise",
            "import_source_file": src or f"wise_transfer_{wid}",
            "import_date": _date_str(row.get("import_date")),
            "source": "backfill_parquet",
        }
        return entity, idem

    if not tid:
        h = hashlib.sha256(
            f"{_date_str(row.get('transaction_date'))}|{_float(row.get('amount_original'))}|{desc}|{src}".encode()
        ).hexdigest()[:16]
        tid = f"parquet-{h}"
        idem = f"backfill-parquet-{h}"
    else:
        idem = f"import-tx-{tid}"

    entity = {
        "entity_type": "transaction",
        "title": desc[:500],
        "external_transaction_id": tid,
        "transaction_date": _date_str(row.get("transaction_date")),
        "posting_date": _date_str(row.get("posting_date")),
        "amount_usd": _float(row.get("amount_usd")),
        "amount_original": _float(row.get("amount_original")),
        "currency_original": _str(row.get("currency_original"), "EUR"),
        "description": desc,
        "category": _str(row.get("category")),
        "account_id": _str(row.get("account_id")),
        "bank_provider": _str(row.get("bank_provider")),
        "import_source_file": src,
        "import_date": _date_str(row.get("import_date")),
        "source": "backfill_parquet",
    }
    return entity, idem


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill transactions.parquet → Neotoma")
    ap.add_argument(
        "--parquet",
        type=Path,
        default=DEFAULT_PARQUET,
        help=f"Path to transactions.parquet (default: {DEFAULT_PARQUET})",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Max rows (0 = all)")
    ap.add_argument("--offset", type=int, default=0, help="Skip first N rows")
    ap.add_argument(
        "--source-csv",
        type=Path,
        default=None,
        help="Attach this CSV as the raw source file (via --file-path) on the first store call",
    )
    args = ap.parse_args()

    path = args.parquet.expanduser().resolve()
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    df = pd.read_parquet(path)
    if df.empty:
        print("Parquet file is empty.")
        sys.exit(0)

    # Stable order + dedupe by transaction_id (keep first)
    if "transaction_date" in df.columns:
        df = df.sort_values("transaction_date", na_position="last")
    if "transaction_id" in df.columns:
        df = df.drop_duplicates(subset=["transaction_id"], keep="first")

    total = len(df)
    slice_df = df.iloc[args.offset :]
    if args.limit > 0:
        slice_df = slice_df.head(args.limit)

    print(f"Rows in file (after dedupe): {total}")
    print(f"Processing: offset={args.offset}, count={len(slice_df)}")
    if args.dry_run:
        print("DRY RUN — no Neotoma writes")

    source_csv_path: str | None = None
    if args.source_csv:
        resolved = args.source_csv.expanduser().resolve()
        if resolved.exists():
            source_csv_path = str(resolved)
        else:
            print(
                f"warning: --source-csv {resolved} not found, ignoring", file=sys.stderr
            )

    ok = fail = 0
    source_attached = False
    for i, (_, row) in enumerate(slice_df.iterrows()):
        parsed = row_to_entity_and_key(row)
        if not parsed:
            fail += 1
            continue
        entity, idem = parsed
        if args.dry_run:
            print(f"  [{i + 1}] {idem} | {entity.get('title', '')[:60]}...")
            ok += 1
            continue
        sfp = None
        if source_csv_path and not source_attached:
            sfp = source_csv_path
        success, err = neotoma_store_transaction_entity(
            entity,
            idem,
            timeout=120,
            source_file_path=sfp,
        )
        if success:
            ok += 1
            if sfp:
                source_attached = True
        else:
            fail += 1
            print(f"  FAIL {idem}: {err}", file=sys.stderr)
        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{len(slice_df)} (ok={ok} fail={fail})")

    print(f"\nDone. ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
