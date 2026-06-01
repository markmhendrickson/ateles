#!/usr/bin/env python3
"""
Fill Aigües de Barcelona form with corrected positions and proper checkbox handling.
"""

import json
import sys
from pathlib import Path

try:
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.colors import black
    from reportlab.pdfgen import canvas
except ImportError:
    print("Error: Missing required library. Install with: pip install pypdf reportlab")
    sys.exit(1)


def fill_form_corrected(
    template_path: str, data_path: str, positions_path: str, output_path: str
):
    """Fill form with corrected positions and proper field handling."""
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

            # Define checkbox fields
            checkbox_fields = [
                "owner_same_as_applicant",
                "installer_same_as_applicant",
                "offer_recipient",
                "contact_person",
                "applicant_capacity",
                "pressure_group",
                "installation_type",
                "installation_category",
                "installation_subcategory",
            ]

            # Handle checkboxes
            if field_name in checkbox_fields:
                should_mark = False

                if field_name == "owner_same_as_applicant":
                    should_mark = value is True
                elif field_name == "installer_same_as_applicant":
                    should_mark = value is True
                elif field_name == "offer_recipient":
                    should_mark = value == "peticionario"
                elif field_name == "contact_person":
                    should_mark = value == "peticionario"
                elif field_name == "applicant_capacity":
                    should_mark = value == "Propietario/a"
                elif field_name == "pressure_group":
                    should_mark = value is False  # "No" checkbox
                elif field_name == "installation_type":
                    should_mark = value == "Modificación instalaciones existentes"
                elif field_name == "installation_category":
                    should_mark = value == "Acometida Divisionaria"
                elif field_name == "installation_subcategory":
                    should_mark = value == "Doméstico"

                if should_mark:
                    # Draw checkmark - adjust position slightly for better alignment
                    c.setFont("Helvetica-Bold", 12)
                    c.setFillColor(black)
                    # Position checkmark in checkbox (slightly offset for visual alignment)
                    c.drawString(float(x), float(y), "✓")

            # Handle text fields
            else:
                value_str = str(value)

                # Special handling for specific text fields
                if field_name == "property_additional_info":
                    value_str = "Cambio de construcción a residencia"

                # Truncate very long values
                if len(value_str) > 60:
                    value_str = value_str[:57] + "..."

                # Set font and draw text
                c.setFont("Helvetica", 10)
                c.setFillColor(black)
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
    output = "operations/execution-plans/aigues-de-barcelona-filled-form-final.pdf"

    print("Filling Aigües de Barcelona form with corrected positions...")
    print("Using manually calibrated positions for accurate alignment...")
    fill_form_corrected(template, data, positions, output)
    print("✓ Complete")
    print(f"\nOutput: {output}")


if __name__ == "__main__":
    main()
