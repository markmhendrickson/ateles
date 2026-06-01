#!/usr/bin/env python3
"""
PDF Field Detector

Automatically detects form field positions using OCR and image processing.
Outputs a positions JSON file that can be used with fill-pdf-form.py

Usage:
    python pdf-field-detector.py --template form.pdf --output positions.json
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import cv2
    import numpy as np
    import pytesseract
    from pdf2image import convert_from_path
    from PIL import Image
except ImportError as e:
    print("Error: Missing required library. Install with:")
    print("  pip install pdf2image opencv-python pillow pytesseract")
    print("  brew install poppler tesseract  # macOS")
    print(f"Missing: {e}")
    sys.exit(1)

# Configure Tesseract to use custom language data if available
try:
    from tesseract_config import configure_tesseract_data_path

    configure_tesseract_data_path()
except ImportError:
    pass  # tesseract_config not available, use system defaults


def detect_form_fields(pdf_path, page_num=0):
    """
    Detect form field positions using OCR and image processing.

    Returns: dict mapping field labels to (x, y) coordinates in PDF points
    """
    print(f"Converting PDF page {page_num} to image...")

    # Convert PDF page to image
    images = convert_from_path(
        pdf_path, first_page=page_num + 1, last_page=page_num + 1, dpi=200
    )
    if not images:
        print(f"Error: Could not convert page {page_num}")
        return {}

    img = np.array(images[0])
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

    print("Detecting horizontal lines (form field underlines)...")

    # Detect horizontal lines (form field underlines)
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    detected_lines = cv2.morphologyEx(
        gray, cv2.MORPH_OPEN, horizontal_kernel, iterations=2
    )

    # Find contours of lines
    contours, _ = cv2.findContours(
        detected_lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    # Filter for horizontal lines (form fields)
    field_positions = {}
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        # Filter: horizontal lines with reasonable width
        if w > 100 and h < 5:
            # Convert pixel coordinates to PDF points
            # Assuming 200 DPI: 1 point = 200/72 pixels
            pdf_x = (x / 200) * 72
            pdf_y = ((img.shape[0] - y) / 200) * 72  # Flip Y axis

            # Use OCR to detect label near this line
            roi = gray[
                max(0, y - 30) : y, max(0, x - 200) : min(img.shape[1], x + w + 50)
            ]
            if roi.size > 0:
                try:
                    text = pytesseract.image_to_string(roi, config="--psm 7").strip()
                    if text and len(text) > 2:
                        # Simple heuristic: use first meaningful word as key
                        key = text.split()[0].lower().replace(":", "").replace("*", "")
                        field_positions[key] = [pdf_x, pdf_y, page_num]
                except Exception:
                    pass

    print(f"Detected {len(field_positions)} potential form fields")
    return field_positions


def detect_labels_and_fields(pdf_path, page_num=0):
    """
    Alternative approach: Use OCR to find labels, then infer field positions.
    """
    print("Using OCR to detect form labels and fields...")

    images = convert_from_path(
        pdf_path, first_page=page_num + 1, last_page=page_num + 1, dpi=200
    )
    if not images:
        return {}

    img = np.array(images[0])

    # Get OCR data with bounding boxes
    try:
        ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    except Exception as e:
        print(f"OCR error: {e}")
        return {}

    # Find common form field labels
    field_labels = [
        "calle",
        "municipio",
        "código postal",
        "distrito",
        "nombre",
        "nif",
        "teléfono",
        "correo",
        "email",
        "número",
        "plantas",
        "tipo",
        "vivienda",
    ]

    field_positions = {}
    height = img.shape[0]

    for i, text in enumerate(ocr_data["text"]):
        text_lower = text.lower().strip()
        if any(label in text_lower for label in field_labels):
            x = ocr_data["left"][i]
            y = ocr_data["top"][i]
            w = ocr_data["width"][i]
            h = ocr_data["height"][i]

            # Field is typically to the right of the label
            field_x = (x + w + 20) / 200 * 72
            field_y = ((height - y - h / 2) / 200) * 72

            # Create key from label
            key = text_lower.replace(":", "").replace("*", "").replace(" ", "_")
            field_positions[key] = [field_x, field_y, page_num]

    print(f"Detected {len(field_positions)} fields via OCR")
    return field_positions


def main():
    parser = argparse.ArgumentParser(
        description="Automatically detect PDF form field positions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--template", required=True, help="PDF template file")
    parser.add_argument("--output", required=True, help="Output positions JSON file")
    parser.add_argument("--page", type=int, default=0, help="Page number (0-indexed)")
    parser.add_argument(
        "--method",
        choices=["lines", "ocr", "both"],
        default="both",
        help="Detection method",
    )

    args = parser.parse_args()

    if not Path(args.template).exists():
        print(f"Error: Template file not found: {args.template}")
        sys.exit(1)

    positions = {}

    if args.method in ["lines", "both"]:
        print("\n=== Method 1: Line Detection ===")
        line_positions = detect_form_fields(args.template, args.page)
        positions.update(line_positions)

    if args.method in ["ocr", "both"]:
        print("\n=== Method 2: OCR Label Detection ===")
        ocr_positions = detect_labels_and_fields(args.template, args.page)
        positions.update(ocr_positions)

    if not positions:
        print("\nWarning: No fields detected. You may need to:")
        print("  1. Install poppler: brew install poppler")
        print("  2. Install tesseract: brew install tesseract")
        print("  3. Check PDF quality/resolution")
        sys.exit(1)

    # Save positions
    with open(args.output, "w") as f:
        json.dump(positions, f, indent=2)

    print(f"\n✓ Detected {len(positions)} field positions")
    print(f"✓ Saved to: {args.output}")
    print("\nReview and adjust coordinates as needed, then use with fill-pdf-form.py")


if __name__ == "__main__":
    main()
