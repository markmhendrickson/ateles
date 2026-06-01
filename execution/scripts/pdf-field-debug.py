#!/usr/bin/env python3
"""
PDF Field Debug Overlay

Purpose:
    Visualize field positions on a PDF by drawing crosshairs and labels
    at each coordinate from a positions JSON file.

Usage:
    python pdf-field-debug.py \
      --template template.pdf \
      --positions field-positions.json \
      --output debug-fields.pdf

Requires:
    pypdf, reportlab
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


def load_json(path: str):
    with open(path) as f:
        return json.load(f)


def create_debug_overlay(
    template_path: str, positions_path: str, output_path: str
) -> bool:
    try:
        reader = PdfReader(template_path)
        positions = load_json(positions_path)

        first_page = reader.pages[0]
        media_box = first_page.mediabox
        width = float(media_box.right) - float(media_box.left)
        height = float(media_box.top) - float(media_box.bottom)

        # Create overlay with same page size
        overlay_path = output_path.replace(".pdf", "_overlay.pdf")
        c = canvas.Canvas(overlay_path, pagesize=(width, height))

        # Group positions by page index
        by_page = {}
        for field_name, coords in positions.items():
            if not isinstance(coords, list | tuple) or len(coords) != 3:
                continue
            x, y, page_idx = coords
            by_page.setdefault(int(page_idx), []).append(
                (field_name, float(x), float(y))
            )

        num_pages = len(reader.pages)
        for page_index in range(num_pages):
            if page_index > 0:
                c.showPage()

            c.setFont("Helvetica", 8)
            # Draw page border for reference
            c.setStrokeColorRGB(1, 0, 0)
            c.rect(5, 5, width - 10, height - 10, stroke=1, fill=0)

            fields = by_page.get(page_index, [])
            for name, x, y in fields:
                # Crosshair
                size = 4
                c.line(x - size, y, x + size, y)
                c.line(x, y - size, x, y + size)
                # Label slightly above/right
                c.drawString(x + 2, y + 6, name)

        c.save()

        # Merge overlay with original
        overlay_reader = PdfReader(overlay_path)
        writer = PdfWriter()
        for i, page in enumerate(reader.pages):
            if i < len(overlay_reader.pages):
                page.merge_page(overlay_reader.pages[i])
            writer.add_page(page)

        with open(output_path, "wb") as out_f:
            writer.write(out_f)

        Path(overlay_path).unlink()

        print(f"Debug PDF written to: {output_path}")
        print("Each crosshair+label shows the configured coordinate for that field.")
        return True

    except Exception as e:
        print(f"Error creating debug overlay: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Create PDF field position debug overlay."
    )
    parser.add_argument("--template", required=True, help="Path to PDF template")
    parser.add_argument(
        "--positions", required=True, help="Path to field positions JSON"
    )
    parser.add_argument("--output", required=True, help="Output debug PDF path")

    args = parser.parse_args()

    if not Path(args.template).exists():
        print(f"Template not found: {args.template}")
        sys.exit(1)
    if not Path(args.positions).exists():
        print(f"Positions file not found: {args.positions}")
        sys.exit(1)

    ok = create_debug_overlay(args.template, args.positions, args.output)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
