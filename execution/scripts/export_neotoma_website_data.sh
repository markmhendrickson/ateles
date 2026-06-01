#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." >/dev/null 2>&1 && pwd)"

if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/.env"
  set +a
fi

TMP_DIR="${REPO_ROOT}/data/tmp"
mkdir -p "${TMP_DIR}"

POSTS_RAW="${TMP_DIR}/neotoma_posts_raw.json"
LINKS_RAW="${TMP_DIR}/neotoma_links.json"
TIMELINE_RAW="${TMP_DIR}/neotoma_timeline.json"
EXPORT_JSON="${TMP_DIR}/neotoma_website_export.json"

echo "Exporting Neotoma website data..."
echo "  posts:    ${POSTS_RAW}"
echo "  links:    ${LINKS_RAW}"
echo "  timeline: ${TIMELINE_RAW}"
echo "  export:   ${EXPORT_JSON}"

neotoma --json entities list --type post --limit 5000 >"${POSTS_RAW}"
neotoma --json entities list --type link --limit 5000 >"${LINKS_RAW}"
neotoma --json entities list --type timeline_entry --limit 5000 >"${TIMELINE_RAW}"

python3 "${REPO_ROOT}/execution/scripts/export_neotoma_posts_to_website_export.py" \
  --input "${POSTS_RAW}" \
  --export "${EXPORT_JSON}"

python3 "${REPO_ROOT}/execution/scripts/build_neotoma_website_export.py" \
  --export "${EXPORT_JSON}" \
  --links "${LINKS_RAW}" \
  --timeline "${TIMELINE_RAW}"

echo "Done."

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." >/dev/null 2>&1 && pwd)"

if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/.env"
  set +a
fi

TMP_DIR="${REPO_ROOT}/data/tmp"
mkdir -p "${TMP_DIR}"

POSTS_RAW="${TMP_DIR}/neotoma_posts_raw.json"
LINKS_RAW="${TMP_DIR}/neotoma_links.json"
TIMELINE_RAW="${TMP_DIR}/neotoma_timeline.json"
EXPORT_JSON="${TMP_DIR}/neotoma_website_export.json"

echo "Exporting Neotoma website data..."
echo "  posts:    ${POSTS_RAW}"
echo "  links:    ${LINKS_RAW}"
echo "  timeline: ${TIMELINE_RAW}"
echo "  export:   ${EXPORT_JSON}"

neotoma --json entities list --type post --limit 5000 >"${POSTS_RAW}"
neotoma --json entities list --type link --limit 5000 >"${LINKS_RAW}"
neotoma --json entities list --type timeline_entry --limit 5000 >"${TIMELINE_RAW}"

python3 "${REPO_ROOT}/execution/scripts/export_neotoma_posts_to_website_export.py" \
  --input "${POSTS_RAW}" \
  --export "${EXPORT_JSON}"

python3 "${REPO_ROOT}/execution/scripts/build_neotoma_website_export.py" \
  --export "${EXPORT_JSON}" \
  --links "${LINKS_RAW}" \
  --timeline "${TIMELINE_RAW}"

echo "Done."

