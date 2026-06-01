#!/usr/bin/env bash
# Copy finance source files (PDF, CSV, XLSX, etc.) into a canonical local imports tree.
# Never commit these paths — they live under ~/Documents (or FINANCE_SOURCE_IMPORT_ROOT).
#
# Usage:
#   ./execution/scripts/finances/copy_finance_source_material.sh --tax-year 2025 --kind modelo_720 FILE [FILE...]
#   ./execution/scripts/finances/copy_finance_source_material.sh --tax-year 2025 --kind us_tax FILE [FILE...]
#
# Kinds (destination subfolder):
#   modelo_720   — Spanish 720 balance / statement evidence
#   modelo_721   — Crypto / Koinly / exchange exports for 721
#   us_tax       — US 1099, W-2, broker tax packets
#   broker       — Generic broker statements (when not only 720)
#   bank         — Bank / card statements
#   sheet_export — Mirrors of sheet pulls (optional; gws already writes to google sheets finances)
#   unclassified — Default if --kind omitted
#
# Env:
#   FINANCE_SOURCE_IMPORT_ROOT  (default: $HOME/Documents/data/imports)

set -euo pipefail

ROOT="${FINANCE_SOURCE_IMPORT_ROOT:-${HOME}/Documents/data/imports}"
TAX_YEAR=""
KIND="unclassified"
FILES=()

usage() {
  sed -n '1,25p' "$0" | tail -n +2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tax-year)
      TAX_YEAR="${2:?}"
      shift 2
      ;;
    --kind)
      KIND="${2:?}"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      FILES+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$TAX_YEAR" ]]; then
  echo "error: --tax-year YYYY is required (e.g. 2025 for filing-year / document year)." >&2
  exit 1
fi
if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "error: pass at least one FILE path." >&2
  exit 1
fi

case "$KIND" in
  modelo_720|modelo_721|us_tax|broker|bank|sheet_export|unclassified) ;;
  *)
    echo "error: unknown --kind $KIND" >&2
    exit 1
    ;;
esac

DEST="${ROOT}/source_material/${TAX_YEAR}/${KIND}"
mkdir -p "$DEST"

for src in "${FILES[@]}"; do
  if [[ ! -f "$src" ]]; then
    echo "error: not a file: $src" >&2
    exit 1
  fi
  base="$(basename "$src")"
  target="${DEST}/${base}"
  if [[ -e "$target" ]]; then
    stem="${base%.*}"
    ext=""
    [[ "$base" == *.* ]] && ext=".${base##*.}"
    target="${DEST}/${stem}.dup-$(date +%Y%m%d-%H%M%S)${ext}"
    echo "note: target exists; writing $target" >&2
  fi
  cp -p "$src" "$target"
  echo "$target"
done
