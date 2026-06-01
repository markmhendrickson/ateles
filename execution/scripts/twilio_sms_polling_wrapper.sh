#!/bin/bash
# Wrapper script for Twilio SMS polling
# Activates virtual environment and runs the polling script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

# Activate virtual environment
if [ -f "execution/venv/bin/activate" ]; then
    source execution/venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Run polling script (fetch last 24 hours, catch any missed messages)
exec python scripts/poll_twilio_messages.py --hours 24 --phone-number +16503198857






