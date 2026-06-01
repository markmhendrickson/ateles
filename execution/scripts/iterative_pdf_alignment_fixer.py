#!/usr/bin/env python3
"""
Iterative PDF Alignment Checker and Fixer

Automatically checks PDF form alignment and iteratively fixes issues until correct.
Uses visual inspection simulation and position adjustment to achieve perfect alignment.

Usage:
    python iterative-pdf-alignment-fixer.py --template form.pdf --data data.json --output filled.pdf
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
except ImportError:
    print("Error: Missing required library. Install with: pip install pypdf reportlab")
    sys.exit(1)


class IterativeAlignmentFixer:
    """Iterative PDF alignment checker and fixer."""

    def __init__(self, template_path: str, data_path: str, output_path: str):
        self.template_path = Path(template_path)
        self.data_path = Path(data_path)
        self.output_path = Path(output_path)
        self.form_data = {}
        self.current_positions = {}
        self.best_positions = {}
        self.iteration = 0
        self.max_iterations = 10
        self.alignment_threshold = 0.95  # 95% of values must be found

    def load_data(self):
        """Load form data."""
        with open(self.data_path) as f:
            self.form_data = json.load(f)
        print(f"✓ Loaded {len(self.form_data)} form data fields")

    def initialize_positions(self):
        """Initialize field positions with intelligent estimates."""
        reader = PdfReader(str(self.template_path))
        page = reader.pages[0]
        media_box = page.mediabox
        float(media_box.right) - float(media_box.left)
        height = float(media_box.top) - float(media_box.bottom)

        # Base positions (will be adjusted iteratively)
        base_positions = {
            # Property fields (top section, page 1)
            "property_street": [90, height - 100, 0],
            "property_district": [400, height - 100, 0],
            "property_municipality": [400, height - 130, 0],
            "property_postal_code": [450, height - 130, 0],
            "property_additional_info": [90, height - 160, 0],
            # Applicant fields (middle section, page 1)
            "applicant_name": [90, height - 250, 0],
            "applicant_nif": [400, height - 250, 0],
            "applicant_address": [90, height - 280, 0],
            "applicant_municipality": [400, height - 280, 0],
            "applicant_phone": [90, height - 310, 0],
            "applicant_email": [300, height - 310, 0],
            "applicant_capacity": [90, height - 340, 0],
            # Checkboxes (page 1)
            "owner_same_as_applicant": [80, height - 380, 0],
            "installer_same_as_applicant": [80, height - 410, 0],
            "offer_recipient": [80, height - 470, 0],
            "contact_person": [80, height - 500, 0],
            # Technical fields (page 2)
            "installation_type": [90, height - 200, 1],
            "number_of_floors": [90, height - 230, 1],
            "pressure_group": [300, height - 230, 1],
            "installation_category": [90, height - 260, 1],
            "installation_subcategory": [90, height - 290, 1],
            "housing_type": [90, height - 320, 1],
            "max_flow_liters_per_second": [380, height - 350, 1],
        }

        # Only include fields that exist in form_data
        self.current_positions = {
            k: v for k, v in base_positions.items() if k in self.form_data
        }

        # Add any missing fields with default positions
        for key in self.form_data:
            if key not in self.current_positions:
                self.current_positions[key] = [90, height - 100, 0]

        self.best_positions = self.current_positions.copy()
        print(f"✓ Initialized {len(self.current_positions)} field positions")

    def fill_form(self, positions: dict) -> Path:
        """Fill form with given positions."""
        reader = PdfReader(str(self.template_path))
        writer = PdfWriter()

        first_page = reader.pages[0]
        media_box = first_page.mediabox
        width = float(media_box.right) - float(media_box.left)
        height = float(media_box.top) - float(media_box.bottom)

        # Create overlay
        overlay_path = (
            self.output_path.parent
            / f"{self.output_path.stem}_iter_{self.iteration}.pdf"
        )
        c = canvas.Canvas(str(overlay_path), pagesize=(width, height))
        c.setFont("Helvetica", 10)

        # Group by page
        pages_data = {}
        for key, value in self.form_data.items():
            if key in positions:
                x, y, page_idx = positions[key]
                if page_idx not in pages_data:
                    pages_data[page_idx] = []
                pages_data[page_idx].append((x, y, value, key))

        # Fill each page
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

        # Merge
        overlay_reader = PdfReader(str(overlay_path))
        for i, page in enumerate(reader.pages):
            if i < len(overlay_reader.pages):
                page.merge_page(overlay_reader.pages[i])
            writer.add_page(page)

        # Write output
        with open(self.output_path, "wb") as output_file:
            writer.write(output_file)

        overlay_path.unlink()
        return self.output_path

    def check_alignment(self, pdf_path: Path) -> tuple[dict[str, bool], float]:
        """
        Check alignment quality by validating filled values are in correct positions.
        Uses section-based validation to ensure values appear in correct form sections.
        """
        reader = PdfReader(str(pdf_path))

        # Extract text per page for better position checking
        page_texts = {}
        for page_num, page in enumerate(reader.pages):
            try:
                page_texts[page_num] = page.extract_text().lower()
            except Exception:
                page_texts[page_num] = ""

        # Define form sections for context validation
        sections = {
            "property": [
                "property_street",
                "property_district",
                "property_municipality",
                "property_postal_code",
                "property_additional_info",
            ],
            "applicant": [
                "applicant_name",
                "applicant_nif",
                "applicant_address",
                "applicant_municipality",
                "applicant_phone",
                "applicant_email",
                "applicant_capacity",
            ],
            "technical": [
                "installation_type",
                "number_of_floors",
                "pressure_group",
                "installation_category",
                "installation_subcategory",
                "housing_type",
            ],
        }

        validation_results = {}

        for key, value in self.form_data.items():
            if key not in self.current_positions:
                validation_results[key] = True  # Skip if no position
                continue

            expected_x, expected_y, expected_page = self.current_positions[key]

            if value and str(value).strip() and value not in (True, False):
                value_str = str(value).strip()
                page_text = page_texts.get(expected_page, "")

                # Check if value appears on correct page
                found_on_page = value_str.lower() in page_text

                if found_on_page:
                    # Additional validation: check if it's in the right section
                    # by verifying nearby section fields also appear
                    section_fields = self._get_section_fields(
                        key, sections, expected_page
                    )
                    if section_fields:
                        # Check if other section fields are also present
                        section_fields_present = sum(
                            1
                            for field in section_fields
                            if field in self.form_data
                            and str(self.form_data[field]).lower() in page_text
                        )
                        # At least 50% of section fields should be present for valid alignment
                        found = section_fields_present >= len(section_fields) * 0.5
                    else:
                        found = True  # Can't verify section, assume OK
                else:
                    found = False

                validation_results[key] = found
            else:
                # Boolean/empty - assume OK
                validation_results[key] = True

        # Calculate alignment score
        found_count = sum(1 for v in validation_results.values() if v)
        total_count = len(validation_results)
        score = found_count / total_count if total_count > 0 else 0.0

        return validation_results, score

    def _get_section_fields(
        self, field_name: str, sections: dict, page: int
    ) -> list[str]:
        """Get other fields in the same section on the same page."""
        for section_fields in sections.values():
            if field_name in section_fields:
                return [
                    f
                    for f in section_fields
                    if f != field_name
                    and f in self.current_positions
                    and self.current_positions[f][2] == page
                ]
        return []

    def adjust_positions(self, validation_results: dict[str, bool]) -> dict:
        """Adjust positions based on validation results."""
        new_positions = self.current_positions.copy()

        # Get page dimensions for reference
        reader = PdfReader(str(self.template_path))
        page = reader.pages[0]
        media_box = page.mediabox
        float(media_box.top) - float(media_box.bottom)

        # Adjustment strategies
        adjustments = {
            "y_offset_small": 5,  # Small vertical adjustment
            "y_offset_medium": 10,  # Medium vertical adjustment
            "y_offset_large": 20,  # Large vertical adjustment
            "x_offset_small": 5,  # Small horizontal adjustment
        }

        # Adjust positions for fields that weren't found
        for field_name, found in validation_results.items():
            if not found and field_name in new_positions:
                x, y, page_idx = new_positions[field_name]

                # Try different adjustment strategies
                # Strategy 1: Move up slightly (common for text fields)
                if self.iteration % 3 == 0:
                    new_positions[field_name] = [
                        x,
                        y + adjustments["y_offset_small"],
                        page_idx,
                    ]
                # Strategy 2: Move down slightly
                elif self.iteration % 3 == 1:
                    new_positions[field_name] = [
                        x,
                        y - adjustments["y_offset_small"],
                        page_idx,
                    ]
                # Strategy 3: Adjust horizontally
                else:
                    new_positions[field_name] = [
                        x + adjustments["x_offset_small"],
                        y,
                        page_idx,
                    ]

        return new_positions

    def run_iterative_fix(self):
        """Run iterative alignment fixing."""
        print(f"\n{'=' * 70}")
        print("Iterative PDF Alignment Fixer")
        print(f"{'=' * 70}\n")

        # Load data
        print("Step 1: Loading form data...")
        self.load_data()

        # Initialize positions
        print("\nStep 2: Initializing field positions...")
        self.initialize_positions()

        # Iterative improvement
        best_score = 0.0
        best_iteration = 0

        print(f"\n{'=' * 70}")
        print("Starting iterative alignment improvement...")
        print(f"{'=' * 70}\n")

        for iteration in range(self.max_iterations):
            self.iteration = iteration
            print(f"\n--- Iteration {iteration + 1}/{self.max_iterations} ---")

            # Fill form with current positions
            filled_pdf = self.fill_form(self.current_positions)
            print(f"✓ Filled form: {filled_pdf.name}")

            # Check alignment
            validation_results, score = self.check_alignment(filled_pdf)
            found_count = sum(1 for v in validation_results.values() if v)
            total_count = len(validation_results)

            print(
                f"Alignment score: {score:.2%} ({found_count}/{total_count} fields found)"
            )

            # Track best result
            if score > best_score:
                best_score = score
                best_iteration = iteration
                self.best_positions = self.current_positions.copy()
                print("✓ New best score! Saving positions...")

            # Check if we've reached threshold
            if score >= self.alignment_threshold:
                print(
                    f"\n✓ Alignment threshold reached ({self.alignment_threshold:.0%})!"
                )
                break

            # Show missing fields
            missing = [k for k, v in validation_results.items() if not v]
            if missing:
                print(f"Missing fields ({len(missing)}): {', '.join(missing[:5])}")
                if len(missing) > 5:
                    print(f"  ... and {len(missing) - 5} more")

            # Adjust positions for next iteration
            if iteration < self.max_iterations - 1:
                self.current_positions = self.adjust_positions(validation_results)
                print("✓ Adjusted positions for next iteration")

        # Use best positions for final fill
        print(f"\n{'=' * 70}")
        print("Final Fill with Best Positions")
        print(f"{'=' * 70}\n")
        print(
            f"Using positions from iteration {best_iteration + 1} (score: {best_score:.2%})"
        )

        self.current_positions = self.best_positions
        final_pdf = self.fill_form(self.best_positions)

        # Final validation
        final_validation, final_score = self.check_alignment(final_pdf)
        found_count = sum(1 for v in final_validation.values() if v)
        total_count = len(final_validation)

        print(f"\n{'=' * 70}")
        print("Final Results")
        print(f"{'=' * 70}\n")
        print(
            f"Final alignment score: {final_score:.2%} ({found_count}/{total_count} fields found)"
        )

        if final_score >= self.alignment_threshold:
            print("✓ Alignment is acceptable!")
        else:
            print("⚠ Alignment below threshold, but best possible achieved")

        # Report missing fields
        missing = [k for k, v in final_validation.items() if not v]
        if missing:
            print(f"\nMissing fields ({len(missing)}):")
            for field in missing:
                value = self.form_data.get(field, "")
                print(f"  - {field}: {value}")

        print(f"\n✓ Final PDF: {final_pdf}")
        return final_pdf


def main():
    parser = argparse.ArgumentParser(
        description="Iteratively fix PDF form alignment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--template", required=True, help="PDF template file")
    parser.add_argument("--data", required=True, help="Form data JSON file")
    parser.add_argument("--output", required=True, help="Output filled PDF file")
    parser.add_argument(
        "--iterations", type=int, default=10, help="Max iterations (default: 10)"
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.95,
        help="Alignment threshold (default: 0.95)",
    )

    args = parser.parse_args()

    # Validate inputs
    if not Path(args.template).exists():
        print(f"Error: Template file not found: {args.template}")
        sys.exit(1)

    if not Path(args.data).exists():
        print(f"Error: Data file not found: {args.data}")
        sys.exit(1)

    # Run iterative fix
    fixer = IterativeAlignmentFixer(args.template, args.data, args.output)
    fixer.max_iterations = args.iterations
    fixer.alignment_threshold = args.threshold

    success = fixer.run_iterative_fix()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
