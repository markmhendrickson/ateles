#!/usr/bin/env python3
"""
Fix PDF Form Alignment Issues

Analyzes filled PDF and fixes alignment problems based on visual inspection.
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


def fix_alignment_issues(template_path, data_path, filled_path, output_path):
    """Fix alignment issues in filled PDF."""

    # Load form data
    with open(data_path) as f:
        form_data = json.load(f)

    reader = PdfReader(template_path)
    writer = PdfWriter()

    # Get page dimensions
    first_page = reader.pages[0]
    media_box = first_page.mediabox
    width = float(media_box.right) - float(media_box.left)
    height = float(media_box.top) - float(media_box.bottom)

    # Corrected field positions based on visual inspection
    # Using A4 dimensions: 595.3 x 841.9 points
    # Y coordinates from top (PDF uses bottom-left origin, so we flip)

    corrected_positions = {
        # Page 1 - Property section (top)
        "property_street": [90, height - 100, 0],
        "property_district": [400, height - 100, 0],
        "property_municipality": [400, height - 130, 0],
        "property_postal_code": [450, height - 130, 0],
        "property_additional_info": [90, height - 160, 0],
        # Page 1 - Applicant section (middle)
        "applicant_name": [90, height - 250, 0],
        "applicant_nif": [400, height - 250, 0],
        "applicant_address": [90, height - 280, 0],
        "applicant_municipality": [400, height - 280, 0],
        "applicant_phone": [90, height - 310, 0],
        "applicant_email": [300, height - 310, 0],
        "applicant_capacity": [90, height - 340, 0],  # Checkbox area
        # Page 1 - Checkboxes
        "owner_same_as_applicant": [80, height - 380, 0],
        "installer_same_as_applicant": [80, height - 410, 0],
        "offer_recipient": [80, height - 470, 0],  # "Coinciden con peticionario/a"
        "contact_person": [80, height - 500, 0],  # "Coinciden con peticionario/a"
        # Page 2 - Technical section
        "installation_type": [
            90,
            height - 200,
            1,
        ],  # "Modificación instalaciones existentes" checkbox
        "number_of_floors": [90, height - 230, 1],
        "pressure_group": [300, height - 230, 1],  # "No" checkbox
        "installation_category": [
            90,
            height - 260,
            1,
        ],  # "Acometida Divisionaria" checkbox
        "installation_subcategory": [90, height - 290, 1],  # "Doméstico" checkbox
        "housing_type": [90, height - 320, 1],  # "Casa"
        "max_flow_liters_per_second": [380, height - 350, 1],
    }

    # Create overlay with corrected positions
    output_path_obj = Path(output_path)
    overlay_path = output_path_obj.parent / f"{output_path_obj.stem}_overlay_fixed.pdf"
    c = canvas.Canvas(str(overlay_path), pagesize=(width, height))
    c.setFont("Helvetica", 10)

    # Group by page
    pages_data = {}
    for key, value in form_data.items():
        if key in corrected_positions:
            x, y, page_idx = corrected_positions[key]
            if page_idx not in pages_data:
                pages_data[page_idx] = []
            pages_data[page_idx].append((x, y, value, key))

    # Fill each page
    for page_idx in sorted(pages_data.keys()):
        if page_idx > 0:
            c.showPage()

        for x, y, value, field_name in pages_data[page_idx]:
            if value is None or value == "":
                continue

            # Handle boolean values (checkboxes) - draw "X" or checkmark
            if isinstance(value, bool):
                if value:
                    # Draw checkmark or X for checkbox
                    c.setFont("Helvetica-Bold", 12)
                    c.drawString(float(x), float(y), "✓")
            else:
                # Handle text values
                value_str = str(value)
                # Special handling for certain fields
                if field_name == "property_additional_info":
                    value_str = "Cambio de construcción a residencia"
                elif field_name == "applicant_capacity":
                    # This is a checkbox, mark it
                    c.setFont("Helvetica-Bold", 10)
                    c.drawString(float(x), float(y), "✓")
                    continue
                elif field_name in ["offer_recipient", "contact_person"]:
                    # These should mark the "peticionario" checkbox
                    c.setFont("Helvetica-Bold", 10)
                    c.drawString(float(x), float(y), "✓")
                    continue

                # Truncate if too long
                if len(value_str) > 50:
                    value_str = value_str[:47] + "..."

                c.setFont("Helvetica", 10)
                c.drawString(float(x), float(y), value_str)

    c.save()

    # Merge with original
    overlay_reader = PdfReader(str(overlay_path))
    for i, page in enumerate(reader.pages):
        if i < len(overlay_reader.pages):
            page.merge_page(overlay_reader.pages[i])
        writer.add_page(page)

    # Write output
    with open(output_path, "wb") as output_file:
        writer.write(output_file)

    # Clean up
    overlay_path.unlink()

    print(f"✓ Fixed alignment issues: {output_path}")
    return True


def main():
    template = "reference/documents/aigues-de-barcelona/Cast-FPT-071-Nueva-o-modificación-instalaciones (1).pdf"
    data = "operations/execution-plans/aigues-de-barcelona-form-data.json"
    filled = "operations/execution-plans/aigues-de-barcelona-filled-form-auto.pdf"
    output = "operations/execution-plans/aigues-de-barcelona-filled-form-fixed.pdf"

    fix_alignment_issues(template, data, filled, output)


if __name__ == "__main__":
    main()
