#!/usr/bin/env python3
"""
Inspect PDF for filled value alignment.

Checks text positions in filled PDF and compares with expected field positions.
"""

import json
import sys
from pathlib import Path

try:
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas
except ImportError:
    print("Error: Missing required library. Install with: pip install pypdf reportlab")
    sys.exit(1)


def extract_text_positions(pdf_path):
    """Extract text and their positions from PDF."""
    reader = PdfReader(pdf_path)
    text_objects = []

    for page_num, page in enumerate(reader.pages):
        if "/Annots" in page:
            annotations = page["/Annots"]
            for annot in annotations:
                obj = annot.get_object()
                if "/Subtype" in obj and obj["/Subtype"] == "/Widget":
                    if "/V" in obj:  # Field value
                        field_name = obj.get("/T", "Unknown")
                        value = obj["/V"]
                        text_objects.append(
                            {
                                "page": page_num,
                                "field": str(field_name),
                                "value": str(value),
                                "type": "form_field",
                            }
                        )

        # Extract text with positions (if available)
        if "/Contents" in page:
            # Try to extract text content
            try:
                text = page.extract_text()
                if text:
                    text_objects.append(
                        {
                            "page": page_num,
                            "text": text[:100],  # First 100 chars
                            "type": "text_content",
                        }
                    )
            except Exception:
                pass

    return text_objects


def check_overlay_alignment(
    pdf_path, expected_positions_path=None, form_data_path=None
):
    """Check alignment of overlay text in PDF."""
    reader = PdfReader(pdf_path)

    print(f"\n{'=' * 60}")
    print(f"Inspecting PDF: {pdf_path}")
    print(f"{'=' * 60}\n")

    # Load expected positions if available
    expected_positions = {}
    if expected_positions_path and Path(expected_positions_path).exists():
        with open(expected_positions_path) as f:
            expected_positions = json.load(f)
        print(f"Loaded {len(expected_positions)} expected field positions\n")

    # Load form data if available
    form_data = {}
    if form_data_path and Path(form_data_path).exists():
        with open(form_data_path) as f:
            form_data = json.load(f)
        print(f"Loaded {len(form_data)} form data fields\n")

    # Check each page
    for page_num, page in enumerate(reader.pages):
        print(f"\n--- Page {page_num + 1} ---")

        # Get page dimensions
        media_box = page.mediabox
        width = float(media_box.right) - float(media_box.left)
        height = float(media_box.top) - float(media_box.bottom)
        print(f"Page size: {width:.1f} x {height:.1f} points")

        # Extract text content
        try:
            text = page.extract_text()
            if text:
                print(f"\nExtracted text content ({len(text)} chars):")
                print("-" * 60)
                # Show first 500 chars
                preview = text[:500].replace("\n", " ")
                print(preview)
                if len(text) > 500:
                    print("...")
                print("-" * 60)
        except Exception as e:
            print(f"Could not extract text: {e}")

        # Check for form fields
        if "/Annots" in page:
            annotations = page["/Annots"]
            print(f"\nFound {len(annotations)} annotations on page {page_num + 1}")
            for i, annot in enumerate(annotations):
                obj = annot.get_object()
                if "/Subtype" in obj:
                    print(f"  Annotation {i + 1}: Type={obj['/Subtype']}")
                    if "/T" in obj:
                        print(f"    Field name: {obj['/T']}")
                    if "/V" in obj:
                        print(f"    Value: {obj['/V']}")

        # Check for XObjects (overlay content)
        if "/Resources" in page and "/XObject" in page["/Resources"]:
            xobjects = page["/Resources"]["/XObject"]
            print(f"\nFound {len(xobjects)} XObjects (possible overlays)")
            for name, obj in xobjects.items():
                print(f"  XObject: {name}")

    # Compare with expected positions
    if expected_positions and form_data:
        print(f"\n{'=' * 60}")
        print("Alignment Check:")
        print(f"{'=' * 60}\n")

        print("Expected fields vs Form data:")
        print("-" * 60)
        for field_name, position in expected_positions.items():
            x, y, page_idx = position
            value = form_data.get(field_name, "NOT IN DATA")
            status = "✓" if field_name in form_data else "✗"
            print(
                f"{status} {field_name:30s} | Pos: ({x:6.1f}, {y:6.1f}, {page_idx}) | Value: {value}"
            )

        print("\nForm data fields not in positions:")
        print("-" * 60)
        for field_name in form_data:
            if field_name not in expected_positions:
                print(f"  ✗ {field_name:30s} | Value: {form_data[field_name]}")


def main():
    pdf_path = "operations/execution-plans/aigues-de-barcelona-filled-form.pdf"
    positions_path = (
        "operations/execution-plans/aigues-de-barcelona-auto-positions-mapped.json"
    )
    data_path = "operations/execution-plans/aigues-de-barcelona-form-data.json"

    if not Path(pdf_path).exists():
        print(f"Error: PDF not found: {pdf_path}")
        sys.exit(1)

    check_overlay_alignment(pdf_path, positions_path, data_path)


if __name__ == "__main__":
    main()
