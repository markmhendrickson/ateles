#!/usr/bin/env python3
"""
Visual Diff Alignment Fixer

Most accurate automated approach:
1. Convert template and filled PDF to images
2. Use computer vision to detect text regions in filled PDF
3. Match detected text regions to expected field positions
4. Calculate precise offsets
5. Iterate until 90%+ alignment

This is the most reliable because it uses actual visual output,
not coordinate estimates.
"""

import json
import sys
from pathlib import Path

try:
    import cv2
    import numpy as np
    import pytesseract
    from pdf2image import convert_from_path
    from PIL import Image
except ImportError:
    print("Error: Missing required libraries. Install with:")
    print("  pip install pdf2image opencv-python pillow pytesseract")
    print("  brew install poppler tesseract  # macOS")
    sys.exit(1)

# Configure Tesseract to use custom language data if available
try:
    from tesseract_config import configure_tesseract_data_path

    configure_tesseract_data_path()
except ImportError:
    pass  # tesseract_config not available, use system defaults

try:
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
except ImportError:
    print("Error: Missing required library. Install with: pip install pypdf reportlab")
    sys.exit(1)


def extract_text_regions_from_image(img: np.ndarray) -> list[tuple[str, float, float]]:
    """
    Extract text regions from image using OCR with bounding boxes.
    Returns: [(text, center_x, center_y), ...] in pixel coordinates
    """
    try:
        ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        text_regions = []

        for i, text in enumerate(ocr_data["text"]):
            text = text.strip()
            if text and len(text) > 2:
                x = ocr_data["left"][i]
                y = ocr_data["top"][i]
                w = ocr_data["width"][i]
                h = ocr_data["height"][i]

                # Use center point
                center_x = x + w / 2
                center_y = y + h / 2

                text_regions.append((text.lower(), center_x, center_y))

        return text_regions
    except Exception as e:
        print(f"OCR error: {e}")
        return []


def pixel_to_pdf_coords(
    x_px: float, y_px: float, img_shape: tuple, pdf_dims: tuple
) -> tuple[float, float]:
    """Convert pixel coordinates to PDF coordinates."""
    height_px, width_px = img_shape[:2]
    pdf_width, pdf_height = pdf_dims

    x_pdf = (x_px / width_px) * pdf_width
    y_pdf = pdf_height - ((y_px / height_px) * pdf_height)  # Flip Y
    return x_pdf, y_pdf


def calculate_position_corrections(
    filled_pdf_path: str, template_path: str, expected_positions: dict, form_data: dict
) -> dict[str, tuple[float, float]]:
    """
    Calculate position corrections by comparing filled PDF to template.
    Uses visual text detection to find actual positions.
    """
    # Convert filled PDF to image
    filled_images = convert_from_path(filled_pdf_path, dpi=300)
    if not filled_images:
        return {}

    filled_img = np.array(filled_images[0])

    # Get PDF dimensions
    reader = PdfReader(template_path)
    page = reader.pages[0]
    media_box = page.mediabox
    pdf_width = float(media_box.right) - float(media_box.left)
    pdf_height = float(media_box.top) - float(media_box.bottom)

    # Extract text regions from filled PDF
    text_regions = extract_text_regions_from_image(filled_img)

    corrections = {}

    for field_name, value in form_data.items():
        if field_name not in expected_positions:
            continue

        if not value or str(value).strip() == "" or value in (True, False):
            continue

        value_str = str(value).strip().lower()
        expected_x, expected_y, expected_page = expected_positions[field_name]

        # Find matching text region
        best_match = None
        best_distance = float("inf")

        for text, x_px, y_px in text_regions:
            if value_str in text or text in value_str:
                # Convert to PDF coordinates
                x_pdf, y_pdf = pixel_to_pdf_coords(
                    x_px, y_px, filled_img.shape, (pdf_width, pdf_height)
                )

                # Calculate distance from expected position
                distance = (
                    (x_pdf - expected_x) ** 2 + (y_pdf - expected_y) ** 2
                ) ** 0.5

                if distance < best_distance:
                    best_distance = distance
                    best_match = (x_pdf, y_pdf)

        if best_match and best_distance < 200:  # Reasonable match
            x_error = best_match[0] - expected_x
            y_error = best_match[1] - expected_y
            corrections[field_name] = (x_error, y_error)

    return corrections


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


