#!/usr/bin/env python3
"""
Generic PDF Form Filler

A universal solution for filling any static PDF form:
1. Detects form fields using OCR + image processing
2. Calibrates positions (one-time per template)
3. Fills forms using calibrated positions
4. Validates alignment iteratively

Usage:
    # Step 1: Detect fields (one-time per template)
    python generic-pdf-form-filler.py detect --template form.pdf --output positions.json

    # Step 2: Calibrate positions (one-time per template, optional)
    python generic-pdf-form-filler.py calibrate --template form.pdf --positions positions.json --data data.json

    # Step 3: Fill form (reusable)
    python generic-pdf-form-filler.py fill --template form.pdf --data data.json --positions positions.json --output filled.pdf

    # Step 4: Auto-fix alignment (optional)
    python generic-pdf-form-filler.py auto-fix --template form.pdf --data data.json --positions positions.json --output filled.pdf
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

try:
    import cv2
    import numpy as np
    import pytesseract
    from pdf2image import convert_from_path
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

try:
    import fitz  # PyMuPDF  # noqa: F401

    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False


class GenericPDFFormFiller:
    """Generic PDF form filler that works with any static PDF."""

    def __init__(self, template_path: str):
        self.template_path = Path(template_path)
        self.template_hash = self._get_template_hash()

    def _get_template_hash(self) -> str:
        """Generate hash of template for caching."""
        with open(self.template_path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]

    def detect_fields(
        self, page_num: int = 0, method: str = "hybrid"
    ) -> dict[str, list[float]]:
        """
        Detect form fields using multiple methods.

        Returns: {field_key: [x, y, page_num]}
        """
        print(f"Detecting fields in {self.template_path.name} (page {page_num})...")

        # Convert PDF to image
        images = convert_from_path(
            str(self.template_path),
            first_page=page_num + 1,
            last_page=page_num + 1,
            dpi=300,
        )
        if not images:
            return {}

        img = np.array(images[0])
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

        # Get PDF dimensions
        reader = PdfReader(str(self.template_path))
        page = reader.pages[page_num]
        media_box = page.mediabox
        pdf_width = float(media_box.right) - float(media_box.left)
        pdf_height = float(media_box.top) - float(media_box.bottom)

        def px_to_pdf(x_px, y_px):
            height_px, width_px = img.shape[:2]
            x_pdf = (x_px / width_px) * pdf_width
            y_pdf = pdf_height - ((y_px / height_px) * pdf_height)
            return x_pdf, y_pdf

        field_positions = {}

        # Method 1: Detect horizontal lines (field underlines)
        if method in ["hybrid", "lines"]:
            print("  Detecting field underlines...")
            horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (50, 1))
            detected_lines = cv2.morphologyEx(
                gray, cv2.MORPH_OPEN, horizontal_kernel, iterations=2
            )
            contours, _ = cv2.findContours(
                detected_lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            line_count = 0
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                if w > 100 and h < 5:  # Horizontal line
                    x_pdf, y_pdf = px_to_pdf(x, y)
                    field_key = f"field_{line_count}"
                    field_positions[field_key] = [x_pdf, y_pdf, page_num]
                    line_count += 1

            print(f"    Found {line_count} field underlines")

        # Method 2: OCR to find labels and infer field positions
        if method in ["hybrid", "ocr"]:
            print("  Using OCR to detect field labels...")
            try:
                ocr_data = pytesseract.image_to_data(
                    img, output_type=pytesseract.Output.DICT
                )

                label_count = 0
                for i, text in enumerate(ocr_data["text"]):
                    text_lower = text.lower().strip()
                    if text_lower and len(text_lower) > 2:
                        x = ocr_data["left"][i]
                        y = ocr_data["top"][i]
                        w = ocr_data["width"][i]

                        # Field is typically to the right of label
                        field_x_px = x + w + 20
                        x_pdf, y_pdf = px_to_pdf(field_x_px, y)

                        # Create key from label text
                        field_key = (
                            text_lower.replace(":", "")
                            .replace("*", "")
                            .replace(" ", "_")
                        )
                        if field_key not in field_positions:
                            field_positions[field_key] = [x_pdf, y_pdf, page_num]
                            label_count += 1

                print(f"    Found {label_count} fields via OCR")
            except Exception as e:
                print(f"    OCR error: {e}")

        return field_positions

    def fill_form(
        self,
        data: dict,
        positions: dict[str, list[float]],
        output_path: str,
        font_size: int = 10,
    ):
        """Fill form with data using positions."""
        reader = PdfReader(str(self.template_path))
        writer = PdfWriter()

        first_page = reader.pages[0]
        media_box = first_page.mediabox
        width = float(media_box.right) - float(media_box.left)
        height = float(media_box.top) - float(media_box.bottom)

        overlay_path = Path(output_path).parent / f"overlay_{self.template_hash}.pdf"
        c = canvas.Canvas(str(overlay_path), pagesize=(width, height))
        c.setFont("Helvetica", font_size)

        pages_data = {}
        for key, value in data.items():
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
                        c.setFont("Helvetica-Bold", font_size + 2)
                        c.drawString(float(x), float(y), "✓")
                else:
                    value_str = str(value)
                    if len(value_str) > 50:
                        value_str = value_str[:47] + "..."

                    c.setFont("Helvetica", font_size)
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

    def validate_alignment(
        self, filled_pdf_path: str, positions: dict, data: dict, tolerance: float = 30.0
    ) -> tuple[dict[str, bool], float]:
        """Validate alignment using visual text detection."""
        filled_images = convert_from_path(filled_pdf_path, dpi=300)
        if not filled_images:
            return {}, 0.0

        filled_img = np.array(filled_images[0])

        reader = PdfReader(filled_pdf_path)
        page = reader.pages[0]
        media_box = page.mediabox
        pdf_width = float(media_box.right) - float(media_box.left)
        pdf_height = float(media_box.top) - float(media_box.bottom)

        def px_to_pdf(x_px, y_px):
            height_px, width_px = filled_img.shape[:2]
            x_pdf = (x_px / width_px) * pdf_width
            y_pdf = pdf_height - ((y_px / height_px) * pdf_height)
            return x_pdf, y_pdf

        # Extract text with OCR
        try:
            ocr_data = pytesseract.image_to_data(
                filled_img, output_type=pytesseract.Output.DICT
            )
            text_positions = {}

            for i, text in enumerate(ocr_data["text"]):
                text = text.strip().lower()
                if text and len(text) > 2:
                    x = ocr_data["left"][i]
                    y = ocr_data["top"][i]
                    w = ocr_data["width"][i]
                    h = ocr_data["height"][i]

                    center_x = x + w / 2
                    center_y = y + h / 2
                    x_pdf, y_pdf = px_to_pdf(center_x, center_y)

                    if text not in text_positions:
                        text_positions[text] = []
                    text_positions[text].append((x_pdf, y_pdf))
        except Exception as e:
            print(f"OCR error: {e}")
            return {}, 0.0

        # Check alignment
        alignment_results = {}
        for field_name, value in data.items():
            if field_name not in positions:
                alignment_results[field_name] = True
                continue

            if not value or str(value).strip() == "" or value in (True, False):
                alignment_results[field_name] = True
                continue

            value_str = str(value).strip().lower()
            expected_x, expected_y, expected_page = positions[field_name]

            found = False
            for text, positions_list in text_positions.items():
                if value_str in text or text in value_str:
                    for x_pdf, y_pdf in positions_list:
                        x_diff = abs(x_pdf - expected_x)
                        y_diff = abs(y_pdf - expected_y)
                        if x_diff <= tolerance and y_diff <= tolerance:
                            found = True
                            break
                    if found:
                        break

            alignment_results[field_name] = found

        found_count = sum(1 for v in alignment_results.values() if v)
        score = found_count / len(alignment_results) if alignment_results else 0.0
        return alignment_results, score

    def auto_fix_alignment(
        self,
        data: dict,
        positions: dict,
        output_path: str,
        max_iterations: int = 20,
        target_score: float = 0.90,
    ) -> dict:
        """Automatically fix alignment iteratively."""
        print(f"Auto-fixing alignment (target: {target_score:.0%})...")

        best_score = 0.0
        best_positions = positions.copy()
        current_positions = positions.copy()

        for iteration in range(max_iterations):
            # Fill form
            temp_output = str(
                Path(output_path).parent / f"temp_{self.template_hash}.pdf"
            )
            self.fill_form(data, current_positions, temp_output)

            # Validate
            results, score = self.validate_alignment(
                temp_output, current_positions, data
            )

            if iteration % 3 == 0:
                found_count = sum(1 for v in results.values() if v)
                print(
                    f"  Iteration {iteration + 1}: {score:.2%} ({found_count}/{len(results)} fields)"
                )

            if score > best_score:
                best_score = score
                best_positions = current_positions.copy()

            if score >= target_score:
                print(f"  ✓ Target reached: {score:.2%}")
                break

            # Calculate corrections
            corrections = self._calculate_corrections(
                temp_output, current_positions, data
            )

            if corrections:
                damping = 0.7
                for field_name, (x_error, y_error) in corrections.items():
                    if field_name in current_positions:
                        x, y, page_idx = current_positions[field_name]
                        max_corr = 50
                        x_corr = max(-max_corr, min(max_corr, -x_error * damping))
                        y_corr = max(-max_corr, min(max_corr, -y_error * damping))
                        current_positions[field_name] = [
                            x + x_corr,
                            y + y_corr,
                            page_idx,
                        ]
            else:
                # Pattern-based adjustment
                misaligned = [k for k, v in results.items() if not v]
                for field in misaligned[:5]:
                    if field in current_positions:
                        x, y, page_idx = current_positions[field]
                        current_positions[field] = [x, y + 20, page_idx]

        # Final fill with best positions
        self.fill_form(data, best_positions, output_path)
        return best_positions

    def _calculate_corrections(
        self, filled_pdf_path: str, positions: dict, data: dict
    ) -> dict[str, tuple[float, float]]:
        """Calculate position corrections."""
        filled_images = convert_from_path(filled_pdf_path, dpi=300)
        if not filled_images:
            return {}

        filled_img = np.array(filled_images[0])

        reader = PdfReader(filled_pdf_path)
        page = reader.pages[0]
        media_box = page.mediabox
        pdf_width = float(media_box.right) - float(media_box.left)
        pdf_height = float(media_box.top) - float(media_box.bottom)

        def px_to_pdf(x_px, y_px):
            height_px, width_px = filled_img.shape[:2]
            x_pdf = (x_px / width_px) * pdf_width
            y_pdf = pdf_height - ((y_px / height_px) * pdf_height)
            return x_pdf, y_pdf

        try:
            ocr_data = pytesseract.image_to_data(
                filled_img, output_type=pytesseract.Output.DICT
            )
            corrections = {}

            for field_name, value in data.items():
                if field_name not in positions:
                    continue

                if not value or str(value).strip() == "" or value in (True, False):
                    continue

                value_str = str(value).strip().lower()
                expected_x, expected_y, expected_page = positions[field_name]

                best_match = None
                best_distance = float("inf")

                for i, text in enumerate(ocr_data["text"]):
                    text_lower = text.strip().lower()
                    if value_str in text_lower or text_lower in value_str:
                        x = ocr_data["left"][i]
                        y = ocr_data["top"][i]
                        w = ocr_data["width"][i]
                        h = ocr_data["height"][i]

                        center_x = x + w / 2
                        center_y = y + h / 2
                        x_pdf, y_pdf = px_to_pdf(center_x, center_y)

                        distance = (
                            (x_pdf - expected_x) ** 2 + (y_pdf - expected_y) ** 2
                        ) ** 0.5
                        if distance < best_distance:
                            best_distance = distance
                            best_match = (x_pdf, y_pdf)

                if best_match and best_distance < 200:
                    x_error = best_match[0] - expected_x
                    y_error = best_match[1] - expected_y
                    corrections[field_name] = (x_error, y_error)

            return corrections
        except Exception:
            return {}


def main():
    parser = argparse.ArgumentParser(
        description="Generic PDF form filler for any static PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Detect fields in template
  python generic-pdf-form-filler.py detect --template form.pdf --output positions.json

  # Fill form with data
  python generic-pdf-form-filler.py fill --template form.pdf --data data.json \\
      --positions positions.json --output filled.pdf

  # Auto-fix alignment
  python generic-pdf-form-filler.py auto-fix --template form.pdf --data data.json \\
      --positions positions.json --output filled.pdf
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command")

    # Detect command
    detect_parser = subparsers.add_parser("detect", help="Detect form fields")
    detect_parser.add_argument("--template", required=True, help="PDF template file")
    detect_parser.add_argument("--output", required=True, help="Output positions JSON")
    detect_parser.add_argument(
        "--page", type=int, default=0, help="Page number (0-indexed)"
    )
    detect_parser.add_argument(
        "--method",
        choices=["hybrid", "lines", "ocr"],
        default="hybrid",
        help="Detection method",
    )

    # Fill command
    fill_parser = subparsers.add_parser("fill", help="Fill form with data")
    fill_parser.add_argument("--template", required=True, help="PDF template file")
    fill_parser.add_argument("--data", required=True, help="Form data JSON")
    fill_parser.add_argument("--positions", required=True, help="Field positions JSON")
    fill_parser.add_argument("--output", required=True, help="Output filled PDF")
    fill_parser.add_argument("--font-size", type=int, default=10, help="Font size")

    # Auto-fix command
    autofix_parser = subparsers.add_parser("auto-fix", help="Auto-fix alignment")
    autofix_parser.add_argument("--template", required=True, help="PDF template file")
    autofix_parser.add_argument("--data", required=True, help="Form data JSON")
    autofix_parser.add_argument(
        "--positions", required=True, help="Field positions JSON"
    )
    autofix_parser.add_argument("--output", required=True, help="Output filled PDF")
    autofix_parser.add_argument(
        "--iterations", type=int, default=20, help="Max iterations"
    )
    autofix_parser.add_argument(
        "--target", type=float, default=0.90, help="Target score"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "detect":
        filler = GenericPDFFormFiller(args.template)
        positions = filler.detect_fields(page_num=args.page, method=args.method)

        with open(args.output, "w") as f:
            json.dump(positions, f, indent=2)

        print(f"\n✓ Detected {len(positions)} fields")
        print(f"✓ Saved to: {args.output}")
        print("\nNext steps:")
        print("  1. Review and adjust positions in JSON file")
        print("  2. Map field keys to your data keys")
        print("  3. Use 'fill' command to fill form")

    elif args.command == "fill":
        filler = GenericPDFFormFiller(args.template)

        with open(args.data) as f:
            data = json.load(f)

        with open(args.positions) as f:
            positions = json.load(f)

        filler.fill_form(data, positions, args.output, font_size=args.font_size)
        print(f"✓ Filled form: {args.output}")

    elif args.command == "auto-fix":
        filler = GenericPDFFormFiller(args.template)

        with open(args.data) as f:
            data = json.load(f)

        with open(args.positions) as f:
            positions = json.load(f)

        best_positions = filler.auto_fix_alignment(
            data,
            positions,
            args.output,
            max_iterations=args.iterations,
            target_score=args.target,
        )

        # Save improved positions
        with open(args.positions, "w") as f:
            json.dump(best_positions, f, indent=2)

        # Final validation
        results, score = filler.validate_alignment(args.output, best_positions, data)
        found_count = sum(1 for v in results.values() if v)

        print(f"\n{'=' * 70}")
        print("Final Results")
        print(f"{'=' * 70}")
        print(f"Final alignment: {score:.2%} ({found_count}/{len(results)} fields)")

        if score >= args.target:
            print("✓ Target achieved!")
        else:
            print(f"⚠ Best achieved: {score:.2%}")

        print(f"\n✓ Output: {args.output}")
        print(f"✓ Updated positions: {args.positions}")


if __name__ == "__main__":
    main()
