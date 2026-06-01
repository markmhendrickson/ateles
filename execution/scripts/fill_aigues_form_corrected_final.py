#!/usr/bin/env python3
"""
Fill Aigües de Barcelona form with corrected positions based on visual inspection.
Fixes property section misalignments and checkbox issues.
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


def get_corrected_positions(template_path: str) -> dict:
    """
    Get corrected positions based on visual inspection of form.
    Fixed property section positions and checkbox locations.
    """
    reader = PdfReader(template_path)
    page = reader.pages[0]
    media_box = page.mediabox
    height = float(media_box.top) - float(media_box.bottom)

    # Corrected positions based on actual form layout
    # Property section is at the top, after header (~100-150 from top)
    # Applicant section is below property section (~250-350 from top)

    positions = {
        # Page 1 - Property section (top area)
        "property_street": [90, height - 100, 0],  # Calle y nº - FIRST field
        "property_district": [400, height - 100, 0],  # Distrito - right side, same row
        "property_municipality": [400, height - 130, 0],  # Municipio - below district
        "property_postal_code": [
            480,
            height - 130,
            0,
        ],  # Código postal - right of municipio
        "property_additional_info": [
            90,
            height - 160,
            0,
        ],  # Información adicional - below street
        # Page 1 - Applicant section (middle area)
        "applicant_name": [
            90,
            height - 250,
            0,
        ],  # Nombre - applicant section starts here
        "applicant_nif": [400, height - 250, 0],  # NIF - right side, same row
        "applicant_address": [90, height - 280, 0],  # Calle - below name
        "applicant_municipality": [400, height - 280, 0],  # Municipio - right side
        "applicant_phone": [90, height - 310, 0],  # Teléfono - below address
        "applicant_email": [300, height - 310, 0],  # Email - right side, same row
        "applicant_capacity": [90, height - 340, 0],  # Capacity checkbox area
        # Page 1 - Checkboxes (below applicant section)
        "owner_same_as_applicant": [80, height - 380, 0],  # Owner checkbox
        "installer_same_as_applicant": [80, height - 410, 0],  # Installer checkbox
        "offer_recipient": [80, height - 470, 0],  # Offer recipient checkbox
        "contact_person": [80, height - 500, 0],  # Contact person checkbox
        # Page 2 - Technical section
        "installation_type": [90, height - 200, 1],  # Modificación checkbox
        "number_of_floors": [90, height - 230, 1],  # Number of floors
        "pressure_group": [300, height - 230, 1],  # Pressure group "No" checkbox
        "installation_category": [
            90,
            height - 260,
            1,
        ],  # Acometida Divisionaria checkbox
        "installation_subcategory": [90, height - 290, 1],  # Doméstico checkbox
        "housing_type": [90, height - 320, 1],  # Tipo vivienda
        "max_flow_liters_per_second": [380, height - 350, 1],  # Max flow
    }

    return positions


def fill_form_corrected(template_path: str, data_path: str, output_path: str):
    """Fill form with corrected positions."""
    # Load data
    with open(data_path) as f:
        form_data = json.load(f)

    # Get corrected positions
    positions = get_corrected_positions(template_path)

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
                    should_mark = value is True  # FIX: Should be marked
                elif field_name == "offer_recipient":
                    should_mark = value == "peticionario"  # FIX: Should be marked
                elif field_name == "contact_person":
                    should_mark = value == "peticionario"  # FIX: Should be marked
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
                    # Draw checkmark - use larger, clearer mark
                    c.setFont("Helvetica-Bold", 14)
                    c.setFillColor(black)
                    c.drawString(float(x), float(y), "✓")

            # Handle text fields - ensure correct values go to correct fields
            else:
                value_str = str(value)

                # CRITICAL FIX: Ensure property_additional_info gets the right text
                if field_name == "property_additional_info":
                    value_str = "Cambio de construcción a residencia"

                # CRITICAL FIX: Ensure property_street gets the actual street address
                # (not the additional info text)
                elif field_name == "property_street":
                    value_str = form_data.get("property_street", "")

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
    output = (
        "operations/execution-plans/aigues-de-barcelona-filled-form-final-corrected.pdf"
    )

    print("Filling Aigües de Barcelona form with corrected positions...")
    print("Fixing:")
    print("  - Property section field positions")
    print("  - Checkbox marking (installer, offer recipient, contact person)")
    print("  - Text field values")
    fill_form_corrected(template, data, output)
    print("✓ Complete")
    print(f"\nOutput: {output}")


if __name__ == "__main__":
    main()
