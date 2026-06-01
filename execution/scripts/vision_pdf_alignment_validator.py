#!/usr/bin/env python3
"""
Vision API-based PDF Alignment Validator

Uses Google Cloud Vision API to:
1. Detect field positions in blank form (by finding field labels)
2. Validate alignment of filled PDF (by comparing text positions)
3. Calculate correction offsets for misaligned fields

Usage:
    python vision_pdf_alignment_validator.py --blank blank.pdf --filled filled.pdf --data data.json [--output report.json]
"""

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

try:
    import io

    from google.cloud import vision
    from pdf2image import convert_from_path
    from PIL import Image
except ImportError as e:
    print(
        "Error: Missing required library. Install with: pip install google-cloud-vision pdf2image pillow"
    )
    print(f"Missing: {e}")
    sys.exit(1)


@dataclass
class TextRegion:
    """Text region with bounding box coordinates."""

    text: str
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    page: int
    confidence: float = 1.0

    @property
    def center_x(self) -> float:
        return (self.x_min + self.x_max) / 2

    @property
    def center_y(self) -> float:
        return (self.y_min + self.y_max) / 2


@dataclass
class FieldAlignment:
    """Alignment information for a form field."""

    field_name: str
    expected_value: str
    found: bool
    expected_x: float | None = None
    expected_y: float | None = None
    actual_x: float | None = None
    actual_y: float | None = None
    x_offset: float | None = None
    y_offset: float | None = None
    distance: float | None = None
    tolerance: float = 30.0
    aligned: bool = False


