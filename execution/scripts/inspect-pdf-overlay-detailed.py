#!/usr/bin/env python3
"""
Detailed PDF overlay inspection - checks actual text rendering positions.
"""

import json
import sys
from pathlib import Path

try:
    from pypdf import PdfReader
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
except ImportError:
    print("Error: Missing required library. Install with: pip install pypdf reportlab")
    sys.exit(1)


def inspect_pdf_overlay(pdf_path, positions_path=None, data_path=None):
    """Inspect PDF overlay content in detail."""
    reader = PdfReader(pdf_path)

    print(f"\n{'=' * 70}")
    print("Detailed PDF Overlay Inspection")
    print(f"{'=' * 70}\n")
    print(f"PDF: {pdf_path}\n")

    # Load data
    positions = {}
    form_data = {}

    if positions_path and Path(positions_path).exists():
        with open(positions_path) as f:
            positions = json.load(f)
        print(f"✓ Loaded {len(positions)} field positions\n")

    if data_path and Path(data_path).exists():
        with open(data_path) as f:
            form_data = json.load(f)
        print(f"✓ Loaded {len(form_data)} form data fields\n")

    # Analyze each page
    for page_num, page in enumerate(reader.pages):
        print(f"\n{'=' * 70}")
        print(f"PAGE {page_num + 1}")
        print(f"{'=' * 70}\n")

        # Page dimensions
        media_box = page.mediabox
        width = float(media_box.right) - float(media_box.left)
        height = float(media_box.top) - float(media_box.bottom)
        print(f"Page dimensions: {width:.1f} x {height:.1f} points (A4: 595.3 x 841.9)")

        # Extract all text with approximate positions
        try:
            text_content = page.extract_text()
            print(f"\nText content length: {len(text_content)} characters")

            # Look for our filled values in the text
            if form_data:
                print(f"\n{'─' * 70}")
                print("Checking for filled values in extracted text:")
                print(f"{'─' * 70}\n")

                found_values = []
                for field_name, value in form_data.items():
                    if value and str(value).strip() and value not in (True, False):
                        value_str = str(value).strip()
                        if value_str in text_content:
                            found_values.append((field_name, value_str, "✓ FOUND"))
                        else:
                            # Check for partial matches
                            if len(value_str) > 5:
                                partial = value_str[:5]
                                if partial in text_content:
                                    found_values.append(
                                        (field_name, value_str, "⚠ PARTIAL")
                                    )
                                else:
                                    found_values.append(
                                        (field_name, value_str, "✗ NOT FOUND")
                                    )

                for field_name, value, status in found_values:
                    print(f"{status:12s} | {field_name:30s} | {value}")
        except Exception as e:
            print(f"Error extracting text: {e}")

        # Check XObjects (overlay layers)
        if "/Resources" in page and "/XObject" in page["/Resources"]:
            xobjects = page["/Resources"]["/XObject"]
            print(f"\n{'─' * 70}")
            print(f"XObjects (overlay layers): {len(xobjects)}")
            print(f"{'─' * 70}\n")

            for name, obj_ref in xobjects.items():
                try:
                    obj = obj_ref.get_object()
                    obj_type = obj.get("/Subtype", "Unknown")
                    print(f"  {name:20s} | Type: {obj_type}")
                except Exception:
                    print(f"  {name:20s} | (could not inspect)")

        # Check annotations
        if "/Annots" in page:
            annotations = page["/Annots"]
            print(f"\n{'─' * 70}")
            print(f"Annotations: {len(annotations)}")
            print(f"{'─' * 70}\n")

            for i, annot_ref in enumerate(annotations):
                try:
                    annot = annot_ref.get_object()
                    annot_type = annot.get("/Subtype", "Unknown")
                    print(f"  Annotation {i + 1}: {annot_type}")
                    if "/T" in annot:
                        print(f"    Field: {annot['/T']}")
                    if "/V" in annot:
                        print(f"    Value: {annot['/V']}")
                except Exception:
                    print(f"  Annotation {i + 1}: (could not inspect)")

        # Show expected positions for this page
        if positions:
            print(f"\n{'─' * 70}")
            print(f"Expected field positions for page {page_num + 1}:")
            print(f"{'─' * 70}\n")

            page_positions = {k: v for k, v in positions.items() if v[2] == page_num}
            if page_positions:
                print(f"{'Field Name':<35s} | {'X':>8s} | {'Y':>8s} | {'Value':<30s}")
                print(f"{'─' * 35}─┼{'─' * 8}─┼{'─' * 8}─┼{'─' * 30}")

                for field_name, (x, y, p) in sorted(page_positions.items()):
                    value = form_data.get(field_name, "")
                    if isinstance(value, bool):
                        value = "✓" if value else ""
                    value_str = str(value)[:30] if value else ""
                    print(
                        f"{field_name:<35s} | {x:>8.1f} | {y:>8.1f} | {value_str:<30s}"
                    )
            else:
                print("  (No positions defined for this page)")

    # Summary
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}\n")

    if positions and form_data:
        mapped_count = sum(1 for k in form_data.keys() if k in positions)
        unmapped_count = len(form_data) - mapped_count

        print(f"Form data fields: {len(form_data)}")
        print(f"  ✓ With position mapping: {mapped_count}")
        print(f"  ✗ Without position mapping: {unmapped_count}")

        if unmapped_count > 0:
            print("\nMissing position mappings:")
            for field_name in form_data:
                if field_name not in positions:
                    value = form_data[field_name]
                    print(f"  ✗ {field_name:35s} | Value: {value}")


def main():
    pdf_path = "operations/execution-plans/aigues-de-barcelona-filled-form.pdf"
    positions_path = (
        "operations/execution-plans/aigues-de-barcelona-auto-positions-mapped.json"
    )
    data_path = "operations/execution-plans/aigues-de-barcelona-form-data.json"

    if not Path(pdf_path).exists():
        print(f"Error: PDF not found: {pdf_path}")
        sys.exit(1)

    inspect_pdf_overlay(pdf_path, positions_path, data_path)


if __name__ == "__main__":
    main()
