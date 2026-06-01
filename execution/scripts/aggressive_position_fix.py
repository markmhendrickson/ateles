#!/usr/bin/env python3
"""
Aggressive Position Fix

Uses large, targeted adjustments to fix remaining misalignments.
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
    output = (
        "operations/execution-plans/aigues-de-barcelona-filled-form-90percent-final.pdf"
    )

    # Load positions
    with open(positions_file) as f:
        positions = json.load(f)

    # Load form data
    with open(data) as f:
        form_data = json.load(f)

    # Get page dimensions
    reader = PdfReader(template)
    page = reader.pages[0]
    media_box = page.mediabox
    height = float(media_box.top) - float(media_box.bottom)

    # Load visual checker (direct version)
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "visual_checker", "scripts/visual-alignment-checker-direct.py"
    )
    visual_checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(visual_checker)

    # Known misalignments: property_postal_code, property_additional_info,
    # applicant_name, applicant_nif, applicant_phone, applicant_email,
    # number_of_floors, housing_type

    # Apply large corrections based on field analysis
    # Applicant fields are consistently misaligned - need to move UP significantly
    # Property fields also need adjustment

    # Start with current best positions
    current_positions = positions.copy()

    print("Applying aggressive position corrections...")
    print("Target: 90%+ visual alignment\n")

    best_score = 0.0
    best_positions = current_positions.copy()

    # Try multiple adjustment strategies
    adjustment_strategies = [
        # Strategy 1: Large upward movement for applicant fields
        lambda p: {
            **p,
            **{
                "applicant_name": [p["applicant_name"][0], height - 230, 0],
                "applicant_nif": [p["applicant_nif"][0], height - 230, 0],
                "applicant_phone": [p["applicant_phone"][0], height - 290, 0],
                "applicant_email": [p["applicant_email"][0], height - 290, 0],
                "property_postal_code": [p["property_postal_code"][0], height - 125, 0],
                "property_additional_info": [
                    p["property_additional_info"][0],
                    height - 155,
                    0,
                ],
                "number_of_floors": [p["number_of_floors"][0], height - 225, 1],
                "housing_type": [p["housing_type"][0], height - 315, 1],
            },
        },
        # Strategy 2: Even larger upward movement
        lambda p: {
            **p,
            **{
                "applicant_name": [p["applicant_name"][0], height - 220, 0],
                "applicant_nif": [p["applicant_nif"][0], height - 220, 0],
                "applicant_phone": [p["applicant_phone"][0], height - 280, 0],
                "applicant_email": [p["applicant_email"][0], height - 280, 0],
                "property_postal_code": [p["property_postal_code"][0], height - 120, 0],
                "property_additional_info": [
                    p["property_additional_info"][0],
                    height - 150,
                    0,
                ],
                "number_of_floors": [p["number_of_floors"][0], height - 220, 1],
                "housing_type": [p["housing_type"][0], height - 310, 1],
            },
        },
        # Strategy 3: Moderate adjustments
        lambda p: {
            **p,
            **{
                "applicant_name": [p["applicant_name"][0], height - 245, 0],
                "applicant_nif": [p["applicant_nif"][0], height - 245, 0],
                "applicant_phone": [p["applicant_phone"][0], height - 305, 0],
                "applicant_email": [p["applicant_email"][0], height - 305, 0],
                "property_postal_code": [p["property_postal_code"][0], height - 130, 0],
                "property_additional_info": [
                    p["property_additional_info"][0],
                    height - 160,
                    0,
                ],
                "number_of_floors": [p["number_of_floors"][0], height - 230, 1],
                "housing_type": [p["housing_type"][0], height - 320, 1],
            },
        },
    ]

    for strategy_num, strategy in enumerate(adjustment_strategies):
        print(f"Trying strategy {strategy_num + 1}...")
        test_positions = strategy(current_positions)

        fill_form(template, data, test_positions, output)
        with open(data) as f:
            form_data = json.load(f)
        results, score = visual_checker.check_visual_alignment_direct(
            output, test_positions, form_data
        )

        print(f"  Score: {score:.2%}")

        if score > best_score:
            best_score = score
            best_positions = test_positions.copy()
            print("  ✓ New best!")

        if score >= 0.90:
            print(f"\n✓ Target reached: {score:.2%}")
            break

    # If still not 90%, iterate with best strategy
    if best_score < 0.90:
        print(f"\nIterating with best strategy (current: {best_score:.2%})...")
        current_positions = best_positions.copy()

        for iteration in range(20):
            # Fill and check
            fill_form(template, data, current_positions, output)
            with open(data) as f:
                form_data = json.load(f)
            results, score = visual_checker.check_visual_alignment_direct(
                output, current_positions, form_data
            )

            if iteration % 5 == 0:
                print(f"  Iteration {iteration + 1}: {score:.2%}")

            if score > best_score:
                best_score = score
                best_positions = current_positions.copy()

            if score >= 0.90:
                print(f"\n✓ Target reached: {score:.2%}")
                break

            # Adjust misaligned fields with large movements
            misaligned = [k for k, v in results.items() if not v]
            if misaligned:
                for field in misaligned:
                    if field in current_positions:
                        x, y, page_idx = current_positions[field]
                        # Large adjustments: 50-100 points
                        if "applicant" in field:
                            current_positions[field] = [x, y + 60, page_idx]  # Large up
                        elif "property" in field:
                            current_positions[field] = [x, y + 40, page_idx]
                        else:
                            current_positions[field] = [x, y + 50, page_idx]

    # Final fill
    print(f"\nUsing best positions (score: {best_score:.2%})")
    fill_form(template, data, best_positions, output)

    # Final check
    with open(data) as f:
        form_data = json.load(f)
    results, final_score = visual_checker.check_visual_alignment_direct(
        output, best_positions, form_data
    )
    found_count = sum(1 for v in results.values() if v)
    total_count = len(results)

    print(f"\n{'=' * 70}")
    print("Final Results")
    print(f"{'=' * 70}\n")
    print(
        f"Final visual alignment: {final_score:.2%} ({found_count}/{total_count} fields)"
    )

    missing = [k for k, v in results.items() if not v]
    if missing:
        print(f"\nMisaligned fields ({len(missing)}): {', '.join(missing[:10])}")
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more")

    if final_score >= 0.90:
        print("\n✓ Target achieved (90%+)!")
    else:
        print(f"\n⚠ Best achieved: {final_score:.2%} (target: 90%)")

    # Save best positions
    with open(positions_file, "w") as f:
        json.dump(best_positions, f, indent=2)

    print(f"\n✓ Output: {output}")
    print(f"✓ Positions saved: {positions_file}")

    return final_score >= 0.90


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
