#!/usr/bin/env bash
# Store a command/procedure in Neotoma and print raw store + entity result.
# Usage: source ateles/.env (or export NEOTOMA_BEARER_TOKEN); ./neotoma_store_command_and_show_raw.sh
# Requires: NEOTOMA_BEARER_TOKEN, optional NEOTOMA_API_URL (default https://neotoma.markmhendrickson.com)
set -euo pipefail
BASE="${NEOTOMA_API_URL:-https://neotoma.markmhendrickson.com}"
if [ -z "${NEOTOMA_BEARER_TOKEN:-}" ]; then
  echo "NEOTOMA_BEARER_TOKEN not set. source ateles/.env or export it." >&2
  exit 1
fi

# Command entity: start Neotoma prod API with watch + tunnel (Documents/data)
IDEMPOTENCY_KEY="command-neotoma-tunnel-watch-prod-$(date +%s)"
ENTITY_JSON=$(cat <<'EOF'
{
  "entities": [
    {
      "entity_type": "standing_rule",
      "title": "Start Neotoma prod tunnel with watch",
      "description": "Start API with tsx watch (auto-reload on repo changes), prod data dir Documents/data, then tunnel. Steps: (1) Kill :3180: lsof -ti :3180 | xargs kill -9. (2) Start watch:prod with ateles env: cd neotoma && set -a && source ateles/.env && set +a && nohup npm run watch:prod >> /tmp/neotoma-watch-prod.log 2>&1 &. (3) Ensure one cloudflared: pkill -f 'cloudflared tunnel run mcp-servers'; nohup cloudflared tunnel run mcp-servers >> /tmp/cloudflared-mcp-servers.log 2>&1 &.",
      "canonical_name": "neotoma-tunnel-watch-prod"
    }
  ],
  "idempotency_key": "PLACEHOLDER"
}
EOF
)
ENTITY_JSON=$(echo "$ENTITY_JSON" | sed "s/PLACEHOLDER/$IDEMPOTENCY_KEY/")

echo "=== POST /store (raw response) ==="
STORE_RESPONSE=$(curl -sS -X POST "$BASE/store" \
  -H "Authorization: Bearer $NEOTOMA_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$ENTITY_JSON")
echo "$STORE_RESPONSE" | jq .

# Extract first entity_id from store result to fetch raw entity
ENTITY_ID=$(echo "$STORE_RESPONSE" | jq -r '.entities[0].entity_id // empty')
if [ -n "$ENTITY_ID" ] && [ "$ENTITY_ID" != "null" ]; then
  echo ""
  echo "=== GET /entities/$ENTITY_ID (raw entity) ==="
  curl -sS -X GET "$BASE/entities/$ENTITY_ID" \
    -H "Authorization: Bearer $NEOTOMA_BEARER_TOKEN" | jq .
else
  echo ""
  echo "(No entity_id in store response; skipping GET /entities/:id)"
fi
