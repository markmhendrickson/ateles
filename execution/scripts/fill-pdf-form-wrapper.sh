#!/bin/bash
# Wrapper script for PDF Form Filler that handles virtual environment

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/../.venv-pdf-filler"

# Activate virtual environment if it exists
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
else
    echo "Error: Virtual environment not found. Run setup first:"
    echo "  ./scripts/setup-pdf-filler.sh"
    exit 1
fi

# Run the Python script with all arguments
python3 "$SCRIPT_DIR/fill-pdf-form.py" "$@"





