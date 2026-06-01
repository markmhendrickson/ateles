#!/usr/bin/env python3
"""
Fill Aigües de Barcelona form with adjusted positions to fix alignment issues.
Uses fine-tuned positions based on common alignment problems.
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


def get_adjusted_positions(template_path: str, base_positions: dict) -> dict:
    """
    Get adjusted positions with fine-tuning for common alignment issues.
    Adjustments based on typical PDF coordinate system and form layout.
    """
    reader = PdfReader(template_path)
    page = reader.pages[0]
    media_box = page.mediabox
    height = float(media_box.top) - float(media_box.bottom)

    # Start with base positions
    adjusted = base_positions.copy()

    # Fine-tune positions based on form structure
    # Y coordinates: PDF uses bottom-left origin, higher Y = higher on page
    # Adjustments: move fields slightly to better align with form fields

    # Property section adjustments (top of page 1)
    if "property_street" in adjusted:
        adjusted["property_street"][1] = height - 100  # Slightly higher
    if "property_district" in adjusted:
        adjusted["property_district"][1] = height - 100
    if "property_municipality" in adjusted:
        adjusted["property_municipality"][1] = height - 130
    if "property_postal_code" in adjusted:
        adjusted["property_postal_code"][0] = 480  # Move right
        adjusted["property_postal_code"][1] = height - 130
    if "property_additional_info" in adjusted:
        adjusted["property_additional_info"][1] = height - 160  # Below municipality

    # Applicant section adjustments (middle of page 1)
    if "applicant_name" in adjusted:
        adjusted["applicant_name"][1] = height - 250  # Ensure it's in applicant section
    if "applicant_nif" in adjusted:
        adjusted["applicant_nif"][1] = height - 250  # Same row as name
    if "applicant_address" in adjusted:
        adjusted["applicant_address"][1] = height - 280
    if "applicant_municipality" in adjusted:
        adjusted["applicant_municipality"][1] = height - 280
    if "applicant_phone" in adjusted:
        adjusted["applicant_phone"][1] = height - 310
    if "applicant_email" in adjusted:
        adjusted["applicant_email"][1] = height - 310
    if "applicant_capacity" in adjusted:
        adjusted["applicant_capacity"][1] = height - 340  # Checkbox area

    # Checkbox positions (below applicant section)
    if "owner_same_as_applicant" in adjusted:
        adjusted["owner_same_as_applicant"][1] = height - 380
    if "installer_same_as_applicant" in adjusted:
        adjusted["installer_same_as_applicant"][1] = height - 410
    if "offer_recipient" in adjusted:
        adjusted["offer_recipient"][1] = height - 470
    if "contact_person" in adjusted:
        adjusted["contact_person"][1] = height - 500

    # Page 2 - Technical section
    if "installation_type" in adjusted:
        adjusted["installation_type"][1] = height - 200  # Top of page 2
    if "number_of_floors" in adjusted:
        adjusted["number_of_floors"][1] = height - 230
    if "pressure_group" in adjusted:
        adjusted["pressure_group"][1] = height - 230  # Same row
    if "installation_category" in adjusted:
        adjusted["installation_category"][1] = height - 260
    if "installation_subcategory" in adjusted:
        adjusted["installation_subcategory"][1] = height - 290
    if "housing_type" in adjusted:
        adjusted["housing_type"][1] = height - 320
    if "max_flow_liters_per_second" in adjusted:
        adjusted["max_flow_liters_per_second"][1] = height - 350

    return adjusted


def fill_form_adjusted(
    template_path: str, data_path: str, positions_path: str, output_path: str
):
    """Fill form with adjusted positions."""
    # Load data
    with open(data_path) as f:
        form_data = json.load(f)

    # Load base positions
    with open(positions_path) as f:
        base_positions = json.load(f)

    # Get adjusted positions
    adjusted_positions = get_adjusted_positions(template_path, base_positions)

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
        if field_name not in adjusted_positions:
            continue

        x, y, page_idx = adjusted_positions[field_name]
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

            # Checkbox fields
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
                    should_mark = value is False
                elif field_name == "installation_type":
                    should_mark = value == "Modificación instalaciones existentes"
                elif field_name == "installation_category":
                    should_mark = value == "Acometida Divisionaria"
                elif field_name == "installation_subcategory":
                    should_mark = value == "Doméstico"

                if should_mark:
                    c.setFont("Helvetica-Bold", 12)
                    c.setFillColor(black)
                    c.drawString(float(x), float(y), "✓")

            # Handle text fields
            else:
                value_str = str(value)

                if field_name == "property_additional_info":
                    value_str = "Cambio de construcción a residencia"

                if len(value_str) > 60:
                    value_str = value_str[:57] + "..."

                c.setFont("Helvetica", 10)
                c.setFillColor(black)
                c.drawString(float(x), float(y), value_str)

    c.save()

    # Merge overlay
    overlay_reader = PdfReader(str(overlay_path))
    for i, page in enumerate(reader.pages):
        if i < len(overlay_reader.pages):
            page.merge_page(overlay_reader.pages[i])
        writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)

    overlay_path.unlink()
    print(f"✓ Form filled: {output_path}")


def main():
    template = "reference/documents/aigues-de-barcelona/Cast-FPT-071-Nueva-o-modificación-instalaciones (1).pdf"
    data = "operations/execution-plans/aigues-de-barcelona-form-data.json"
    positions = "operations/execution-plans/aigues-de-barcelona-manual-calibrated-positions.json"
    output = "operations/execution-plans/aigues-de-barcelona-filled-form-adjusted.pdf"

    print("Filling form with adjusted positions...")
    fill_form_adjusted(template, data, positions, output)
    print("✓ Complete")


if __name__ == "__main__":
    main()
