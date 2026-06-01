#!/bin/bash
# Setup script for PDF Form Filler

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/../.venv-pdf-filler"

echo "Setting up PDF Form Filler..."

# Create virtual environment
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# Activate virtual environment
source "$VENV_DIR/bin/activate"

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip
pip install pypdf reportlab

echo ""
echo "Setup complete!"
echo ""
echo "To use the PDF filler:"
echo "  source $VENV_DIR/bin/activate"
echo "  python3 scripts/fill-pdf-form.py --help"
echo ""
echo "Or use the wrapper script:"
echo "  ./scripts/fill-pdf-form-wrapper.sh --help"





