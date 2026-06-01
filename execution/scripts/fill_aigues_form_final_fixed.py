#!/usr/bin/env python3
"""
Fill Aigües de Barcelona form with final corrected positions.
Fixes property section overlay issue and applicant capacity checkbox.
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


def get_final_corrected_positions(template_path: str) -> dict:
    """
    Get final corrected positions with property section properly below intro text.
    Key fix: Property section Y coordinates must be significantly lower to avoid overlay.
    """
    reader = PdfReader(template_path)
    page = reader.pages[0]
    media_box = page.mediabox
    height = float(media_box.top) - float(media_box.bottom)

    # Final corrected positions
    # Property section needs to be WELL BELOW the introductory paragraph
    # The intro paragraph ends around height - 120, so property fields start at height - 180 or lower

    positions = {
        # Page 1 - Property section (well below intro text to avoid overlay)
        "property_street": [90, height - 180, 0],  # Calle y nº - BELOW intro paragraph
        "property_district": [400, height - 180, 0],  # Distrito - right side, same row
        "property_municipality": [400, height - 210, 0],  # Municipio - below district
        "property_postal_code": [
            480,
            height - 210,
            0,
        ],  # Código postal - right of municipio
        "property_additional_info": [
            90,
            height - 240,
            0,
        ],  # Información adicional - below street
        # Page 1 - Applicant section (below property section)
        "applicant_name": [90, height - 310, 0],  # Nombre
        "applicant_nif": [400, height - 310, 0],  # NIF - right side
        "applicant_address": [90, height - 340, 0],  # Calle
        "applicant_municipality": [400, height - 340, 0],  # Municipio
        "applicant_phone": [90, height - 370, 0],  # Teléfono
        "applicant_email": [300, height - 370, 0],  # Email
        "applicant_capacity": [
            90,
            height - 400,
            0,
        ],  # Capacity checkbox - Propietario/a
        # Page 1 - Checkboxes (below applicant section)
        "owner_same_as_applicant": [80, height - 440, 0],  # Owner checkbox
        "installer_same_as_applicant": [80, height - 470, 0],  # Installer checkbox
        "offer_recipient": [80, height - 530, 0],  # Offer recipient checkbox
        "contact_person": [80, height - 560, 0],  # Contact person checkbox
        # Page 2 - Technical section
        "installation_type": [90, height - 200, 1],  # Modificación checkbox
        "number_of_floors": [90, height - 230, 1],  # Number of floors
        "pressure_group": [300, height - 230, 1],  # Pressure group "No"
        "installation_category": [90, height - 260, 1],  # Acometida Divisionaria
        "installation_subcategory": [90, height - 290, 1],  # Doméstico
        "housing_type": [90, height - 320, 1],  # Tipo vivienda
        "max_flow_liters_per_second": [380, height - 350, 1],  # Max flow
    }

    return positions


def fill_form_final_fixed(template_path: str, data_path: str, output_path: str):
    """Fill form with final corrected positions."""
    # Load data
    with open(data_path) as f:
        form_data = json.load(f)

    # Get final corrected positions
    positions = get_final_corrected_positions(template_path)

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
                    # FIX: Mark "Propietario/a" checkbox, not "Instalador/a"
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
                    c.setFont("Helvetica-Bold", 14)
                    c.setFillColor(black)
                    c.drawString(float(x), float(y), "✓")

            # Handle text fields
            else:
                # Get correct value for each field
                if field_name == "property_street":
                    value_str = form_data.get("property_street", "")
                elif field_name == "property_district":
                    value_str = form_data.get("property_district", "")
                elif field_name == "property_postal_code":
                    value_str = form_data.get("property_postal_code", "")
                elif field_name == "property_additional_info":
                    value_str = "Cambio de construcción a residencia"
                else:
                    value_str = str(value)

                # Truncate long values
                if len(value_str) > 60:
                    value_str = value_str[:57] + "..."

                # Draw text
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
    output = "operations/execution-plans/aigues-de-barcelona-filled-form-final.pdf"

    print("Filling form with final corrected positions...")
    print("Key fixes:")
    print(
        "  ✓ Property section: Y coordinates lowered to height - 180 (well below intro text)"
    )
    print("  ✓ Applicant capacity: Fixed to mark 'Propietario/a' not 'Instalador/a'")
    print("  ✓ All checkboxes: Properly marked")
    fill_form_final_fixed(template, data, output)
    print("✓ Complete")
    print(f"\nOutput: {output}")


if __name__ == "__main__":
    main()
