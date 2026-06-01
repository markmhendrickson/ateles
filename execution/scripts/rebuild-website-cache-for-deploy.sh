#!/usr/bin/env bash
# Rebuild website cache from Neotoma export so you can commit and push;
# GitHub deploy will use the committed cache (no NEOTOMA_WEBSITE_EXPORT_JSON needed).
#
# Prereq: Export from Neotoma to data/tmp/neotoma_website_export.json (from repo root).
# Run from ateles repo root.
set -e
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"
EXPORT="${1:-data/tmp/neotoma_website_export.json}"
if [ ! -f "$EXPORT" ]; then
  echo "Missing export: $EXPORT" >&2
  echo "Export from Neotoma to that path, or pass path: $0 /path/to/neotoma_website_export.json" >&2
  exit 1
fi
python3 execution/scripts/generate_posts_cache.py --from-neotoma-json "$EXPORT"
echo ""
echo "Cache rebuilt. To commit and push (from website repo):"
echo "  cd execution/website/markmhendrickson"
echo "  git add react-app/cache/*.json react-app/cache/api/*.json"
echo "  git commit -m \"Rebuild cache from Neotoma\""
echo "  git push origin main"
