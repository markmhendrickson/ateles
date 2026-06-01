#!/usr/bin/env python3
"""
PDF Form Filler Script

Reusable script for filling PDF forms programmatically.
Supports both fillable PDF forms and static PDFs (with text overlay).

Usage:
    python fill-pdf-form.py --template template.pdf --data data.json --output filled.pdf
    python fill-pdf-form.py --template template.pdf --data data.json --output filled.pdf --method overlay

Requirements:
    pip install pypdf reportlab
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
except ImportError as e:
    print("Error: Missing required library. Install with: pip install pypdf reportlab")
    print(f"Missing: {e}")
    sys.exit(1)


def fill_fillable_pdf(template_path, data, output_path):
    """Fill a PDF that has fillable form fields."""
    try:
        reader = PdfReader(template_path)
        writer = PdfWriter()

        # Check if PDF has form fields
        if "/AcroForm" in reader.trailer.get("/Root", {}):
            # Fill form fields
            for page in reader.pages:
                writer.add_page(page)

            # Try to fill fields
            if hasattr(reader, "get_form_text_fields"):
                fields = reader.get_form_text_fields()
                print(f"Found {len(fields)} form fields")

                # Update fields with data
                for field_name, value in data.items():
                    if field_name in fields:
                        writer.update_page_form_field_values(
                            writer.pages[0], {field_name: str(value)}
                        )
            else:
                print("Warning: PDF may not have fillable form fields")
                return False
        else:
            print("PDF does not have fillable form fields")
            return False

        # Write output
        with open(output_path, "wb") as output_file:
            writer.write(output_file)

        print(f"Successfully filled PDF: {output_path}")
        return True

    except Exception as e:
        print(f"Error filling fillable PDF: {e}")
        return False


def fill_pdf_overlay(template_path, data, output_path, field_positions=None):
    """
    Fill PDF by overlaying text on top of an existing static PDF.

    field_positions: dict mapping field names to (x_points, y_points, page_index) coordinates.
    - Coordinates are in PDF points (1 point = 1/72 inch), origin at bottom-left.
    - If not provided, a generic overlay block is written on the first page with
      all key/value pairs in a vertical list. This guarantees a usable filled PDF
      even without precise coordinates.
    """
    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(template_path)
        writer = PdfWriter()

        # Derive page size from the first page so overlay matches the template
        first_page = reader.pages[0]
        media_box = first_page.mediabox
        width = float(media_box.right) - float(media_box.left)
        height = float(media_box.top) - float(media_box.bottom)

        # Create overlay PDF with text using the same page size
        overlay_path = output_path.replace(".pdf", "_overlay.pdf")
        c = canvas.Canvas(overlay_path, pagesize=(width, height))

        if field_positions:
            # Explicit coordinate-based overlay
            # field_positions format: {field_name: (x_points, y_points, page_index)}
            for page_index in range(len(reader.pages)):
                if page_index > 0:
                    c.showPage()

                for field_name, value in data.items():
                    if field_name not in field_positions:
                        continue

                    x, y, target_page = field_positions[field_name]
                    if target_page != page_index:
                        continue

                    # Skip empty / falsey values that shouldn't render
                    if value is None or value == "" or value is False:
                        continue

                    # Render booleans as an 'X' (for checkboxes) when True
                    if isinstance(value, bool):
                        c.drawString(float(x), float(y), "X")
                    else:
                        c.drawString(float(x), float(y), str(value))
        else:
            # Generic overlay: write all key/value pairs as a block on page 0
            margin_x = 36  # 0.5 inch
            start_y = height - 72  # start 1 inch from top
            line_height = 14

            c.setFont("Helvetica", 10)
            c.drawString(
                margin_x,
                start_y,
                "DATOS RELLENADOS AUTOMÁTICAMENTE / AUTO-FILLED DATA:",
            )
            y = start_y - 2 * line_height

            for key, value in data.items():
                if value in (None, ""):
                    continue
                line = f"{key}: {value}"
                c.drawString(margin_x, y, line)
                y -= line_height
                if y < 72:  # move to next page if we run out of space
                    c.showPage()
                    y = height - 72

        c.save()

        # Merge overlay with original
        overlay_reader = PdfReader(overlay_path)
        for i, page in enumerate(reader.pages):
            if i < len(overlay_reader.pages):
                page.merge_page(overlay_reader.pages[i])
            writer.add_page(page)

        with open(output_path, "wb") as output_file:
            writer.write(output_file)

        # Clean up overlay file
        Path(overlay_path).unlink()

        print(f"Successfully created PDF with overlay: {output_path}")
        return True

    except Exception as e:
        print(f"Error creating PDF overlay: {e}")
        return False


def load_data(data_path):
    """Load form data from JSON file."""
    with open(data_path) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(
        description="Fill PDF forms programmatically",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fill fillable PDF
  python fill-pdf-form.py --template form.pdf --data data.json --output filled.pdf

  # Fill static PDF with overlay
  python fill-pdf-form.py --template form.pdf --data data.json --output filled.pdf --method overlay

  # With field positions
  python fill-pdf-form.py --template form.pdf --data data.json --output filled.pdf --positions positions.json
        """,
    )

    parser.add_argument("--template", required=True, help="Path to PDF template")
    parser.add_argument("--data", required=True, help="Path to JSON data file")
    parser.add_argument("--output", required=True, help="Output PDF path")
    parser.add_argument(
        "--method",
        choices=["auto", "fillable", "overlay"],
        default="auto",
        help="Filling method",
    )
    parser.add_argument(
        "--positions", help="JSON file with field positions for overlay method"
    )

    args = parser.parse_args()

    # Validate inputs
    if not Path(args.template).exists():
        print(f"Error: Template file not found: {args.template}")
        sys.exit(1)

    if not Path(args.data).exists():
        print(f"Error: Data file not found: {args.data}")
        sys.exit(1)

    # Load data
    data = load_data(args.data)

    # Load positions if provided
    positions = None
    if args.positions and Path(args.positions).exists():
        positions = load_data(args.positions)

    # Fill PDF
    success = False

    if args.method == "auto":
        # Try fillable first, then overlay
        print("Attempting fillable PDF method...")
        success = fill_fillable_pdf(args.template, data, args.output)

        if not success:
            print("Fillable method failed, trying overlay method...")
            success = fill_pdf_overlay(args.template, data, args.output, positions)

    elif args.method == "fillable":
        success = fill_fillable_pdf(args.template, data, args.output)

    elif args.method == "overlay":
        success = fill_pdf_overlay(args.template, data, args.output, positions)

    if not success:
        print("Failed to fill PDF. Check error messages above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
