#!/usr/bin/env python3
"""
Detect Form Requirements

Analyzes a PDF form and lists all fields that need to be filled,
including which are mandatory vs optional.
"""

import argparse
import json
import sys
from pathlib import Path

try:
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
    from pypdf import PdfReader
except ImportError:
    print("Error: Missing required library. Install with: pip install pypdf")
    sys.exit(1)


def extract_text_with_positions(pdf_path: str, page_num: int = 0) -> list[dict]:
    """Extract all text from PDF with positions."""
    images = convert_from_path(
        pdf_path, first_page=page_num + 1, last_page=page_num + 1, dpi=300
    )
    if not images:
        return []

    img = np.array(images[0])

    try:
        ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        text_items = []

        for i, text in enumerate(ocr_data["text"]):
            text = text.strip()
            if text and len(text) > 1:
                text_items.append(
                    {
                        "text": text,
                        "x": ocr_data["left"][i],
                        "y": ocr_data["top"][i],
                        "width": ocr_data["width"][i],
                        "height": ocr_data["height"][i],
                        "confidence": ocr_data["conf"][i],
                    }
                )

        return text_items
    except Exception as e:
        print(f"OCR error: {e}")
        return []


def detect_form_fields(pdf_path: str, page_num: int = 0) -> list[dict]:
    """
    Detect form fields and their requirements.
    Returns list of field information.
    """
    print(f"Analyzing form: {Path(pdf_path).name} (page {page_num + 1})...")

    # Extract text
    text_items = extract_text_with_positions(pdf_path, page_num)

    # Also extract text directly from PDF
    reader = PdfReader(pdf_path)
    page = reader.pages[page_num]
    pdf_text = page.extract_text()

    fields = []

    # Common field patterns
    field_patterns = [
        # Spanish form patterns
        (r"(?i)(nombre|name|apellidos|surnames)", "text", True),
        (r"(?i)(nif|cif|dni|id)", "text", True),
        (r"(?i)(calle|street|dirección|address)", "text", True),
        (r"(?i)(municipio|municipality|ciudad|city)", "text", True),
        (r"(?i)(código postal|postal code|código|code)", "text", True),
        (r"(?i)(distrito|district)", "text", False),
        (r"(?i)(teléfono|phone|tel)", "text", True),
        (r"(?i)(correo|email|e-mail|mail)", "text", True),
        (r"(?i)(información adicional|additional info|notes)", "text", False),
        (r"(?i)(propietario|owner)", "checkbox", False),
        (r"(?i)(instalador|installer)", "checkbox", False),
        (r"(?i)(número|number|nº|no\.)", "text", False),
        (r"(?i)(plantas|floors|pisos)", "text", False),
        (r"(?i)(tipo|type)", "text", False),
        (r"(?i)(categoría|category)", "text", False),
        (r"(?i)(vivienda|housing|residencia)", "text", False),
    ]

    # Look for mandatory indicators
    mandatory_indicators = ["*", "obligatorio", "required", "mandatory"]

    # Detect fields by looking for labels
    detected_labels = {}

    for item in text_items:
        text_lower = item["text"].lower()

        # Check for mandatory indicator
        is_mandatory = (
            any(indicator in text_lower for indicator in mandatory_indicators)
            or "*" in item["text"]
        )

        # Match against patterns
        for pattern, field_type, default_mandatory in field_patterns:
            import re

            if re.search(pattern, text_lower):
                # Extract field name
                field_name = item["text"].strip().rstrip(":*")

                # Check if this is a label (usually followed by field)
                if field_name not in detected_labels:
                    detected_labels[field_name] = {
                        "label": field_name,
                        "type": field_type,
                        "mandatory": is_mandatory or default_mandatory,
                        "y_position": item["y"],
                    }

    # Also parse PDF text directly for better context
    lines = pdf_text.split("\n")
    current_section = None

    for line in lines:
        line_lower = line.lower().strip()

        # Detect sections
        if "datos de la finca" in line_lower or "property data" in line_lower:
            current_section = "property"
        elif "datos del peticionario" in line_lower or "applicant data" in line_lower:
            current_section = "applicant"
        elif "datos del propietario" in line_lower or "owner data" in line_lower:
            current_section = "owner"
        elif "datos del instalador" in line_lower or "installer data" in line_lower:
            current_section = "installer"

        # Look for field labels in text
        for pattern, field_type, default_mandatory in field_patterns:
            import re

            if re.search(pattern, line_lower):
                is_mandatory = "*" in line or "obligatorio" in line_lower
                field_label = line.strip().rstrip(":*").strip()

                if field_label and len(field_label) > 2:
                    key = (
                        field_label.lower()
                        .replace(" ", "_")
                        .replace(":", "")
                        .replace("*", "")
                    )
                    if key not in detected_labels:
                        detected_labels[key] = {
                            "label": field_label,
                            "type": field_type,
                            "mandatory": is_mandatory or default_mandatory,
                            "section": current_section,
                            "y_position": 0,
                        }

    # Convert to list and sort by position
    fields = list(detected_labels.values())
    fields.sort(key=lambda x: x.get("y_position", 0), reverse=True)

    return fields


