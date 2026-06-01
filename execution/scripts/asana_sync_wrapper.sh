#!/bin/bash
# Wrapper script for Asana task sync service
# This script activates the virtual environment and runs the sync daemon
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

# Run the sync script in daemon mode (default 60s interval)
exec python3 "$SCRIPT_DIR/sync_asana_tasks.py" --daemon --interval 60 "$@"








