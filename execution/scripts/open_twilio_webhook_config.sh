#!/bin/bash
# Open Twilio Console to phone number configuration page
# Extracts tunnel URL from logs and opens browser with webhook URL ready to paste

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Require DATA_DIR environment variable
if [ -z "$DATA_DIR" ]; then
    echo "Error: DATA_DIR environment variable is not set" >&2
    exit 1
fi

TUNNEL_LOG="$DATA_DIR/logs/cloudflare_twilio_sms_tunnel.error.log"

# Get tunnel URL from logs
TUNNEL_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | tail -1)

if [ -z "$TUNNEL_URL" ]; then
    echo "⚠️  Tunnel URL not found in logs"
    echo "Checking other log files..."
    TUNNEL_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$DATA_DIR/logs/cloudflare_twilio_sms_tunnel.log" 2>/dev/null | tail -1)
fi

if [ -z "$TUNNEL_URL" ]; then
    echo "❌ Could not find tunnel URL"
    echo "Please check tunnel logs:"
    echo "  tail -f $DATA_DIR/logs/cloudflare_twilio_sms_tunnel.error.log"
    exit 1
fi

WEBHOOK_URL="${TUNNEL_URL}/webhook/twilio/sms"

echo "Tunnel URL: $TUNNEL_URL"
echo "Webhook URL: $WEBHOOK_URL"
echo ""
echo "Opening Twilio Console..."
echo ""

# Open Twilio phone number configuration page
open "https://console.twilio.com/us1/develop/phone-numbers/manage/incoming"

echo "Instructions:"
echo "1. Click on phone number +16503198857"
echo "2. Scroll to 'Messaging' section"
echo "3. Find 'A MESSAGE COMES IN' field"
echo "4. Paste this URL: $WEBHOOK_URL"
echo "5. Set HTTP Method to POST"
echo "6. Click Save"
echo ""
echo "Webhook URL copied to clipboard (if pbcopy is available)"
echo "$WEBHOOK_URL" | pbcopy 2>/dev/null && echo "✓ URL copied to clipboard" || echo "⚠️  Could not copy to clipboard"






