#!/usr/bin/env python3
"""
Fix Alignment Using Detected Positions

Uses detected positions as base, then adjusts based on visual alignment feedback.
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


def load_visual_checker():
    """Load visual alignment checker function."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "visual_checker", "scripts/visual-alignment-checker.py"
    )
    visual_checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(visual_checker)
    return visual_checker.check_visual_alignment


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
    detected_positions_file = (
        "operations/execution-plans/aigues-de-barcelona-complete-positions.json"
    )
    output = (
        "operations/execution-plans/aigues-de-barcelona-filled-form-90percent-final.pdf"
    )

    # Load detected positions
    with open(detected_positions_file) as f:
        detected_positions = json.load(f)

    # Load form data to get all fields
    with open(data) as f:
        form_data = json.load(f)

    # Get page dimensions
    reader = PdfReader(template)
    page = reader.pages[0]
    media_box = page.mediabox
    height = float(media_box.top) - float(media_box.bottom)

    # Start with detected positions, add missing ones with estimates
    positions = detected_positions.copy()

    # Add missing fields with intelligent estimates
    # Based on visual inspection, applicant fields need to move UP
    if "applicant_name" in detected_positions:
        # Adjust applicant fields upward (they're too low)
        base_y = detected_positions["applicant_name"][1]
        positions["applicant_name"] = [
            detected_positions["applicant_name"][0],
            base_y + 80,
            0,
        ]  # Move up 80
        positions["applicant_nif"] = [
            detected_positions["applicant_nif"][0],
            base_y + 80,
            0,
        ]

    if "applicant_phone" in detected_positions:
        base_y = detected_positions["applicant_phone"][1]
        positions["applicant_phone"] = [
            detected_positions["applicant_phone"][0],
            base_y + 80,
            0,
        ]
        positions["applicant_email"] = [
            detected_positions["applicant_email"][0],
            base_y + 80,
            0,
        ]

    # Add all missing fields
    missing_fields = {
        "property_postal_code": [450, height - 130, 0],
        "property_additional_info": [90, height - 160, 0],
        "applicant_capacity": [90, height - 340, 0],
        "owner_same_as_applicant": [80, height - 380, 0],
        "installer_same_as_applicant": [80, height - 410, 0],
        "offer_recipient": [80, height - 470, 0],
        "contact_person": [80, height - 500, 0],
        "installation_type": [90, height - 200, 1],
        "number_of_floors": [90, height - 230, 1],
        "pressure_group": [300, height - 230, 1],
        "installation_category": [90, height - 260, 1],
        "installation_subcategory": [90, height - 290, 1],
        "housing_type": [90, height - 320, 1],
        "max_flow_liters_per_second": [380, height - 350, 1],
    }

    for key, pos in missing_fields.items():
        if key in form_data and key not in positions:
            positions[key] = pos

    # Load visual checker
    check_visual = load_visual_checker()

    # Iterative improvement
    print("Starting iterative alignment improvement...")
    best_score = 0.0
    best_positions = positions.copy()

    for iteration in range(15):
        print(f"\nIteration {iteration + 1}/15")

        # Fill form
        fill_form(template, data, positions, output)

        # Check alignment
        results, score = check_visual(output, detected_positions_file, data)

        print(f"  Visual alignment: {score:.2%}")

        if score > best_score:
            best_score = score
            best_positions = positions.copy()
            print("  ✓ New best!")

        if score >= 0.90:
            print(f"\n✓ Target reached: {score:.2%}")
            break

        # Adjust misaligned fields
        misaligned = [k for k, v in results.items() if not v]
        if misaligned:
            print(f"  Adjusting {len(misaligned)} misaligned fields...")
            for field in misaligned:
                if field in positions:
                    x, y, page_idx = positions[field]
                    # Try different strategies
                    if iteration % 3 == 0:
                        positions[field] = [x, y + 20, page_idx]  # Move up
                    elif iteration % 3 == 1:
                        positions[field] = [x, y - 20, page_idx]  # Move down
                    else:
                        positions[field] = [x + 10, y, page_idx]  # Move right

    # Final fill with best positions
    print(f"\nUsing best positions (score: {best_score:.2%})")
    fill_form(template, data, best_positions, output)

    # Final check
    results, final_score = check_visual(output, detected_positions_file, data)
    print(f"\nFinal visual alignment: {final_score:.2%}")

    if final_score >= 0.90:
        print("✓ Target achieved!")
    else:
        print(f"⚠ Best achieved: {final_score:.2%}")

    print(f"\n✓ Output: {output}")


if __name__ == "__main__":
    main()
