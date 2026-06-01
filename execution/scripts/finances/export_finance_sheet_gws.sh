#!/usr/bin/env bash
# Export tabs from the Google Sheets workbook **Finances** to CSV via gws (Google Workspace CLI).
# (Spreadsheet title in Drive: "Finances"; ID is still taken from the URL, not the title.)
# Prereqs: gws installed; `gws auth login` with sheets scope (see main_financial_accounts_registry.md).
#
# Env (set in repo .env or export before running):
#   FINANCE_GOOGLE_SHEET_ID   — required. ID from URL: .../spreadsheets/d/<ID>/edit (Finances workbook).
#   FINANCE_GWS_OUTPUT_DIR    — optional. Default: ~/Documents/data/imports/google sheets finances
#   FINANCE_SHEET_TABS        — optional. Comma-separated tab names. Default when unset:
#                             Assets, Savings accounts (Step 1 priority). Set
#                             FINANCE_EXPORT_ALL_TABS=1 to export every tab in the workbook.
#
# Usage:
#   ./export_finance_sheet_gws.sh
#   FINANCE_SHEET_TABS="Assets,Savings accounts" ./export_finance_sheet_gws.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${REPO_ROOT}/.env"
  set +a
fi

if ! command -v gws >/dev/null 2>&1; then
  echo "error: gws not found. Install: https://github.com/googleworkspace/cli" >&2
  exit 1
fi

if [[ -z "${FINANCE_GOOGLE_SHEET_ID:-}" ]]; then
  echo "error: FINANCE_GOOGLE_SHEET_ID is not set (add to ${REPO_ROOT}/.env)" >&2
  exit 1
fi

OUT_DIR="${FINANCE_GWS_OUTPUT_DIR:-${HOME}/Documents/data/imports/google sheets finances}"
RUN_DATE="$(date +%Y-%m-%d)"
mkdir -p "${OUT_DIR}"

SID="${FINANCE_GOOGLE_SHEET_ID}"

quote_range() {
  local name="$1"
  local escaped="${name//\'/\'\'}"
  echo "'${escaped}'!A:ZZ"
}

safe_filename() {
  echo "$1" | tr '/' '-' | tr ':' '-'
}

list_all_tabs() {
  if ! command -v jq >/dev/null 2>&1; then
    echo "error: jq required when FINANCE_EXPORT_ALL_TABS=1" >&2
    exit 1
  fi
  gws sheets spreadsheets get \
    --params "$(jq -n --arg id "${SID}" '{spreadsheetId: $id}')" \
    --format json | jq -r '.sheets[].properties.title'
}

if [[ -n "${FINANCE_SHEET_TABS:-}" ]]; then
  IFS=',' read -r -a TABS <<< "${FINANCE_SHEET_TABS}"
  for i in "${!TABS[@]}"; do
    TABS[i]="$(echo "${TABS[i]}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  done
elif [[ "${FINANCE_EXPORT_ALL_TABS:-0}" == "1" ]]; then
  mapfile -t TABS < <(list_all_tabs)
else
  TABS=("Assets" "Savings accounts")
  echo "note: exporting default tabs only (Assets, Savings accounts). Set FINANCE_SHEET_TABS or FINANCE_EXPORT_ALL_TABS=1 for more."
fi

echo "Exporting ${#TABS[@]} tab(s) to ${OUT_DIR}"
echo "Run date: ${RUN_DATE} (use as as_of_date for Neotoma import_artifact)"

for tab in "${TABS[@]}"; do
  [[ -z "${tab}" ]] && continue
  range="$(quote_range "${tab}")"
  safe="$(safe_filename "${tab}")"
  out="${OUT_DIR}/${RUN_DATE}_${safe}-Table 1.csv"
  echo "  -> ${tab} -> ${out}"
  gws sheets +read --spreadsheet "${SID}" --range "${range}" --format csv > "${out}"
done

echo "Done."
