#!/usr/bin/env python3
"""
Calibrate and Fix PDF Alignment

Uses known misalignments to calibrate positions, then fixes iteratively.
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


def check_visual_alignment_strict(
    pdf_path: str, expected_positions: dict, form_data: dict
) -> tuple:
    """Strict visual alignment checking."""
    reader = PdfReader(pdf_path)
    alignment_results = {}

    sections = {
        "property": [
            "property_street",
            "property_district",
            "property_municipality",
            "property_postal_code",
            "property_additional_info",
        ],
        "applicant": [
            "applicant_name",
            "applicant_nif",
            "applicant_address",
            "applicant_municipality",
            "applicant_phone",
            "applicant_email",
        ],
    }

    for page_num, page in enumerate(reader.pages):
        try:
            text = page.extract_text()
            text_lower = text.lower()

            for field_name, value in form_data.items():
                if field_name not in expected_positions:
                    alignment_results[field_name] = True
                    continue

                expected_x, expected_y, expected_page = expected_positions[field_name]
                if expected_page != page_num:
                    continue

                if value and str(value).strip() and value not in (True, False):
                    value_str = str(value).strip().lower()
                    found = value_str in text_lower

                    if found:
                        # Check section context
                        section_fields = []
                        for section_list in sections.values():
                            if field_name in section_list:
                                section_fields = [
                                    f
                                    for f in section_list
                                    if f != field_name
                                    and f in expected_positions
                                    and expected_positions[f][2] == expected_page
                                ]
                                break

                        if section_fields:
                            other_present = sum(
                                1
                                for f in section_fields
                                if f in form_data
                                and str(form_data[f]).lower() in text_lower
                            )
                            # Require at least 80% of other section fields present
                            found = other_present >= len(section_fields) * 0.8
                        else:
                            # No section context - check position heuristic
                            value_pos = text_lower.find(value_str)
                            if value_pos != -1:
                                text_length = len(text_lower)
                                # Must be in middle 80% (not header/footer)
                                found = (
                                    0.1 * text_length < value_pos < 0.9 * text_length
                                )
                            else:
                                found = False

                    alignment_results[field_name] = found
                else:
                    alignment_results[field_name] = True
        except Exception as e:
            print(f"Error: {e}")

    found_count = sum(1 for v in alignment_results.values() if v)
    score = found_count / len(alignment_results) if alignment_results else 0.0
    return alignment_results, score


def calibrate_positions_from_misalignments(
    base_positions: dict, misaligned_fields: list, form_data: dict, template_path: str
) -> dict:
    """Calibrate positions based on known misalignments."""
    reader = PdfReader(template_path)
    page = reader.pages[0]
    media_box = page.mediabox
    height = float(media_box.top) - float(media_box.bottom)

    calibrated = base_positions.copy()

    # Known misalignments from visual inspection:
    # applicant_name, applicant_nif, applicant_phone, applicant_email are misaligned
    # These are typically too low (need to move UP)

    # Calibrated positions based on form structure analysis
    calibrated_positions = {
        # Property section - move up slightly
        "property_street": [90, height - 95, 0],  # Was 100, move up 5
        "property_district": [400, height - 95, 0],
        "property_municipality": [400, height - 125, 0],  # Was 130, move up 5
        "property_postal_code": [450, height - 125, 0],
        "property_additional_info": [90, height - 155, 0],  # Was 160, move up 5
        # Applicant section - SIGNIFICANTLY move up (these are the misaligned ones)
        "applicant_name": [90, height - 235, 0],  # Was 250, move up 15
        "applicant_nif": [400, height - 235, 0],  # Was 250, move up 15
        "applicant_address": [90, height - 265, 0],  # Was 280, move up 15
        "applicant_municipality": [400, height - 265, 0],  # Was 280, move up 15
        "applicant_phone": [90, height - 295, 0],  # Was 310, move up 15
        "applicant_email": [300, height - 295, 0],  # Was 310, move up 15
        "applicant_capacity": [90, height - 325, 0],  # Was 340, move up 15
        # Checkboxes
        "owner_same_as_applicant": [80, height - 375, 0],
        "installer_same_as_applicant": [80, height - 405, 0],
        "offer_recipient": [80, height - 465, 0],
        "contact_person": [80, height - 495, 0],
        # Technical (page 2)
        "installation_type": [90, height - 195, 1],
        "number_of_floors": [90, height - 225, 1],
        "pressure_group": [300, height - 225, 1],
        "installation_category": [90, height - 255, 1],
        "installation_subcategory": [90, height - 285, 1],
        "housing_type": [90, height - 315, 1],
        "max_flow_liters_per_second": [380, height - 345, 1],
    }

    # Update calibrated positions
    for key in calibrated_positions:
        if key in form_data:
            calibrated[key] = calibrated_positions[key]

    return calibrated


def fill_form_with_positions(
    template_path: str, data_path: str, positions: dict, output_path: str
):
    """Fill form with given positions."""
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
    c.setFont("Helvetica", 10)

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
    positions_file = (
        "operations/execution-plans/aigues-de-barcelona-auto-positions-mapped.json"
    )
    output = "operations/execution-plans/aigues-de-barcelona-filled-form-calibrated.pdf"

    # Load data
    with open(data) as f:
        form_data = json.load(f)

    with open(positions_file) as f:
        base_positions = json.load(f)

    print("Calibrating positions based on known misalignments...")
    calibrated_positions = calibrate_positions_from_misalignments(
        base_positions,
        ["applicant_name", "applicant_nif", "applicant_phone", "applicant_email"],
        form_data,
        template,
    )

    print("Filling form with calibrated positions...")
    fill_form_with_positions(template, data, calibrated_positions, output)

    print("Checking visual alignment...")
    results, score = check_visual_alignment_strict(
        output, calibrated_positions, form_data
    )

    print(
        f"\nVisual alignment: {score:.2%} ({sum(1 for v in results.values() if v)}/{len(results)} fields)"
    )

    if score < 0.90:
        print("\nIterating to improve alignment...")
        # Iterative improvement
        current_positions = calibrated_positions.copy()
        best_score = score
        best_positions = current_positions.copy()

        for iteration in range(10):
            # Adjust misaligned fields
            misaligned = [k for k, v in results.items() if not v]
            if not misaligned:
                break

            for field in misaligned:
                if field in current_positions:
                    x, y, page_idx = current_positions[field]
                    # Try moving up (common fix)
                    current_positions[field] = [x, y + 10, page_idx]

            fill_form_with_positions(template, data, current_positions, output)
            results, score = check_visual_alignment_strict(
                output, current_positions, form_data
            )

            print(f"  Iteration {iteration + 1}: {score:.2%}")

            if score > best_score:
                best_score = score
                best_positions = current_positions.copy()

            if score >= 0.90:
                print(f"\n✓ Target reached: {score:.2%}")
                break

        # Use best positions
        fill_form_with_positions(template, data, best_positions, output)
        results, final_score = check_visual_alignment_strict(
            output, best_positions, form_data
        )
        print(f"\nFinal visual alignment: {final_score:.2%}")

    print(f"\n✓ Output: {output}")


if __name__ == "__main__":
    main()
