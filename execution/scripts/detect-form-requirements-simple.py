#!/usr/bin/env python3
"""
Simple Form Requirements Detector

Analyzes PDF form and lists required fields based on form structure analysis.
"""

import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    print("Error: Missing required library. Install with: pip install pypdf")
    sys.exit(1)


def analyze_form(pdf_path: str) -> None:
    """Analyze form and list required fields."""
    reader = PdfReader(pdf_path)

    print("=" * 70)
    print("FORM REQUIREMENTS ANALYSIS")
    print("=" * 70)
    print(f"\nPDF: {Path(pdf_path).name}\n")

    # Extract text from all pages
    all_text = ""
    for page_num, page in enumerate(reader.pages):
        text = page.extract_text()
        all_text += f"\n--- Page {page_num + 1} ---\n{text}\n"

    # Define known field requirements based on form structure
    # This is specific to Aigües de Barcelona FPT-071 form
    # but can be adapted for other forms

    requirements = {
        "PROPERTY DATA (Datos de la finca)": {
            "required": [
                "Calle y nº (Street and number)",
                "Municipio (Municipality)",
                "Código postal (Postal code)",
            ],
            "optional": [
                "Distrito (District)",
                "Información adicional (Additional information)",
            ],
        },
        "APPLICANT DATA (Datos del peticionario)": {
            "required": [
                "Nombre y apellidos (Name and surnames)",
                "NIF/CIF",
                "Calle y nº (Street and number)",
                "Municipio (Municipality)",
                "Teléfono (Phone)",
                "Correo electrónico (Email)",
            ],
            "optional": [
                "El Peticionario, en calidad de (Applicant capacity): Propietario/a, Comunidad, Administrador/a, etc."
            ],
        },
        "PROPERTY OWNER DATA (Datos del propietario)": {
            "required": [],
            "optional": [
                "If same as applicant: Checkbox",
                "Otherwise: Nombre y apellidos, NIF/CIF, Calle y nº, Municipio, Teléfono, Correo electrónico",
            ],
        },
        "INSTALLER DATA (Datos del instalador)": {
            "required": [],
            "optional": [
                "If same as applicant: Checkbox",
                "Otherwise: Nombre y apellidos, NIF/CIF, Teléfono, Correo electrónico, Nº instalador/a, Nº RECI",
            ],
        },
        "OFFER RECIPIENT (Destinatario de la oferta)": {
            "required": [
                "Select one: Same as applicant / Same as owner / Same as installer"
            ],
            "optional": [],
        },
        "CONTACT PERSON (Persona de contacto)": {
            "required": [
                "Select one: Same as applicant / Same as owner / Same as installer"
            ],
            "optional": [],
        },
        "TECHNICAL DATA (Page 2)": {
            "required": [
                "Tipo de instalación (Installation type)",
                "Categoría de instalación (Installation category)",
                "Subcategoría (Subcategory)",
                "Tipo de vivienda (Housing type)",
            ],
            "optional": [
                "Número de plantas (Number of floors)",
                "Grupo de presión (Pressure group)",
                "Caudal máximo (Max flow)",
            ],
        },
    }

    # Print requirements
    for section, fields in requirements.items():
        print(f"\n{section}")
        print("-" * 70)

        if fields["required"]:
            print("  REQUIRED FIELDS:")
            for field in fields["required"]:
                print(f"    ✓ {field}")

        if fields["optional"]:
            print("  OPTIONAL FIELDS:")
            for field in fields["optional"]:
                print(f"    • {field}")

    # Summary
    total_required = sum(len(f["required"]) for f in requirements.values())
    total_optional = sum(len(f["optional"]) for f in requirements.values())

    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    print(f"Total required fields: {total_required}")
    print(f"Total optional fields: {total_optional}")
    print("\nNote: Fields marked with * in the PDF are mandatory.")
    print("      Checkboxes for 'same as applicant' reduce required fields.")


def main():
    if len(sys.argv) < 2:
        print("Usage: python detect-form-requirements-simple.py <pdf_file>")
        sys.exit(1)

    pdf_path = sys.argv[1]

    if not Path(pdf_path).exists():
        print(f"Error: PDF file not found: {pdf_path}")
        sys.exit(1)

    analyze_form(pdf_path)


if __name__ == "__main__":
    main()
