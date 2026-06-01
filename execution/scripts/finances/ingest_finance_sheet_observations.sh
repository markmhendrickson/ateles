#!/usr/bin/env bash
# Step 1c: build Neotoma store payload from gws-exported Assets (+ optional Savings) JSON files
# and store with a stable idempotency key (re-run safe for same file bytes).
#
# Prerequisites: Neotoma CLI (`neotoma --servers=start` reaches API), same repo .env as exports.
# Exports are JSON with top-level "values" (Sheets API shape), not plain CSV, when using export_finance_sheet_gws.sh.
#
# Usage:
#   ./execution/scripts/finances/ingest_finance_sheet_observations.sh \
#     "/path/to/YYYY-MM-DD_Assets-Table 1.csv" \
#     "/path/to/YYYY-MM-DD_Savings accounts-Table 1.csv"
#
# Savings path is optional (Assets-only ingest).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
ASSETS="${1:?Usage: $0 <Assets-export.json> [Savings-export.json]}"
SAVINGS="${2:-}"

if [[ ! -f "$ASSETS" ]]; then
  echo "error: Assets file not found: $ASSETS" >&2
  exit 1
fi
if [[ -n "$SAVINGS" && ! -f "$SAVINGS" ]]; then
  echo "error: Savings file not found: $SAVINGS" >&2
  exit 1
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

NEO_JSON="$TMPDIR/financial_accounts.json"
(cd "$ROOT" && neotoma --servers=start entities list --type financial_account --limit 500 --api-only >"$NEO_JSON")

PY="$ROOT/execution/scripts/finances/build_assets_observation_entities.py"
OUT="$TMPDIR/entities.json"
if [[ -n "$SAVINGS" ]]; then
  python3 "$PY" "$ASSETS" "$NEO_JSON" "$SAVINGS" >"$OUT"
else
  python3 "$PY" "$ASSETS" "$NEO_JSON" >"$OUT"
fi

SHA16="$(shasum -a 256 "$ASSETS" | awk '{print substr($1,1,16)}')"
AS_OF="$(basename "$ASSETS" | cut -d_ -f1)"
KEY="finances-assets-gws-${AS_OF}-${SHA16}"

STORE_ARGS=(--file "$OUT" --idempotency-key "$KEY" --file-path "$ASSETS" --api-only)
(cd "$ROOT" && neotoma --servers=start store "${STORE_ARGS[@]}")
echo "Stored with idempotency_key=$KEY (source: $ASSETS)"
