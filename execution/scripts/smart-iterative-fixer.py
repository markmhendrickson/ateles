#!/usr/bin/env python3
"""
Smart Iterative PDF Alignment Fixer

Uses visual alignment checker to accurately detect misalignments,
then iteratively adjusts positions until correct.
"""

import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None  # Will fail at runtime if not available

# Import from existing scripts
sys.path.insert(0, str(Path(__file__).parent))
from iterative_pdf_alignment_fixer import IterativeAlignmentFixer
from visual_alignment_checker import check_visual_alignment


class SmartIterativeFixer(IterativeAlignmentFixer):
    """Smart iterative fixer that uses visual alignment checking."""

    def check_alignment(self, pdf_path: Path) -> tuple[dict[str, bool], float]:
        """
        Use visual alignment checker for accurate position validation.
        """
        # Use the visual alignment checker which validates actual positions
        try:
            results, score = check_visual_alignment(
                str(pdf_path),
                "operations/execution-plans/aigues-de-barcelona-auto-positions-mapped.json",
                str(self.data_path),
            )
            return results, score
        except Exception as e:
            print(f"Visual alignment check failed: {e}")
            # Fallback to parent method
            return super().check_alignment(pdf_path)

    def adjust_positions(self, validation_results: dict[str, bool]) -> dict:
        """Adjust positions with smarter strategies based on misalignment patterns."""
        new_positions = self.current_positions.copy()

        reader = PdfReader(str(self.template_path))
        page = reader.pages[0]
        media_box = page.mediabox
        float(media_box.top) - float(media_box.bottom)

        # Identify misaligned fields
        misaligned = [k for k, v in validation_results.items() if not v]

        # Group misaligned fields by section for pattern detection
        applicant_fields = [f for f in misaligned if f.startswith("applicant_")]
        property_fields = [f for f in misaligned if f.startswith("property_")]

        # Smart adjustments based on patterns
        if applicant_fields:
            # If multiple applicant fields misaligned, likely Y coordinate issue
            # Try moving applicant section up/down
            for field in applicant_fields:
                if field in new_positions:
                    x, y, page_idx = new_positions[field]
                    # Try moving up (common issue - fields too low)
                    new_positions[field] = [x, y + 15, page_idx]

        if property_fields:
            # Similar for property fields
            for field in property_fields:
                if field in new_positions:
                    x, y, page_idx = new_positions[field]
                    new_positions[field] = [x, y + 10, page_idx]

        # Individual field adjustments
        for field_name in misaligned:
            if field_name in new_positions:
                x, y, page_idx = new_positions[field_name]

                # Try different adjustment strategies based on iteration
                if self.iteration % 4 == 0:
                    new_positions[field_name] = [x, y + 15, page_idx]  # Move up
                elif self.iteration % 4 == 1:
                    new_positions[field_name] = [x, y - 15, page_idx]  # Move down
                elif self.iteration % 4 == 2:
                    new_positions[field_name] = [x + 10, y, page_idx]  # Move right
                else:
                    new_positions[field_name] = [x - 10, y, page_idx]  # Move left

        return new_positions


def main():
    template = "reference/documents/aigues-de-barcelona/Cast-FPT-071-Nueva-o-modificación-instalaciones (1).pdf"
    data = "operations/execution-plans/aigues-de-barcelona-form-data.json"
    output = (
        "operations/execution-plans/aigues-de-barcelona-filled-form-smart-iterative.pdf"
    )

    fixer = SmartIterativeFixer(template, data, output)
    fixer.max_iterations = 15
    fixer.alignment_threshold = 0.95

    success = fixer.run_iterative_fix()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
