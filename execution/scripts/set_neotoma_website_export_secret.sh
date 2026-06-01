#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

SECRET_NAME="NEOTOMA_WEBSITE_EXPORT_JSON"
TARGET_REPO="markmhendrickson/markmhendrickson"
EXPORT_PATH="${1:-data/tmp/neotoma_website_export.json}"

if [[ "${1:-}" == "--clear" ]]; then
  gh secret delete "$SECRET_NAME" -R "$TARGET_REPO"
  echo "Deleted $SECRET_NAME from $TARGET_REPO."
  echo "Deploy workflow will use committed cache/*.json."
  exit 0
fi

if [[ ! -f "$EXPORT_PATH" ]]; then
  echo "Missing export file: $EXPORT_PATH" >&2
  echo "Generate it first or pass a path as arg 1." >&2
  exit 1
fi

ENCODED="$(python3 - "$EXPORT_PATH" <<'PY'
import base64
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
print(base64.b64encode(path.read_bytes()).decode("ascii"), end="")
PY
)"

python3 - "$ENCODED" <<'PY'
import base64
import sys

base64.b64decode(sys.argv[1], validate=True)
PY

gh secret set "$SECRET_NAME" -R "$TARGET_REPO" --body "$ENCODED"
echo "Updated $SECRET_NAME in $TARGET_REPO from $EXPORT_PATH."
echo "Validation: payload is valid base64 and was stored via --body."
