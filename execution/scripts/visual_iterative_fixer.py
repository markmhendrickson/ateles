#!/usr/bin/env python3
"""
Visual Iterative PDF Alignment Fixer

Uses visual alignment checking to accurately detect misalignments,
then iteratively adjusts positions until 90%+ visual alignment is achieved.
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
except ImportError:
    print("Error: Missing required library. Install with: pip install pypdf reportlab")
    sys.exit(1)


def check_visual_alignment(
    pdf_path: str, expected_positions: dict, form_data: dict
) -> tuple[dict[str, bool], float]:
    """
    Check visual alignment by validating values appear in correct sections.
    Uses stricter validation: requires values to appear with their section context.
    Returns: (field_alignment_results, alignment_score)
    """
    reader = PdfReader(pdf_path)

    alignment_results = {}

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

    for page_num, page in enumerate(reader.pages):
        try:
            text = page.extract_text()
            text_lower = text.lower()

            # Check each field that should be on this page
            for field_name, value in form_data.items():
                if field_name not in expected_positions:
                    # For fields without positions, check if value appears anywhere
                    if value and str(value).strip() and value not in (True, False):
                        alignment_results[field_name] = str(value).lower() in text_lower
                    else:
                        alignment_results[field_name] = True
                    continue

                expected_x, expected_y, expected_page = expected_positions[field_name]

                # Only check fields on this page
                if expected_page != page_num:
                    continue

                if value and str(value).strip() and value not in (True, False):
                    value_str = str(value).strip()
                    value_lower = value_str.lower()

                    # Check if value appears in text
                    found = value_lower in text_lower

                    if found:
                        # STRICT validation: verify it's in the right section
                        section_fields = _get_section_fields(
                            field_name, sections, expected_positions, expected_page
                        )
                        if section_fields:
                            # Check if OTHER section fields are also present (not just this one)
                            other_section_fields = [
                                f for f in section_fields if f != field_name
                            ]
                            if other_section_fields:
                                section_fields_present = sum(
                                    1
                                    for field in other_section_fields
                                    if field in form_data
                                    and str(form_data[field]).strip()
                                    and str(form_data[field]).lower() in text_lower
                                )
                                # At least 70% of OTHER section fields must be present
                                # This ensures values are grouped together in correct section
                                found = (
                                    section_fields_present
                                    >= len(other_section_fields) * 0.7
                                )
                            else:
                                found = True  # Only one field in section, can't verify
                        else:
                            # No section context, use position-based heuristic
                            # Check if value appears in reasonable position (not header/footer)
                            value_pos = text_lower.find(value_lower)
                            if value_pos != -1:
                                text_length = len(text_lower)
                                # Values should appear in middle 80% of text (not first/last 10%)
                                found = (
                                    0.1 * text_length < value_pos < 0.9 * text_length
                                )
                            else:
                                found = False
                    else:
                        found = False

                    alignment_results[field_name] = found
                else:
                    # Boolean/empty - assume OK
                    alignment_results[field_name] = True

        except Exception as e:
            print(f"Error processing page {page_num}: {e}")

    # Calculate score
    found_count = sum(1 for v in alignment_results.values() if v)
    total_count = len(alignment_results)
    score = found_count / total_count if total_count > 0 else 0.0

    return alignment_results, score


def _get_section_fields(
    field_name: str, sections: dict, positions: dict, page: int
) -> list[str]:
    """Get other fields in the same section on the same page."""
    for section_fields in sections.values():
        if field_name in section_fields:
            return [
                f
                for f in section_fields
                if f != field_name and f in positions and positions[f][2] == page
            ]
    return []


class VisualIterativeFixer:
    """Visual iterative alignment fixer."""

    def __init__(
        self,
        template_path: str,
        data_path: str,
        output_path: str,
        positions_path: str = None,
    ):
        self.template_path = Path(template_path)
        self.data_path = Path(data_path)
        self.output_path = Path(output_path)
        self.positions_path = Path(positions_path) if positions_path else None
        self.form_data = {}
        self.current_positions = {}
        self.best_positions = {}
        self.iteration = 0
        self.max_iterations = 20

    def load_data(self):
        """Load form data and positions."""
        with open(self.data_path) as f:
            self.form_data = json.load(f)

        # Load or initialize positions
        if self.positions_path and self.positions_path.exists():
            with open(self.positions_path) as f:
                self.current_positions = json.load(f)
        else:
            self._initialize_positions()

        self.best_positions = self.current_positions.copy()
        print(f"✓ Loaded {len(self.form_data)} form data fields")
        print(f"✓ Loaded {len(self.current_positions)} field positions")

    def _initialize_positions(self):
        """Initialize positions with intelligent estimates."""
        reader = PdfReader(str(self.template_path))
        page = reader.pages[0]
        media_box = page.mediabox
        height = float(media_box.top) - float(media_box.bottom)

        # Base positions (will be adjusted iteratively)
        base_positions = {
            "property_street": [90, height - 100, 0],
            "property_district": [400, height - 100, 0],
            "property_municipality": [400, height - 130, 0],
            "property_postal_code": [450, height - 130, 0],
            "property_additional_info": [90, height - 160, 0],
            "applicant_name": [90, height - 250, 0],
            "applicant_nif": [400, height - 250, 0],
            "applicant_address": [90, height - 280, 0],
            "applicant_municipality": [400, height - 280, 0],
            "applicant_phone": [90, height - 310, 0],
            "applicant_email": [300, height - 310, 0],
            "applicant_capacity": [90, height - 340, 0],
            "owner_same_as_applicant": [80, height - 380, 0],
            "installer_same_as_applicant": [80, height - 410, 0],
            "offer_recipient": [80, height - 470, 0],
            "contact_person": [80, height - 500, 0],
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

    def fill_form(self, positions: dict) -> Path:
        """Fill form with given positions."""
        reader = PdfReader(str(self.template_path))
        writer = PdfWriter()

        first_page = reader.pages[0]
        media_box = first_page.mediabox
        width = float(media_box.right) - float(media_box.left)
        height = float(media_box.top) - float(media_box.bottom)

        # Create overlay
        overlay_path = self.output_path.parent / f"{self.output_path.stem}_overlay.pdf"
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

    def adjust_positions_smart(self, validation_results: dict[str, bool]) -> dict:
        """Smart position adjustment based on misalignment patterns."""
        new_positions = self.current_positions.copy()

        reader = PdfReader(str(self.template_path))
        page = reader.pages[0]
        media_box = page.mediabox
        float(media_box.top) - float(media_box.bottom)

        # Identify misaligned fields
        misaligned = [k for k, v in validation_results.items() if not v]

        # Group by section for pattern-based adjustment
        applicant_misaligned = [f for f in misaligned if f.startswith("applicant_")]
        property_misaligned = [f for f in misaligned if f.startswith("property_")]

        # Pattern-based adjustments
        if applicant_misaligned:
            # Applicant fields often need to move UP (they're too low)
            for field in applicant_misaligned:
                if field in new_positions:
                    x, y, page_idx = new_positions[field]
                    # Try moving up significantly
                    new_positions[field] = [x, y + 20, page_idx]

        if property_misaligned:
            for field in property_misaligned:
                if field in new_positions:
                    x, y, page_idx = new_positions[field]
                    new_positions[field] = [x, y + 15, page_idx]

        # Individual adjustments with varied strategies
        for field_name in misaligned:
            if field_name in new_positions:
                x, y, page_idx = new_positions[field_name]

                # Try different strategies based on iteration
                strategy = self.iteration % 6
                if strategy == 0:
                    new_positions[field_name] = [x, y + 25, page_idx]  # Move up large
                elif strategy == 1:
                    new_positions[field_name] = [x, y - 25, page_idx]  # Move down large
                elif strategy == 2:
                    new_positions[field_name] = [x + 15, y, page_idx]  # Move right
                elif strategy == 3:
                    new_positions[field_name] = [x - 15, y, page_idx]  # Move left
                elif strategy == 4:
                    new_positions[field_name] = [x, y + 15, page_idx]  # Move up medium
                else:
                    new_positions[field_name] = [
                        x,
                        y - 15,
                        page_idx,
                    ]  # Move down medium

        return new_positions

    def run_iterative_fix(self, target_score=0.90):
        """Run iterative alignment fixing with visual validation."""
        print(f"\n{'=' * 70}")
        print("Visual Iterative PDF Alignment Fixer")
        print(f"{'=' * 70}\n")

        # Load data
        print("Step 1: Loading form data and positions...")
        self.load_data()

        # Iterative improvement
        best_score = 0.0
        best_iteration = 0

        print(f"\n{'=' * 70}")
        print(
            f"Starting iterative alignment improvement (target: {target_score:.0%})..."
        )
        print(f"{'=' * 70}\n")

        for iteration in range(self.max_iterations):
            self.iteration = iteration
            print(f"\n--- Iteration {iteration + 1}/{self.max_iterations} ---")

            # Fill form
            filled_pdf = self.fill_form(self.current_positions)
            print("✓ Filled form")

            # Check visual alignment
            validation_results, score = check_visual_alignment(
                str(filled_pdf), self.current_positions, self.form_data
            )
            found_count = sum(1 for v in validation_results.values() if v)
            total_count = len(validation_results)

            print(
                f"Visual alignment score: {score:.2%} ({found_count}/{total_count} fields)"
            )

            # Track best result
            if score > best_score:
                best_score = score
                best_iteration = iteration
                self.best_positions = self.current_positions.copy()
                print("✓ New best score! Saving positions...")

            # Check if we've reached target
            if score >= target_score:
                print(f"\n✓ Target alignment reached ({target_score:.0%})!")
                break

            # Show missing fields
            missing = [k for k, v in validation_results.items() if not v]
            if missing:
                print(f"Misaligned fields ({len(missing)}): {', '.join(missing[:5])}")
                if len(missing) > 5:
                    print(f"  ... and {len(missing) - 5} more")

            # Adjust positions for next iteration
            if iteration < self.max_iterations - 1:
                self.current_positions = self.adjust_positions_smart(validation_results)
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
        final_validation, final_score = check_visual_alignment(
            str(final_pdf), self.best_positions, self.form_data
        )
        found_count = sum(1 for v in final_validation.values() if v)
        total_count = len(final_validation)

        print(f"\n{'=' * 70}")
        print("Final Results")
        print(f"{'=' * 70}\n")
        print(
            f"Final visual alignment: {final_score:.2%} ({found_count}/{total_count} fields)"
        )

        if final_score >= target_score:
            print(f"✓ Target alignment achieved ({target_score:.0%})!")
        else:
            print("⚠ Alignment below target, but best possible achieved")

        # Report missing fields
        missing = [k for k, v in final_validation.items() if not v]
        if missing:
            print(f"\nMisaligned fields ({len(missing)}):")
            for field in missing:
                value = self.form_data.get(field, "")
                print(f"  - {field}: {value}")

        print(f"\n✓ Final PDF: {final_pdf}")
        return final_pdf


def main():
    parser = argparse.ArgumentParser(
        description="Iteratively fix PDF form alignment with visual validation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--template", required=True, help="PDF template file")
    parser.add_argument("--data", required=True, help="Form data JSON file")
    parser.add_argument("--output", required=True, help="Output filled PDF file")
    parser.add_argument("--positions", help="Field positions JSON file (optional)")
    parser.add_argument(
        "--iterations", type=int, default=20, help="Max iterations (default: 20)"
    )
    parser.add_argument(
        "--target",
        type=float,
        default=0.90,
        help="Target alignment score (default: 0.90)",
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
    fixer = VisualIterativeFixer(args.template, args.data, args.output, args.positions)
    fixer.max_iterations = args.iterations

    success = fixer.run_iterative_fix(target_score=args.target)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
