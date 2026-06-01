#!/bin/bash
# Wrapper script for Twilio SMS webhook server
# Activates virtual environment and runs the webhook server

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

# Activate virtual environment
if [ -f "execution/venv/bin/activate" ]; then
    source execution/venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Run webhook server
exec python scripts/twilio_sms_webhook_server.py --port 8081






