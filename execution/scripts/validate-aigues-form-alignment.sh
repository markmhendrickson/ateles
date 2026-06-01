#!/bin/bash
# Helper script to validate Aigües de Barcelona form alignment using Vision API

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Paths
BLANK_FORM="$PROJECT_ROOT/reference/documents/aigues-de-barcelona/Cast-FPT-071-Nueva-o-modificación-instalaciones (1).pdf"
FILLED_FORM="$PROJECT_ROOT/operations/execution-plans/aigues-de-barcelona-filled-form-auto.pdf"
FORM_DATA="$PROJECT_ROOT/operations/execution-plans/aigues-de-barcelona-form-data.json"
EXPECTED_POSITIONS="$PROJECT_ROOT/operations/execution-plans/aigues-de-barcelona-manual-calibrated-positions.json"
FIELD_LABELS="$PROJECT_ROOT/operations/execution-plans/aigues-de-barcelona-field-labels.json"
OUTPUT_REPORT="$PROJECT_ROOT/operations/execution-plans/aigues-de-barcelona-vision-alignment-report.json"

# Check if files exist
if [ ! -f "$BLANK_FORM" ]; then
    echo "Error: Blank form not found at $BLANK_FORM"
    exit 1
fi

if [ ! -f "$FILLED_FORM" ]; then
    echo "Error: Filled form not found at $FILLED_FORM"
    echo "Please run the form filling script first"
    exit 1
fi

if [ ! -f "$FORM_DATA" ]; then
    echo "Error: Form data not found at $FORM_DATA"
    exit 1
fi

# Run validation
echo "Validating Aigües de Barcelona form alignment using Vision API..."
echo ""

python "$SCRIPT_DIR/vision_pdf_alignment_validator.py" \
    --blank "$BLANK_FORM" \
    --filled "$FILLED_FORM" \
    --data "$FORM_DATA" \
    --positions "$EXPECTED_POSITIONS" \
    --labels "$FIELD_LABELS" \
    --output "$OUTPUT_REPORT" \
    --tolerance 30.0

echo ""
echo "Validation complete. Report saved to: $OUTPUT_REPORT"

