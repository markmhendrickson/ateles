#!/usr/bin/env python3
"""
Fix Actual Misalignments

Fixes specific misalignments visible in the PDF:
1. Postal code in wrong field (Distrito instead of Código postal)
2. Applicant name/NIF appearing below property section
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


def fill_form(template_path: str, data_path: str, positions: dict, output_path: str):
    """Fill form with positions."""
    with open(data_path) as f:
        form_data = json.load(f)

    reader = PdfReader(template_path)
    writer = PdfWriter()

    first_page = reader.pages[0]
    media_box = first_page.mediabox
    width = float(media_box.right) - float(media_box.left)
    height = float(media_box.top) - float(media_box.bottom)

    overlay_path = Path(output_path).parent / "overlay_temp.pdf"
    c = canvas.Canvas(str(overlay_path), pagesize=(width, height))

    pages_data = {}
    for key, value in form_data.items():
        if key in positions:
            x, y, page_idx = positions[key]
            if page_idx not in pages_data:
                pages_data[page_idx] = []
            pages_data[page_idx].append((x, y, value, key))

    for page_idx in sorted(pages_data.keys()):
        if page_idx > 0:
            c.showPage()

        for x, y, value, field_name in pages_data[page_idx]:
            if value is None or value == "":
                continue

            if isinstance(value, bool):
                if value:
                    c.setFont("Helvetica-Bold", 12)
                    c.drawString(float(x), float(y), "✓")
            else:
                value_str = str(value)
                if field_name == "property_additional_info":
                    value_str = "Cambio de construcción a residencia"
                elif field_name == "applicant_capacity":
                    c.setFont("Helvetica-Bold", 10)
                    c.drawString(float(x), float(y), "✓")
                    continue
                elif field_name in ["offer_recipient", "contact_person"]:
                    if value == "peticionario":
                        c.setFont("Helvetica-Bold", 10)
                        c.drawString(float(x), float(y), "✓")
                        continue

                if len(value_str) > 50:
                    value_str = value_str[:47] + "..."

                c.setFont("Helvetica", 10)
                c.drawString(float(x), float(y), value_str)

    c.save()

    overlay_reader = PdfReader(str(overlay_path))
    for i, page in enumerate(reader.pages):
        if i < len(overlay_reader.pages):
            page.merge_page(overlay_reader.pages[i])
        writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)

    overlay_path.unlink()


def main():
    template = "reference/documents/aigues-de-barcelona/Cast-FPT-071-Nueva-o-modificación-instalaciones (1).pdf"
    data = "operations/execution-plans/aigues-de-barcelona-form-data.json"
    positions_file = "operations/execution-plans/aigues-de-barcelona-manual-calibrated-positions.json"
    output = "operations/execution-plans/aigues-de-barcelona-filled-form-corrected.pdf"

    # Load current positions
    with open(positions_file) as f:
        positions = json.load(f)

    # Load form data
    with open(data) as f:
        json.load(f)

    # Get page dimensions
    reader = PdfReader(template)
    page = reader.pages[0]
    media_box = page.mediabox
    height = float(media_box.top) - float(media_box.bottom)

    # Fix specific misalignments based on visual inspection:

    # 1. Postal code is in Distrito field - need to move it RIGHT and possibly UP
    #    Distrito field is around x=400, postal code should be around x=450-470
    if "property_postal_code" in positions:
        # Move postal code to correct position (right of municipality)
        positions["property_postal_code"] = [
            470,
            height - 135,
            0,
        ]  # Right of municipality field

    # 2. Applicant name/NIF appearing below property section - need to move UP significantly
    #    They're currently appearing around the note area, need to be in applicant section
    if "applicant_name" in positions:
        # Move applicant fields UP to proper applicant section
        # Applicant section starts around height - 250 to height - 320
        positions["applicant_name"] = [90, height - 250, 0]  # Top of applicant section
        positions["applicant_nif"] = [400, height - 250, 0]  # Right side, same row

    # 3. Applicant address, municipality, phone, email need proper spacing
    if "applicant_address" in positions:
        positions["applicant_address"] = [90, height - 280, 0]  # Below name
        positions["applicant_municipality"] = [400, height - 280, 0]  # Right side
        positions["applicant_phone"] = [90, height - 310, 0]  # Below address
        positions["applicant_email"] = [
            300,
            height - 310,
            0,
        ]  # Right side, same row as phone

    # 4. Property district should be empty or in correct position
    #    Currently postal code is there, so we need to ensure district is separate
    # District field is left empty (no value in form_data), so no adjustment needed

    # Fill form with corrected positions
    print("Filling form with corrected positions...")
    fill_form(template, data, positions, output)

    # Save corrected positions
    with open(positions_file, "w") as f:
        json.dump(positions, f, indent=2)

    print(f"✓ Output: {output}")
    print(f"✓ Positions saved: {positions_file}")
    print("\nKey fixes applied:")
    print("  1. Postal code moved to correct field (right of municipality)")
    print("  2. Applicant name/NIF moved to proper applicant section")
    print("  3. Applicant fields properly spaced")


if __name__ == "__main__":
    main()
