# Generic PDF Form Filler Guide

Universal solution for filling any static PDF form automatically. Works with any static PDF form (forms without fillable fields). Uses OCR and image processing to detect fields, then fills them using text overlay.

## Installation

```bash
# Install Python dependencies
pip install pdf2image opencv-python pillow pytesseract pypdf reportlab

# Install system dependencies (macOS)
brew install poppler tesseract

# Optional: Install PyMuPDF for better coordinate extraction
pip install pymupdf
```

## Workflow

### Step 1: Detect Fields (One-time per template)

Detect form fields in your PDF template:

```bash
python scripts/generic-pdf-form-filler.py detect \
    --template path/to/form.pdf \
    --output positions.json \
    --method hybrid
```

**Methods:**
- `hybrid` (default): Combines line detection + OCR
- `lines`: Detects field underlines only
- `ocr`: Uses OCR to find labels only

**Output:** `positions.json` with detected field positions

### Step 2: Map Fields to Data Keys

Edit `positions.json` to map detected field keys to your data keys:

```json
{
  "applicant_name": [90, 600, 0],
  "applicant_email": [300, 570, 0],
  "property_address": [90, 700, 0]
}
```

**Format:** `[x, y, page_number]` where:
- `x`: Horizontal position in PDF points
- `y`: Vertical position in PDF points (from bottom)
- `page_number`: 0-indexed page number

### Step 3: Prepare Form Data

Create a JSON file with your form data:

```json
{
  "applicant_name": "John Doe",
  "applicant_email": "john@example.com",
  "property_address": "123 Main St"
}
```

**Data types supported:**
- Strings: Displayed as text
- Booleans (`true`/`false`): Displayed as checkmark (✓)
- Empty strings: Skipped

### Step 4: Fill Form

Fill the form with your data:

```bash
python scripts/generic-pdf-form-filler.py fill \
    --template path/to/form.pdf \
    --data data.json \
    --positions positions.json \
    --output filled.pdf
```

### Step 5: Auto-Fix Alignment (Optional)

If alignment isn't perfect, use auto-fix:

```bash
python scripts/generic-pdf-form-filler.py auto-fix \
    --template path/to/form.pdf \
    --data data.json \
    --positions positions.json \
    --output filled.pdf \
    --target 0.90 \
    --iterations 20
```

This will:
1. Fill form with current positions
2. Detect actual text positions using OCR
3. Calculate corrections
4. Iterate until target alignment reached
5. Save improved positions

## Advanced Usage

### Multi-page Forms

For multi-page forms, detect fields on each page:

```bash
# Page 1
python scripts/generic-pdf-form-filler.py detect \
    --template form.pdf --output page1_positions.json --page 0

# Page 2
python scripts/generic-pdf-form-filler.py detect \
    --template form.pdf --output page2_positions.json --page 1
```

Then merge position files manually or in your data mapping step.

### Custom Font Sizes

Adjust font size for better fit:

```bash
python scripts/generic-pdf-form-filler.py fill \
    --template form.pdf \
    --data data.json \
    --positions positions.json \
    --output filled.pdf \
    --font-size 12
```

### Manual Position Calibration

If auto-detection doesn't work well:

1. Open PDF in a PDF viewer
2. Note field positions (you can use PDF coordinate tools)
3. Manually create/edit `positions.json` with exact coordinates
4. Use `fill` command with manual positions

## Troubleshooting

### Fields Not Detected

**Problem:** No fields detected during detection step.

**Solutions:**
1. Try different detection method: `--method ocr` or `--method lines`
2. Increase PDF resolution (edit script to use higher DPI)
3. Manually create positions file
4. Check PDF quality - scanned PDFs may need preprocessing

### Poor Alignment

**Problem:** Filled text doesn't align with form fields.

**Solutions:**
1. Run `auto-fix` command to iteratively improve
2. Manually adjust positions in JSON file
3. Check coordinate system (PDF uses bottom-left origin)
4. Verify font size matches form field size

### OCR Errors

**Problem:** OCR fails or produces incorrect text.

**Solutions:**
1. Install language packs: `brew install tesseract-lang` (macOS)
2. Download custom language files to `$DATA_DIR/tesseract/`:
   ```bash
   python execution/scripts/setup_tesseract_languages.py --languages eng spa
   ```
3. Set `TESSDATA_PREFIX` environment variable (scripts auto-configure if files exist in `$DATA_DIR/tesseract/`)
4. Use `--method lines` to skip OCR
5. Preprocess PDF images (increase contrast, remove noise)

## Best Practices

1. **One-time calibration:** Detect/calibrate positions once per template, reuse for all fills
2. **Save positions:** Keep `positions.json` files organized by template
3. **Validate output:** Always review filled PDFs before submission
4. **Version control:** Track position files in git for templates you use frequently
5. **Test with sample data:** Test with sample data before using real data

## Integration

### Python API

```python
from scripts.generic_pdf_form_filler import GenericPDFFormFiller

# Initialize
filler = GenericPDFFormFiller("form.pdf")

# Detect fields
positions = filler.detect_fields(page_num=0, method='hybrid')

# Fill form
data = {"field1": "value1", "field2": "value2"}
filler.fill_form(data, positions, "output.pdf")

# Validate
results, score = filler.validate_alignment("output.pdf", positions, data)
```

### Batch Processing

```bash
# Process multiple forms
for form in forms/*.pdf; do
    python scripts/generic-pdf-form-filler.py fill \
        --template "$form" \
        --data data.json \
        --positions "${form%.pdf}_positions.json" \
        --output "filled_$(basename $form)"
done
```

## Limitations

1. **Static PDFs only:** Doesn't work with fillable PDF forms (use `pypdf` form fields instead)
2. **Text overlay:** Uses text overlay, may not match original font exactly
3. **OCR dependency:** Field detection requires OCR, may fail on low-quality PDFs
4. **Manual mapping:** Requires manual mapping of detected fields to data keys

## Examples

See `operations/execution-plans/` for real-world examples:
- Aigües de Barcelona form filling
- Position calibration workflows
- Data mapping examples