def format_field_list(fields: list[dict]) -> str:
    """Format fields as a readable list."""
    if not fields:
        return "No fields detected."

    output = []
    output.append(f"\n{'=' * 70}")
    output.append("FORM FIELDS DETECTED")
    output.append(f"{'=' * 70}\n")

    # Group by section
    sections = {}

    for field in fields:
        section = field.get("section", "other")
        if section not in sections:
            sections[section] = []
        sections[section].append(field)

    # Print sections
    section_names = {
        "property": "Property Data (Datos de la finca)",
        "applicant": "Applicant Data (Datos del peticionario)",
        "owner": "Owner Data (Datos del propietario)",
        "installer": "Installer Data (Datos del instalador)",
        "other": "Other Fields",
    }

    for section_key, section_fields in sections.items():
        section_name = section_names.get(
            section_key, section_key.title() if section_key else "Other Fields"
        )
        output.append(f"\n{section_name}:")
        output.append("-" * 70)

        for field in section_fields:
            mandatory = "✓ REQUIRED" if field.get("mandatory", False) else "  Optional"
            field_type = field.get("type", "text")
            label = field.get("label", "Unknown")

            output.append(f"  {mandatory} | {field_type:10s} | {label}")

    # Summary
    mandatory_count = sum(1 for f in fields if f.get("mandatory", False))
    optional_count = len(fields) - mandatory_count

    output.append(f"\n{'=' * 70}")
    output.append("SUMMARY")
    output.append(f"{'=' * 70}")
    output.append(f"Total fields: {len(fields)}")
    output.append(f"Required: {mandatory_count}")
    output.append(f"Optional: {optional_count}")
    output.append("")

    return "\n".join(output)


def generate_data_template(fields: list[dict]) -> dict:
    """Generate a data template JSON structure."""
    template = {}

    for field in fields:
        key = (
            field.get("label", "")
            .lower()
            .replace(" ", "_")
            .replace(":", "")
            .replace("*", "")
            .replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
        )
        field_type = field.get("type", "text")

        if field_type == "checkbox":
            template[key] = False
        elif field_type == "text":
            template[key] = ""
        else:
            template[key] = ""

    return template


def main():
    parser = argparse.ArgumentParser(
        description="Detect form fields and requirements from PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--template", required=True, help="PDF template file")
    parser.add_argument("--page", type=int, default=0, help="Page number (0-indexed)")
    parser.add_argument("--output", help="Output JSON file for data template")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    if not Path(args.template).exists():
        print(f"Error: Template file not found: {args.template}")
        sys.exit(1)

    # Detect fields
    fields = detect_form_fields(args.template, args.page)

    if args.json:
        # Output as JSON
        output = {
            "fields": fields,
            "summary": {
                "total": len(fields),
                "required": sum(1 for f in fields if f.get("mandatory", False)),
                "optional": sum(1 for f in fields if not f.get("mandatory", False)),
            },
        }
        print(json.dumps(output, indent=2))
    else:
        # Output as formatted text
        print(format_field_list(fields))

        # Generate data template if requested
        if args.output:
            template = generate_data_template(fields)
            with open(args.output, "w") as f:
                json.dump(template, f, indent=2)
            print(f"\n✓ Data template saved to: {args.output}")
            print("  Fill in the values and use with generic-pdf-form-filler.py")


if __name__ == "__main__":
    main()
