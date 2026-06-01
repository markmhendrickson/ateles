#!/bin/bash
# Wrapper script for Asana webhook server
# This script activates the virtual environment and runs the webhook server
# Used by LaunchAgent for automatic startup

# Get script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_DIR="$PROJECT_ROOT/execution/venv"

# Change to project root
cd "$PROJECT_ROOT"

# Activate virtual environment if it exists
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
else
    echo "Warning: Virtual environment not found at $VENV_DIR" >&2
    echo "Falling back to system Python" >&2
fi

# Run the webhook server
exec python3 "$SCRIPT_DIR/asana_webhook_server.py" --port 8080