class VisionPDFAlignmentValidator:
    """Vision API-based PDF alignment validator."""

    def __init__(self, credentials_path: str | None = None):
        """Initialize Vision API client."""
        self.client = self._init_vision_client(credentials_path)
        self.text_regions_blank: dict[int, list[TextRegion]] = {}
        self.text_regions_filled: dict[int, list[TextRegion]] = {}
        self.field_label_positions: dict[str, TextRegion] = {}

    def _init_vision_client(
        self, credentials_path: str | None
    ) -> vision.ImageAnnotatorClient:
        """Initialize Vision API client with multiple authentication methods."""
        # Method 1: Explicit credentials file
        if credentials_path:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(
                Path(credentials_path).expanduser().resolve()
            )
            try:
                return vision.ImageAnnotatorClient()
            except Exception as e:
                print(f"Error with credentials file: {e}")

        # Method 2: Application default credentials
        try:
            return vision.ImageAnnotatorClient()
        except Exception as e:
            print(f"Error initializing Vision API client: {e}")
            print("\nSetup options:")
            print("1. Use application default credentials (recommended):")
            print("   gcloud auth application-default login")
            print("\n2. Create service account and use --credentials flag")
            sys.exit(1)

    def extract_text_regions(
        self, pdf_path: Path, dpi: int = 300
    ) -> dict[int, list[TextRegion]]:
        """
        Extract text regions with bounding boxes from PDF using Vision API.
        Returns dict mapping page number to list of TextRegion objects.
        """
        print(f"Converting PDF to images: {pdf_path}")
        images = convert_from_path(str(pdf_path), dpi=dpi)

        text_regions = {}
        for page_num, image in enumerate(images, 1):
            print(f"Extracting text from page {page_num}/{len(images)}...")
            regions = self._extract_text_from_image(image, page_num)
            text_regions[page_num] = regions
            print(f"  Found {len(regions)} text regions")

        return text_regions

    def _extract_text_from_image(
        self, image: Image.Image, page_num: int
    ) -> list[TextRegion]:
        """Extract text regions from a single image using Vision API."""
        # Convert PIL Image to bytes
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format="PNG")
        img_byte_arr.seek(0)

        # Get image dimensions for coordinate conversion
        img_height = image.height

        # Create Vision API image
        vision_image = vision.Image(content=img_byte_arr.read())

        # Perform text detection with document text detection for better results
        response = self.client.document_text_detection(image=vision_image)

        regions = []
        if response.full_text_annotation:
            # Use document text detection for better structure
            for page in response.full_text_annotation.pages:
                for block in page.blocks:
                    for paragraph in block.paragraphs:
                        for word in paragraph.words:
                            # Get bounding box
                            vertices = word.bounding_box.vertices
                            if len(vertices) >= 4:
                                # Convert to PDF coordinates (0,0 at bottom-left)
                                x_coords = [v.x for v in vertices]
                                y_coords = [v.y for v in vertices]

                                x_min = min(x_coords)
                                x_max = max(x_coords)
                                y_min = min(y_coords)
                                y_max = max(y_coords)

                                # Convert Y coordinate (Vision API uses top-left, PDF uses bottom-left)
                                y_min_pdf = img_height - y_max
                                y_max_pdf = img_height - y_min

                                # Extract word text
                                word_text = "".join(
                                    [symbol.text for symbol in word.symbols]
                                )

                                if word_text.strip():
                                    regions.append(
                                        TextRegion(
                                            text=word_text.strip(),
                                            x_min=x_min,
                                            y_min=y_min_pdf,
                                            x_max=x_max,
                                            y_max=y_max_pdf,
                                            page=page_num,
                                            confidence=(
                                                word.confidence
                                                if hasattr(word, "confidence")
                                                else 1.0
                                            ),
                                        )
                                    )

        # Fallback to regular text detection if document detection didn't work
        if not regions:
            response = self.client.text_detection(image=vision_image)
            if response.text_annotations:
                # First annotation is the full text, skip it
                for annotation in response.text_annotations[1:]:
                    vertices = annotation.bounding_poly.vertices
                    if len(vertices) >= 4:
                        x_coords = [v.x for v in vertices]
                        y_coords = [v.y for v in vertices]

                        x_min = min(x_coords)
                        x_max = max(x_coords)
                        y_min = min(y_coords)
                        y_max = max(y_coords)

                        # Convert Y coordinate
                        y_min_pdf = img_height - y_max
                        y_max_pdf = img_height - y_min

                        regions.append(
                            TextRegion(
                                text=annotation.description,
                                x_min=x_min,
                                y_min=y_min_pdf,
                                x_max=x_max,
                                y_max=y_max_pdf,
                                page=page_num,
                            )
                        )

        return regions

    def detect_field_labels(
        self, blank_pdf_path: Path, field_labels: dict[str, str]
    ) -> dict[str, TextRegion]:
        """
        Detect field label positions in blank form.

        Args:
            blank_pdf_path: Path to blank form PDF
            field_labels: Dict mapping field names to label text to search for
                          e.g., {"property_street": "Calle", "applicant_name": "Nombre"}

        Returns:
            Dict mapping field names to TextRegion of label position
        """
        print("\n=== Detecting Field Labels in Blank Form ===")
        self.text_regions_blank = self.extract_text_regions(blank_pdf_path)

        field_positions = {}

        for field_name, label_text in field_labels.items():
            print(f"\nSearching for field label: {field_name} -> '{label_text}'")

            # Search for label text in all pages
            float("inf")

            for page_num, regions in self.text_regions_blank.items():
                for region in regions:
                    # Check if label text appears in this region
                    if (
                        label_text.lower() in region.text.lower()
                        or region.text.lower() in label_text.lower()
                    ):
                        # Found potential label - field is typically to the right or below
                        # For now, use the label position as reference
                        if region.text.lower().strip() == label_text.lower().strip():
                            # Exact match - this is likely the label
                            field_positions[field_name] = region
                            print(
                                f"  ✓ Found on page {page_num} at ({region.center_x:.1f}, {region.center_y:.1f})"
                            )
                            break

                if field_name in field_positions:
                    break

            if field_name not in field_positions:
                print(f"  ⚠ Label not found for {field_name}")

        return field_positions

    def validate_alignment(
        self,
        filled_pdf_path: Path,
        form_data: dict,
        expected_positions: dict[str, list[float]] | None = None,
        tolerance: float = 30.0,
    ) -> list[FieldAlignment]:
        """
        Validate alignment of filled PDF by comparing actual text positions to expected positions.

        Args:
            filled_pdf_path: Path to filled PDF
            form_data: Dict of field names to expected values
            expected_positions: Optional dict of field names to [x, y, page] expected positions
            tolerance: Maximum distance in pixels for alignment to be considered correct

        Returns:
            List of FieldAlignment objects with validation results
        """
        print("\n=== Validating Alignment of Filled PDF ===")
        self.text_regions_filled = self.extract_text_regions(filled_pdf_path)

        alignments = []

        for field_name, expected_value in form_data.items():
            # Skip boolean and empty values
            if isinstance(expected_value, bool) or not str(expected_value).strip():
                alignments.append(
                    FieldAlignment(
                        field_name=field_name,
                        expected_value=str(expected_value),
                        found=False,
                        aligned=True,  # Skip validation for these
                    )
                )
                continue

            expected_value_str = str(expected_value).strip().lower()

            # Get expected position if provided
            expected_x = None
            expected_y = None
            expected_page = 0

            if expected_positions and field_name in expected_positions:
                expected_x, expected_y, expected_page = expected_positions[field_name]

            # Search for value in filled PDF
            found_region = None
            min_distance = float("inf")

            for page_num, regions in self.text_regions_filled.items():
                for region in regions:
                    region_text = region.text.lower()

                    # Check for exact or partial match
                    if (
                        expected_value_str == region_text
                        or expected_value_str in region_text
                        or region_text in expected_value_str
                    ):
                        # Calculate distance from expected position if available
                        if expected_x is not None and expected_y is not None:
                            distance = (
                                (region.center_x - expected_x) ** 2
                                + (region.center_y - expected_y) ** 2
                            ) ** 0.5

                            if distance < min_distance:
                                min_distance = distance
                                found_region = region
                        else:
                            # No expected position, just use first match
                            if not found_region:
                                found_region = region
                                break

            # Create alignment result
            if found_region:
                x_offset = None
                y_offset = None
                distance = None
                aligned = False

                if expected_x is not None and expected_y is not None:
                    x_offset = found_region.center_x - expected_x
                    y_offset = found_region.center_y - expected_y
                    distance = min_distance
                    aligned = distance <= tolerance
                else:
                    # Found but no expected position to compare
                    aligned = True

                alignments.append(
                    FieldAlignment(
                        field_name=field_name,
                        expected_value=str(expected_value),
                        found=True,
                        expected_x=expected_x,
                        expected_y=expected_y,
                        actual_x=found_region.center_x,
                        actual_y=found_region.center_y,
                        x_offset=x_offset,
                        y_offset=y_offset,
                        distance=distance,
                        tolerance=tolerance,
                        aligned=aligned,
                    )
                )
            else:
                alignments.append(
                    FieldAlignment(
                        field_name=field_name,
                        expected_value=str(expected_value),
                        found=False,
                        expected_x=expected_x,
                        expected_y=expected_y,
                        tolerance=tolerance,
                        aligned=False,
                    )
                )

        return alignments

    def calculate_corrections(
        self, alignments: list[FieldAlignment]
    ) -> dict[str, dict[str, float]]:
        """
        Calculate correction offsets for misaligned fields.

        Returns:
            Dict mapping field names to correction offsets
        """
        corrections = {}

        for alignment in alignments:
            if (
                not alignment.aligned
                and alignment.x_offset is not None
                and alignment.y_offset is not None
            ):
                corrections[alignment.field_name] = {
                    "x_offset": -alignment.x_offset,  # Negate to correct
                    "y_offset": -alignment.y_offset,
                    "current_x": alignment.actual_x,
                    "current_y": alignment.actual_y,
                    "target_x": alignment.expected_x,
                    "target_y": alignment.expected_y,
                }

        return corrections

    def generate_report(
        self,
        alignments: list[FieldAlignment],
        corrections: dict[str, dict[str, float]],
        output_path: Path | None = None,
    ) -> dict:
        """Generate alignment validation report."""
        total_fields = len(alignments)
        found_fields = sum(1 for a in alignments if a.found)
        aligned_fields = sum(1 for a in alignments if a.aligned)
        misaligned_fields = [a for a in alignments if a.found and not a.aligned]

        report = {
            "summary": {
                "total_fields": total_fields,
                "found_fields": found_fields,
                "aligned_fields": aligned_fields,
                "misaligned_fields": len(misaligned_fields),
                "missing_fields": total_fields - found_fields,
                "alignment_score": (
                    (aligned_fields / total_fields * 100) if total_fields > 0 else 0
                ),
            },
            "alignments": [asdict(a) for a in alignments],
            "corrections": corrections,
            "misaligned_details": [asdict(a) for a in misaligned_fields],
        }

        # Print summary
        print("\n" + "=" * 60)
        print("ALIGNMENT VALIDATION REPORT")
        print("=" * 60)
        print(f"Total fields: {total_fields}")
        print(f"Found fields: {found_fields}")
        print(f"Aligned fields: {aligned_fields}")
        print(f"Misaligned fields: {len(misaligned_fields)}")
        print(f"Missing fields: {total_fields - found_fields}")
        print(f"Alignment score: {report['summary']['alignment_score']:.1f}%")
        print("=" * 60)

        if misaligned_fields:
            print("\nMisaligned fields:")
            for field in misaligned_fields:
                print(f"  - {field.field_name}:")
                print(
                    f"      Expected: ({field.expected_x:.1f}, {field.expected_y:.1f})"
                )
                print(f"      Actual:   ({field.actual_x:.1f}, {field.actual_y:.1f})")
                print(f"      Offset:   ({field.x_offset:+.1f}, {field.y_offset:+.1f})")
                print(f"      Distance: {field.distance:.1f} pixels")

        if corrections:
            print("\nCorrection offsets needed:")
            for field_name, offsets in corrections.items():
                print(f"  - {field_name}:")
                print(f"      X correction: {offsets['x_offset']:+.1f}")
                print(f"      Y correction: {offsets['y_offset']:+.1f}")

        # Save to file if requested
        if output_path:
            with open(output_path, "w") as f:
                json.dump(report, f, indent=2)
            print(f"\nReport saved to: {output_path}")

        return report


