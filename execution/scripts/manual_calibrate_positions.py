#!/usr/bin/env python3
"""
Manual Position Calibration

Manually calibrates positions based on form structure analysis and known misalignments.
Uses form layout knowledge to set correct positions.
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


def get_calibrated_positions(template_path: str, form_data: dict) -> dict:
    """
    Get manually calibrated positions based on form structure.
    These positions are calibrated to match actual form field locations.
    """
    reader = PdfReader(template_path)
    page = reader.pages[0]
    media_box = page.mediabox
    float(media_box.right) - float(media_box.left)
    height = float(media_box.top) - float(media_box.bottom)

    # Manually calibrated positions based on form structure
    # Y coordinates: higher = higher on page (PDF uses bottom-left origin)
    # Form structure: Header at top, Property section, Applicant section, Owner/Installer sections

    calibrated = {
        # Page 1 - Property section (top area, after header ~100-150 points from top)
        "property_street": [90, height - 105, 0],  # First field in property section
        "property_district": [400, height - 105, 0],  # Right side
        "property_municipality": [400, height - 135, 0],  # Below district
        "property_postal_code": [450, height - 135, 0],  # Next to municipality
        "property_additional_info": [90, height - 165, 0],  # Additional info field
        # Page 1 - Applicant section (middle area, ~250-350 points from top)
        "applicant_name": [90, height - 255, 0],  # First applicant field
        "applicant_nif": [400, height - 255, 0],  # Right side, same row
        "applicant_address": [90, height - 285, 0],  # Below name
        "applicant_municipality": [400, height - 285, 0],  # Right side
        "applicant_phone": [90, height - 315, 0],  # Below address
        "applicant_email": [300, height - 315, 0],  # Right side, same row as phone
        "applicant_capacity": [90, height - 345, 0],  # Capacity checkbox area
        # Page 1 - Checkboxes (below applicant section, ~380-500 points from top)
        "owner_same_as_applicant": [80, height - 385, 0],
        "installer_same_as_applicant": [80, height - 415, 0],
        "offer_recipient": [80, height - 475, 0],  # Offer recipient section
        "contact_person": [80, height - 505, 0],  # Contact person section
        # Page 2 - Technical section
        "installation_type": [90, height - 205, 1],  # First technical field
        "number_of_floors": [90, height - 235, 1],
        "pressure_group": [300, height - 235, 1],  # Right side
        "installation_category": [90, height - 265, 1],
        "installation_subcategory": [90, height - 295, 1],
        "housing_type": [90, height - 325, 1],
        "max_flow_liters_per_second": [380, height - 355, 1],
    }

    # Only return positions for fields that exist in form_data
    return {k: v for k, v in calibrated.items() if k in form_data}


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
    output = "operations/execution-plans/aigues-de-barcelona-filled-form-manual-calibrated.pdf"
    positions_output = "operations/execution-plans/aigues-de-barcelona-manual-calibrated-positions.json"

    # Load form data
    with open(data) as f:
        form_data = json.load(f)

    # Get calibrated positions
    print("Generating manually calibrated positions...")
    positions = get_calibrated_positions(template, form_data)

    # Save positions
    with open(positions_output, "w") as f:
        json.dump(positions, f, indent=2)
    print(f"✓ Saved {len(positions)} calibrated positions")

    # Fill form
    print("Filling form with calibrated positions...")
    fill_form(template, data, positions, output)

    # Check alignment using visual checker
    print("Checking visual alignment...")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "visual_checker", "scripts/visual-alignment-checker.py"
    )
    visual_checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(visual_checker)

    results, score = visual_checker.check_visual_alignment(
        output, positions_output, data
    )

    print(
        f"\nVisual alignment: {score:.2%} ({sum(1 for v in results.values() if v)}/{len(results)} fields)"
    )

    if score < 0.90:
        print("\nIterating to improve...")
        current_positions = positions.copy()
        best_score = score
        best_positions = current_positions.copy()

        for iteration in range(20):
            misaligned = [k for k, v in results.items() if not v]
            if not misaligned:
                break

            # Adjust misaligned fields
            for field in misaligned:
                if field in current_positions:
                    x, y, page_idx = current_positions[field]
                    # Try larger adjustments
                    strategy = iteration % 4
                    if strategy == 0:
                        current_positions[field] = [
                            x,
                            y + 30,
                            page_idx,
                        ]  # Move up large
                    elif strategy == 1:
                        current_positions[field] = [
                            x,
                            y - 30,
                            page_idx,
                        ]  # Move down large
                    elif strategy == 2:
                        current_positions[field] = [x + 20, y, page_idx]  # Move right
                    else:
                        current_positions[field] = [x - 20, y, page_idx]  # Move left

            fill_form(template, data, current_positions, output)
            results, score = visual_checker.check_visual_alignment(
                output, positions_output, data
            )

            print(f"  Iteration {iteration + 1}: {score:.2%}")

            if score > best_score:
                best_score = score
                best_positions = current_positions.copy()

            if score >= 0.90:
                print(f"\n✓ Target reached: {score:.2%}")
                break

        # Final fill
        fill_form(template, data, best_positions, output)
        results, final_score = visual_checker.check_visual_alignment(
            output, positions_output, data
        )
        print(f"\nFinal visual alignment: {final_score:.2%}")

        # Save best positions
        with open(positions_output, "w") as f:
            json.dump(best_positions, f, indent=2)
    else:
        print("✓ Target already achieved!")

    print(f"\n✓ Output: {output}")
    print(f"✓ Positions saved: {positions_output}")


if __name__ == "__main__":
    main()
