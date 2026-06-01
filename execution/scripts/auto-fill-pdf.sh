#!/bin/bash
#
# Automated PDF Form Filler Wrapper
#
# Automatically fills PDF forms with form data.
# This is the main entry point for PDF form automation.
#
# Usage:
#   ./scripts/auto-fill-pdf.sh --template form.pdf --data data.json --output filled.pdf
#   ./scripts/auto-fill-pdf.sh --template form.pdf --data data.json  # auto-generates output name
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PATH="$SCRIPT_DIR/../.venv-pdf-filler"

# Activate virtual environment
if [ ! -d "$VENV_PATH" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_PATH"
    source "$VENV_PATH/bin/activate"
    pip install --quiet pypdf reportlab
else
    source "$VENV_PATH/bin/activate"
fi

# Parse arguments
TEMPLATE=""
DATA=""
OUTPUT=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --template)
            TEMPLATE="$2"
            shift 2
            ;;
        --data)
            DATA="$2"
            shift 2
            ;;
        --output)
            OUTPUT="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Validate required arguments
if [ -z "$TEMPLATE" ] || [ -z "$DATA" ]; then
    echo "Usage: $0 --template <pdf> --data <json> [--output <pdf>]"
    exit 1
fi

# Auto-generate output name if not provided
if [ -z "$OUTPUT" ]; then
    TEMPLATE_BASENAME=$(basename "$TEMPLATE" .pdf)
    DATA_DIR=$(dirname "$DATA")
    OUTPUT="$DATA_DIR/${TEMPLATE_BASENAME}-filled.pdf"
fi

# Make paths absolute
TEMPLATE=$(cd "$(dirname "$TEMPLATE")" && pwd)/$(basename "$TEMPLATE")
DATA=$(cd "$(dirname "$DATA")" && pwd)/$(basename "$DATA")
OUTPUT=$(cd "$(dirname "$OUTPUT")" && pwd)/$(basename "$OUTPUT")

# Run automation
cd "$SCRIPT_DIR"
python3 auto-fill-pdf-complete.py \
    --template "$TEMPLATE" \
    --data "$DATA" \
    --output "$OUTPUT"

# Open filled PDF
if [ -f "$OUTPUT" ]; then
    echo ""
    echo "Opening filled PDF..."
    open "$OUTPUT"
fi

deactivate