def main():
    parser = argparse.ArgumentParser(
        description="Validate PDF form alignment using Google Cloud Vision API"
    )
    parser.add_argument(
        "--blank", type=Path, required=True, help="Path to blank form PDF"
    )
    parser.add_argument("--filled", type=Path, required=True, help="Path to filled PDF")
    parser.add_argument(
        "--data", type=Path, required=True, help="Path to form data JSON file"
    )
    parser.add_argument(
        "--positions", type=Path, help="Path to expected positions JSON file (optional)"
    )
    parser.add_argument(
        "--labels",
        type=Path,
        help="Path to field labels JSON file (optional, for field detection)",
    )
    parser.add_argument(
        "--output", type=Path, help="Path to save validation report JSON"
    )
    parser.add_argument(
        "--credentials", help="Path to Google Cloud service account JSON key"
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=30.0,
        help="Alignment tolerance in pixels (default: 30.0)",
    )

    args = parser.parse_args()

    # Load form data
    with open(args.data) as f:
        form_data = json.load(f)

    # Load expected positions if provided
    expected_positions = None
    if args.positions:
        with open(args.positions) as f:
            expected_positions = json.load(f)

    # Load field labels if provided
    field_labels = None
    if args.labels:
        with open(args.labels) as f:
            field_labels = json.load(f)

    # Initialize validator
    validator = VisionPDFAlignmentValidator(credentials_path=args.credentials)

    # Detect field labels if labels file provided
    if field_labels:
        label_positions = validator.detect_field_labels(args.blank, field_labels)
        print(f"\nDetected {len(label_positions)} field labels")

    # Validate alignment
    alignments = validator.validate_alignment(
        args.filled,
        form_data,
        expected_positions=expected_positions,
        tolerance=args.tolerance,
    )

    # Calculate corrections
    corrections = validator.calculate_corrections(alignments)

    # Generate report
    report = validator.generate_report(alignments, corrections, output_path=args.output)

    # Exit with error code if alignment is poor
    if report["summary"]["alignment_score"] < 80.0:
        print("\n⚠ Warning: Alignment score is below 80%")
        sys.exit(1)
    else:
        print("\n✓ Alignment validation complete")
        sys.exit(0)


if __name__ == "__main__":
    main()
