#!/usr/bin/env bash
# Re-store account_statement entities with PDF sources while preserving statement metadata.
#
# Why this exists:
# - A prior provenance reload only stored canonical_name + statement_pdf_path.
# - That can hide fields the UI depends on (statement_as_of_date / period dates / registry link).
#
# This script tries to preserve or restore metadata in three layers:
# 1) Existing latest observation fields for the same account_statement entity (if present).
# 2) Metadata override file (JSON map by statement key).
# 3) Required base fields for provenance reload (canonical_name, statement_pdf_path, source_file).
#
# Usage:
#   ./execution/scripts/finances/reload_account_statements_with_metadata_and_pdfs.sh
#
# Optional environment variables:
#   STATEMENT_METADATA_JSON=/abs/path/account_statement_reload_metadata.json
#     - JSON object keyed by statement key, value = object of fields to set/override.
#   ALLOW_MINIMAL=1
#     - If set, allows reload even when no statement date fields are available.
#
# Metadata JSON shape:
# {
#   "fidelity_lyft_2025-q4": {
#     "statement_as_of_date": "2025-12-31",
#     "statement_period_start": "2025-10-01",
#     "statement_period_end": "2025-12-31",
#     "statement_source_kind": "fidelity_investment_report_pdf",
#     "account_registry_id": "fidelity_lyft_shares",
#     "tax_year_context": 2025
#   }
# }

set -euo pipefail

if ! command -v neotoma >/dev/null 2>&1; then
  echo "Missing required command: neotoma" >&2
  exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "Missing required command: jq" >&2
  exit 2
fi

HOME_IMPORTS="$HOME/Documents/data/imports"
DEFAULT_METADATA_FILE="$(cd "$(dirname "$0")" && pwd)/account_statement_reload_metadata.json"
STATEMENT_METADATA_JSON="${STATEMENT_METADATA_JSON:-$DEFAULT_METADATA_FILE}"
ALLOW_MINIMAL="${ALLOW_MINIMAL:-0}"

KEYS=(
  "amex_hysa_combined_2025-12"
  "schwab_individual_brokerage_2025-12-31"
  "schwab_roth_ira_2025-12-31"
  "schwab_sep_ira_2025-12-31"
  "schwab_investor_checking_2025-12-31"
  "capital_one_venture_2025-12-to-2026-01"
  "fidelity_lyft_2025-q4"
)

PDFS=(
  "$HOME_IMPORTS/source_material/2025/bank/Amex_HYSA_combined_2025-12-03_to_2026-01-02.pdf"
  "$HOME_IMPORTS/source_material/2025/modelo_720/Brokerage Statement_2025-12-31_997.PDF"
  "$HOME_IMPORTS/source_material/2025/modelo_720/Brokerage Statement_2025-12-31_393.PDF"
  "$HOME_IMPORTS/source_material/2025/modelo_720/Brokerage Statement_2025-12-31_821.PDF"
  "$HOME_IMPORTS/source_material/2025/bank/Bank Statement_2025-12-31_676.PDF"
  "$HOME_IMPORTS/source_material/2025/bank/Capital_One_Venture_5566_statement_2025-12-15_to_2026-01-14.pdf"
  "$HOME_IMPORTS/source_material/2025/modelo_720/Fidelity_LYFT_investment_report_2025-10-01_to_2025-12-31.pdf"
)

if [[ -f "$STATEMENT_METADATA_JSON" ]]; then
  echo "Using metadata overrides: $STATEMENT_METADATA_JSON"
  if ! jq -e type "$STATEMENT_METADATA_JSON" >/dev/null 2>&1; then
    echo "Invalid JSON file: $STATEMENT_METADATA_JSON" >&2
    exit 2
  fi
else
  echo "Metadata override file not found at $STATEMENT_METADATA_JSON (continuing without it)."
fi

ok=0
skip=0
fail=0

