#!/usr/bin/env python3
"""
Fill Aigües de Barcelona form using manually calibrated positions.
"""

import json
import sys
from pathlib import Path

try:
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
except ImportError:
    print("Error: Missing required library. Install with: pip install pypdf reportlab")
    sys.exit(1)


def fill_form_with_positions(
    template_path: str, data_path: str, positions_path: str, output_path: str
):
    """Fill form using pre-calibrated positions."""
    # Load data
    with open(data_path) as f:
        form_data = json.load(f)

    # Load positions
    with open(positions_path) as f:
        positions = json.load(f)

    # Read template
    reader = PdfReader(template_path)
    writer = PdfWriter()

    # Get page dimensions
    first_page = reader.pages[0]
    media_box = first_page.mediabox
    width = float(media_box.right) - float(media_box.left)
    height = float(media_box.top) - float(media_box.bottom)

    # Create overlay
    overlay_path = Path(output_path).parent / "overlay_temp.pdf"
    c = canvas.Canvas(str(overlay_path), pagesize=(width, height))

    # Group fields by page
    pages_data = {}
    for field_name, value in form_data.items():
        if field_name not in positions:
            continue

        x, y, page_idx = positions[field_name]
        if page_idx not in pages_data:
            pages_data[page_idx] = []
        pages_data[page_idx].append((x, y, value, field_name))

    # Fill each page
    for page_idx in sorted(pages_data.keys()):
        if page_idx > 0:
            c.showPage()

        for x, y, value, field_name in pages_data[page_idx]:
            if value is None or value == "":
                continue

            # Handle checkboxes
            if isinstance(value, bool):
                if value:
                    c.setFont("Helvetica-Bold", 12)
                    c.drawString(float(x), float(y), "✓")
            elif field_name == "applicant_capacity":
                # Mark Propietario/a checkbox
                c.setFont("Helvetica-Bold", 10)
                c.drawString(float(x), float(y), "✓")
            elif field_name in [
                "owner_same_as_applicant",
                "installer_same_as_applicant",
            ]:
                if value:
                    c.setFont("Helvetica-Bold", 10)
                    c.drawString(float(x), float(y), "✓")
            elif field_name in ["offer_recipient", "contact_person"]:
                if value == "peticionario":
                    c.setFont("Helvetica-Bold", 10)
                    c.drawString(float(x), float(y), "✓")
            else:
                # Text field
                value_str = str(value)

                # Special handling for specific fields
                if field_name == "property_additional_info":
                    value_str = "Cambio de construcción a residencia"
                elif field_name == "installation_type":
                    # This is a checkbox, not text field
                    if value == "Modificación instalaciones existentes":
                        c.setFont("Helvetica-Bold", 10)
                        c.drawString(float(x), float(y), "✓")
                        continue

                # Truncate long values
                if len(value_str) > 50:
                    value_str = value_str[:47] + "..."

                c.setFont("Helvetica", 10)
                c.drawString(float(x), float(y), value_str)

    c.save()

    # Merge overlay with template
    overlay_reader = PdfReader(str(overlay_path))
    for i, page in enumerate(reader.pages):
        if i < len(overlay_reader.pages):
            page.merge_page(overlay_reader.pages[i])
        writer.add_page(page)

    # Write output
    with open(output_path, "wb") as f:
        writer.write(f)

    # Cleanup
    overlay_path.unlink()

    print(f"✓ Form filled: {output_path}")


def main():
    template = "reference/documents/aigues-de-barcelona/Cast-FPT-071-Nueva-o-modificación-instalaciones (1).pdf"
    data = "operations/execution-plans/aigues-de-barcelona-form-data.json"
    positions = "operations/execution-plans/aigues-de-barcelona-manual-calibrated-positions.json"
    output = "operations/execution-plans/aigues-de-barcelona-filled-form-corrected.pdf"

    print("Filling Aigües de Barcelona form with manually calibrated positions...")
    fill_form_with_positions(template, data, positions, output)
    print("✓ Complete")


if __name__ == "__main__":
    main()
