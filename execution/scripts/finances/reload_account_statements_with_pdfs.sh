#!/usr/bin/env bash
# Re-store account_statement entities with their PDF source files attached via --file-path.
# This creates new observations with the raw PDF preserved in Neotoma's sources table.
#
# Uses idempotency keys with -prov suffix so existing observations remain intact.
#
# Usage: ./execution/scripts/finances/reload_account_statements_with_pdfs.sh

set -euo pipefail

HOME_IMPORTS="$HOME/Documents/data/imports"

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

ok=0
fail=0

for i in "${!KEYS[@]}"; do
  key="${KEYS[$i]}"
  pdf="${PDFS[$i]}"

  if [[ ! -f "$pdf" ]]; then
    echo "SKIP $key: PDF not found at $pdf" >&2
    fail=$((fail + 1))
    continue
  fi

  idem="stmt-${key}-prov"
  basename_pdf="$(basename "$pdf")"

  entity_json="[{\"entity_type\":\"account_statement\",\"canonical_name\":\"${key}\",\"statement_pdf_path\":\"${pdf}\",\"source_file\":\"${basename_pdf}\",\"observation_kind\":\"provenance_reload\"}]"

  echo "Storing $key with PDF: $basename_pdf ..."
  if neotoma --servers=start store --json="$entity_json" --file-path "$pdf" --idempotency-key "$idem" --api-only; then
    echo "  OK: $key (idempotency_key=$idem)"
    ok=$((ok + 1))
  else
    echo "  FAIL: $key" >&2
    fail=$((fail + 1))
  fi
done

echo ""
echo "Done. ok=$ok fail=$fail"