build_preserved_fields_from_observations() {
  local entity_id="$1"
  neotoma observations list \
    --entity-id "$entity_id" \
    --limit 200 \
    --json \
    --api-only \
  | jq -c '
      .observations
      | map(.fields // {})
      | {
          statement_as_of_date: (map(select(.statement_as_of_date != null and (.statement_as_of_date|tostring|length)>0) | .statement_as_of_date) | .[0]),
          statement_period_start: (map(select(.statement_period_start != null and (.statement_period_start|tostring|length)>0) | .statement_period_start) | .[0]),
          statement_period_end: (map(select(.statement_period_end != null and (.statement_period_end|tostring|length)>0) | .statement_period_end) | .[0]),
          statement_source_kind: (map(select(.statement_source_kind != null and (.statement_source_kind|tostring|length)>0) | .statement_source_kind) | .[0]),
          account_registry_id: (map(select(.account_registry_id != null and (.account_registry_id|tostring|length)>0) | .account_registry_id) | .[0]),
          tax_year_context: (map(select(.tax_year_context != null) | .tax_year_context) | .[0]),
          account_value: (map(select(.account_value != null) | .account_value) | .[0]),
          account_value_currency: (map(select(.account_value_currency != null and (.account_value_currency|tostring|length)>0) | .account_value_currency) | .[0]),
          ending_account_value_usd: (map(select(.ending_account_value_usd != null) | .ending_account_value_usd) | .[0]),
          ending_account_value_eur: (map(select(.ending_account_value_eur != null) | .ending_account_value_eur) | .[0])
        }
      | with_entries(select(.value != null and (if (.value|type) == "string" then (.value|length) > 0 else true end)))
    '
}

for i in "${!KEYS[@]}"; do
  key="${KEYS[$i]}"
  pdf="${PDFS[$i]}"

  if [[ ! -f "$pdf" ]]; then
    echo "SKIP $key: PDF not found at $pdf" >&2
    skip=$((skip + 1))
    continue
  fi

  basename_pdf="$(basename "$pdf")"
  idem="stmt-${key}-prov-preserve"

  entity_search_json="$(neotoma entities search --entity-type account_statement --identifier "$key" --limit 5 --json --api-only || true)"
  entity_id="$(printf '%s' "$entity_search_json" | jq -r '.entities[0].entity_id // empty')"

  preserved='{}'
  if [[ -n "$entity_id" ]]; then
    preserved="$(build_preserved_fields_from_observations "$entity_id")"
  fi

  override='{}'
  if [[ -f "$STATEMENT_METADATA_JSON" ]]; then
    override="$(jq -c --arg key "$key" '.[$key] // {}' "$STATEMENT_METADATA_JSON")"
  fi

  merged="$(jq -c --arg key "$key" --arg pdf "$pdf" --arg src "$basename_pdf" \
    --argjson preserved "$preserved" --argjson override "$override" '
      ($preserved + $override + {
        entity_type: "account_statement",
        canonical_name: $key,
        observation_kind: "provenance_reload_preserve",
        statement_pdf_path: $pdf,
        source_file: $src
      })
    ' <<< '{}')"

  has_date="$(jq -r '((.statement_as_of_date // "")|tostring|length) > 0 or ((.statement_period_end // "")|tostring|length) > 0' <<< "$merged")"
  if [[ "$has_date" != "true" && "$ALLOW_MINIMAL" != "1" ]]; then
    echo "SKIP $key: no statement date fields found in history/override; set ALLOW_MINIMAL=1 to force." >&2
    skip=$((skip + 1))
    continue
  fi

  entity_json="$(jq -c -n --argjson obj "$merged" '[$obj]')"

  echo "Storing $key with PDF: $basename_pdf ..."
  if neotoma store --json="$entity_json" --file-path "$pdf" --idempotency-key "$idem" --api-only; then
    echo "  OK: $key (idempotency_key=$idem)"
    ok=$((ok + 1))
  else
    echo "  FAIL: $key" >&2
    fail=$((fail + 1))
  fi
done

echo ""
echo "Done. ok=$ok skip=$skip fail=$fail"
