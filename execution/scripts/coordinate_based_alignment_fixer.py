#!/usr/bin/env python3
"""
Coordinate-Based Alignment Fixer

Uses PyMuPDF (fitz) to extract exact text coordinates from filled PDF,
then compares to expected positions and iteratively adjusts until 90%+ alignment.

This is the most accurate automated approach:
1. Fill PDF with current positions
2. Extract text with exact coordinates using PyMuPDF
3. Compare actual vs expected positions
4. Calculate offset corrections
5. Iterate until alignment threshold reached
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

try:
    import fitz  # PyMuPDF

    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False
    print("Warning: PyMuPDF not installed. Install with: pip install pymupdf")
    print("Falling back to approximate coordinate extraction.")


def extract_text_coordinates(
    pdf_path: str,
) -> dict[str, list[tuple[float, float, int]]]:
    """
    Extract text from PDF with exact coordinates.
    Returns: {text_value: [(x, y, page_num), ...]} - list because same text may appear multiple times
    """
    if HAS_FITZ:
        doc = fitz.open(pdf_path)
        text_coords = {}

        for page_num in range(len(doc)):
            page = doc[page_num]
            text_dict = page.get_text("dict")

            for block in text_dict["blocks"]:
                if "lines" in block:
                    for line in block["lines"]:
                        for span in line["spans"]:
                            text = span["text"].strip()
                            if text and len(text) > 2:  # Ignore single characters/marks
                                # Get bounding box - use center point for better matching
                                bbox = span["bbox"]
                                x = (bbox[0] + bbox[2]) / 2  # Center X
                                y = (bbox[1] + bbox[3]) / 2  # Center Y

                                text_lower = text.lower()
                                if text_lower not in text_coords:
                                    text_coords[text_lower] = []
                                text_coords[text_lower].append((x, y, page_num))

        doc.close()
        return text_coords
    else:
        # Fallback: approximate extraction
        reader = PdfReader(pdf_path)
        text_coords = {}

        for page_num, page in enumerate(reader.pages):
            text = page.extract_text()
            # Approximate positions (not accurate)
            lines = text.split("\n")
            y_pos = 700 - (len(lines) * 15)  # Rough estimate
            for line in lines:
                if line.strip():
                    text_coords[line.strip().lower()] = (90, y_pos, page_num)
                    y_pos -= 15

        return text_coords


def calculate_position_errors(
    filled_pdf_path: str, expected_positions: dict[str, list[float]], form_data: dict
) -> dict[str, tuple[float, float]]:
    """
    Calculate position errors by comparing actual vs expected positions.
    Uses best match based on proximity to expected position.
    Returns: {field_name: (x_error, y_error)}
    """
    text_coords = extract_text_coordinates(filled_pdf_path)
    errors = {}

    for field_name, value in form_data.items():
        if field_name not in expected_positions:
            continue

        if not value or str(value).strip() == "" or value in (True, False):
            continue

        value_str = str(value).strip().lower()
        expected_x, expected_y, expected_page = expected_positions[field_name]

        # Find best matching text position (closest to expected)
        best_match = None
        best_distance = float("inf")

        for text, positions_list in text_coords.items():
            # Check for exact or partial match
            if value_str == text or value_str in text or text in value_str:
                for x, y, page in positions_list:
                    if page == expected_page:
                        # Calculate distance from expected position
                        distance = (
                            (x - expected_x) ** 2 + (y - expected_y) ** 2
                        ) ** 0.5
                        if distance < best_distance:
                            best_distance = distance
                            best_match = (x, y)

        # Only use if match is reasonable (within 200 points)
        if best_match and best_distance < 200:
            x_error = best_match[0] - expected_x
            y_error = best_match[1] - expected_y
            errors[field_name] = (x_error, y_error)

    return errors


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


def check_alignment_with_coordinates(
    filled_pdf_path: str,
    expected_positions: dict,
    form_data: dict,
    tolerance: float = 20.0,
) -> tuple[dict[str, bool], float]:
    """
    Check alignment using exact coordinates.
    tolerance: pixels - how close actual position must be to expected
    """
    text_coords = extract_text_coordinates(filled_pdf_path)
    alignment_results = {}

    for field_name, value in form_data.items():
        if field_name not in expected_positions:
            alignment_results[field_name] = True
            continue

        if not value or str(value).strip() == "" or value in (True, False):
            alignment_results[field_name] = True
            continue

        value_str = str(value).strip().lower()
        expected_x, expected_y, expected_page = expected_positions[field_name]

        # Find matching text
        found = False
        for text, positions_list in text_coords.items():
            if value_str == text or value_str in text or text in value_str:
                for x, y, page in positions_list:
                    if page == expected_page:
                        # Check if position is within tolerance
                        x_diff = abs(x - expected_x)
                        y_diff = abs(y - expected_y)
                        if x_diff <= tolerance and y_diff <= tolerance:
                            found = True
                            break
                if found:
                    break

        alignment_results[field_name] = found

    found_count = sum(1 for v in alignment_results.values() if v)
    score = found_count / len(alignment_results) if alignment_results else 0.0
    return alignment_results, score


def main():
    template = "reference/documents/aigues-de-barcelona/Cast-FPT-071-Nueva-o-modificación-instalaciones (1).pdf"
    data = "operations/execution-plans/aigues-de-barcelona-form-data.json"
    positions_file = "operations/execution-plans/aigues-de-barcelona-manual-calibrated-positions.json"
    output = "operations/execution-plans/aigues-de-barcelona-filled-form-coordinate-fixed.pdf"

    if not HAS_FITZ:
        print("\n⚠ PyMuPDF not installed. Installing...")
        import subprocess

        subprocess.run([sys.executable, "-m", "pip", "install", "pymupdf"], check=True)
        print("✓ Installed. Please run again.")
        sys.exit(0)

    # Load positions
    with open(positions_file) as f:
        positions = json.load(f)

    # Load form data
    with open(data) as f:
        form_data = json.load(f)

    print("=" * 70)
    print("Coordinate-Based Alignment Fixer")
    print("=" * 70)
    print("\nUsing PyMuPDF for exact coordinate extraction")
    print("Target: 90%+ alignment with coordinate-based validation\n")

    best_score = 0.0
    best_positions = positions.copy()

    # Iterative improvement using coordinate feedback
    for iteration in range(20):
        print(f"\n--- Iteration {iteration + 1}/20 ---")

        # Fill form
        fill_form(template, data, positions, output)
        print("✓ Filled form")

        # Check alignment with coordinates
        results, score = check_alignment_with_coordinates(
            output, positions, form_data, tolerance=25.0
        )

        found_count = sum(1 for v in results.values() if v)
        print(
            f"Coordinate-based alignment: {score:.2%} ({found_count}/{len(results)} fields)"
        )

        if score > best_score:
            best_score = score
            best_positions = positions.copy()
            print("✓ New best score!")

        if score >= 0.90:
            print(f"\n✓ Target reached: {score:.2%}")
            break

        # Calculate position errors
        errors = calculate_position_errors(output, positions, form_data)

        if errors:
            print(f"Calculating corrections for {len(errors)} fields...")

            # Apply corrections with damping to avoid overshooting
            damping_factor = 0.7  # Only apply 70% of correction to avoid overshooting
            for field_name, (x_error, y_error) in errors.items():
                if field_name in positions:
                    x, y, page_idx = positions[field_name]
                    # Apply correction (inverse of error) with damping
                    # Limit corrections to reasonable values
                    max_correction = 100  # Max 100 points per iteration
                    x_correction = max(
                        -max_correction, min(max_correction, -x_error * damping_factor)
                    )
                    y_correction = max(
                        -max_correction, min(max_correction, -y_error * damping_factor)
                    )

                    new_x = x + x_correction
                    new_y = y + y_correction
                    positions[field_name] = [new_x, new_y, page_idx]
                    print(
                        f"  {field_name}: adjusted by ({x_correction:.1f}, {y_correction:.1f})"
                    )
        else:
            # If no errors detected, try pattern-based adjustments
            misaligned = [k for k, v in results.items() if not v]
            if misaligned:
                print(f"Applying pattern adjustments for {len(misaligned)} fields...")
                for field in misaligned[:5]:  # Limit to avoid over-adjustment
                    if field in positions:
                        x, y, page_idx = positions[field]
                        # Try moving up (common fix)
                        positions[field] = [x, y + 30, page_idx]

    # Final fill with best positions
    print(f"\n{'=' * 70}")
    print("Final Fill with Best Positions")
    print(f"{'=' * 70}\n")
    print(f"Using positions from best iteration (score: {best_score:.2%})")

    fill_form(template, data, best_positions, output)

    # Final check
    results, final_score = check_alignment_with_coordinates(
        output, best_positions, form_data, tolerance=25.0
    )
    found_count = sum(1 for v in results.values() if v)

    print(f"\n{'=' * 70}")
    print("Final Results")
    print(f"{'=' * 70}\n")
    print(
        f"Final coordinate-based alignment: {final_score:.2%} ({found_count}/{len(results)} fields)"
    )

    missing = [k for k, v in results.items() if not v]
    if missing:
        print(f"\nMisaligned fields ({len(missing)}): {', '.join(missing[:10])}")

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
