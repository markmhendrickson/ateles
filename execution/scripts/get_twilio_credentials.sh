#!/bin/bash
# Helper script to get Twilio credentials and set them up

echo "Twilio Credentials Setup"
echo "========================"
echo ""
echo "Opening Twilio Console in your browser..."
echo ""

# Open Twilio Console to API Keys page
open "https://console.twilio.com/us1/develop/account/keys-credentials/api-keys"

echo "In the Twilio Console:"
echo "1. If you see an existing API Key, click 'Show' next to it"
echo "2. Copy the 'Account SID' (starts with AC...)"
echo "3. Copy the 'Auth Token' (or 'Secret' if using API Key)"
echo ""
echo "Alternatively, go to Account → Account Info to get:"
echo "  - Account SID (from main account page)"
echo "  - Auth Token (click 'Show' to reveal)"
echo ""
echo "Once you have the credentials, run:"
echo "  python scripts/debug_twilio_sms.py"
echo ""
echo "Or set them in .env file:"
echo "  TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
echo "  TWILIO_AUTH_TOKEN=your_auth_token_here"