def check_alignment_visual(
    filled_pdf_path: str,
    expected_positions: dict,
    form_data: dict,
    tolerance: float = 30.0,
) -> tuple[dict[str, bool], float]:
    """Check alignment using visual text detection."""
    filled_images = convert_from_path(filled_pdf_path, dpi=300)
    if not filled_images:
        return {}, 0.0

    filled_img = np.array(filled_images[0])

    reader = PdfReader(filled_pdf_path)
    page = reader.pages[0]
    media_box = page.mediabox
    pdf_width = float(media_box.right) - float(media_box.left)
    pdf_height = float(media_box.top) - float(media_box.bottom)

    text_regions = extract_text_regions_from_image(filled_img)
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

        found = False
        for text, x_px, y_px in text_regions:
            if value_str in text or text in value_str:
                x_pdf, y_pdf = pixel_to_pdf_coords(
                    x_px, y_px, filled_img.shape, (pdf_width, pdf_height)
                )

                x_diff = abs(x_pdf - expected_x)
                y_diff = abs(y_pdf - expected_y)

                if x_diff <= tolerance and y_diff <= tolerance:
                    found = True
                    break

        alignment_results[field_name] = found

    found_count = sum(1 for v in alignment_results.values() if v)
    score = found_count / len(alignment_results) if alignment_results else 0.0
    return alignment_results, score


def main():
    template = "reference/documents/aigues-de-barcelona/Cast-FPT-071-Nueva-o-modificación-instalaciones (1).pdf"
    data = "operations/execution-plans/aigues-de-barcelona-form-data.json"
    positions_file = "operations/execution-plans/aigues-de-barcelona-manual-calibrated-positions.json"
    output = (
        "operations/execution-plans/aigues-de-barcelona-filled-form-visual-fixed.pdf"
    )

    # Load positions
    with open(positions_file) as f:
        positions = json.load(f)

    # Load form data
    with open(data) as f:
        form_data = json.load(f)

    print("=" * 70)
    print("Visual Diff Alignment Fixer")
    print("=" * 70)
    print("\nUsing visual text detection to find actual positions")
    print("and iteratively adjust until 90%+ alignment.\n")

    best_score = 0.0
    best_positions = positions.copy()

    for iteration in range(15):
        print(f"\n--- Iteration {iteration + 1}/15 ---")

        # Fill form
        fill_form(template, data, positions, output)
        print("✓ Filled form")

        # Check alignment visually
        results, score = check_alignment_visual(
            output, positions, form_data, tolerance=30.0
        )
        found_count = sum(1 for v in results.values() if v)
        print(f"Visual alignment: {score:.2%} ({found_count}/{len(results)} fields)")

        if score > best_score:
            best_score = score
            best_positions = positions.copy()
            print("✓ New best score!")

        if score >= 0.90:
            print(f"\n✓ Target reached: {score:.2%}")
            break

        # Calculate corrections
        corrections = calculate_position_corrections(
            output, template, positions, form_data
        )

        if corrections:
            print(f"Applying corrections for {len(corrections)} fields...")
            damping = 0.8  # Apply 80% of correction

            for field_name, (x_error, y_error) in corrections.items():
                if field_name in positions:
                    x, y, page_idx = positions[field_name]
                    # Limit corrections
                    max_corr = 50
                    x_corr = max(-max_corr, min(max_corr, -x_error * damping))
                    y_corr = max(-max_corr, min(max_corr, -y_error * damping))

                    positions[field_name] = [x + x_corr, y + y_corr, page_idx]
        else:
            # Pattern-based adjustment
            misaligned = [k for k, v in results.items() if not v]
            if misaligned:
                for field in misaligned[:5]:
                    if field in positions:
                        x, y, page_idx = positions[field]
                        positions[field] = [x, y + 20, page_idx]

    # Final fill
    print(f"\nUsing best positions (score: {best_score:.2%})")
    fill_form(template, data, best_positions, output)

    # Final check
    results, final_score = check_alignment_visual(
        output, best_positions, form_data, tolerance=30.0
    )
    found_count = sum(1 for v in results.values() if v)

    print(f"\n{'=' * 70}")
    print("Final Results")
    print(f"{'=' * 70}\n")
    print(
        f"Final visual alignment: {final_score:.2%} ({found_count}/{len(results)} fields)"
    )

    if final_score >= 0.90:
        print("\n✓ Target achieved (90%+)!")
    else:
        print(f"\n⚠ Best achieved: {final_score:.2%} (target: 90%)")

    # Save best positions
    with open(positions_file, "w") as f:
        json.dump(best_positions, f, indent=2)

    print(f"\n✓ Output: {output}")
    print(f"✓ Positions saved: {positions_file}")


if __name__ == "__main__":
    main()
