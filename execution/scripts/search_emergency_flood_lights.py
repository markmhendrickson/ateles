#!/usr/bin/env python3
"""
Search PDFs for emergency flood light information on back façade.
Extracts text from certification PDFs and searches for emergency light models.
"""

import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    print("Error: Missing pypdf. Install with: pip install pypdf")
    sys.exit(1)


def search_pdf_for_emergency_lights(pdf_path: Path, search_terms=None):
    """Extract text from PDF and search for emergency light information."""
    if search_terms is None:
        search_terms = [
            "emergency",
            "emergencia",
            "luz emergencia",
            "flood",
            "fachada",
            "trasera",
            "back",
            "exterior",
            "emergency light",
            "luminaria emergencia",
        ]

    if not pdf_path.exists():
        return None

    print(f"\n{'=' * 70}")
    print(f"Searching: {pdf_path.name}")
    print(f"{'=' * 70}")

    try:
        reader = PdfReader(str(pdf_path))
        all_matches = []

        for page_num, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            text_lower = text.lower()

            # Search for emergency light related terms
            matches = []
            for term in search_terms:
                if term.lower() in text_lower:
                    # Find context around the match
                    idx = text_lower.find(term.lower())
                    start = max(0, idx - 100)
                    end = min(len(text), idx + len(term) + 200)
                    context = text[start:end].replace("\n", " ")
                    matches.append(
                        f"  Found '{term}' on page {page_num}: ...{context}..."
                    )

            if matches:
                all_matches.extend(matches)
                print(f"\nPage {page_num} matches:")
                for match in matches:
                    print(match)

        if not all_matches:
            print("  No emergency light references found")

        return all_matches

    except Exception as e:
        print(f"Error reading PDF: {e}")
        return None


def main():
    """Search certification PDFs for emergency flood light information."""
    base_dir = Path(__file__).parent.parent / "operations" / "admin"

    # PDFs to search (in order of likelihood)
    pdfs_to_search = [
        base_dir / "legrand-netatmo-docs" / "cert-20-partidas.pdf",
        base_dir / "legrand-netatmo-docs" / "cert-19-partidas.pdf",
        base_dir / "legrand-netatmo-docs" / "cert-18-partidas.pdf",
        base_dir / "legrand-netatmo-docs" / "cert-17-partidas.pdf",
        base_dir / "legrand-netatmo-docs" / "3330 _ Certificación 20 _ Partidas.pdf",
        base_dir / "legrand-netatmo-docs" / "3330 _ Certificación 19 _ Partidas.pdf",
        base_dir / "legrand-netatmo-docs" / "3330 _ Certificación 18 _ Partidas.pdf",
        base_dir / "legrand-netatmo-docs" / "3330 _ Certificación 17 _ Partidas.pdf",
    ]

    print("Searching for Emergency Flood Lights on Back Façade")
    print("=" * 70)
    print("\nLooking for:")
    print("  - Emergency flood lights")
    print("  - Back façade / fachada trasera")
    print("  - Exterior emergency lights")
    print("  - Model numbers and specifications")

    found_any = False
    for pdf_path in pdfs_to_search:
        if pdf_path.exists():
            found_any = True
            matches = search_pdf_for_emergency_lights(pdf_path)
            if matches:
                found_any = True

    if not found_any:
        print("\n" + "=" * 70)
        print("No PDFs found. Please download the certification PDFs first.")
        print("\nTo download:")
        print("1. Use Gmail web interface to download PDFs from:")
        print("   - Message ID: 17d76a17d5e22e31 (Cert 20)")
        print("   - Message ID: 17ce16638dc6105a (Cert 19)")
        print("   - Message ID: 17be4e5daf78fd76 (Cert 18)")
        print("   - Message ID: 17b1ae1db1ef77bf (Cert 17)")
        print("2. Save 'Partidas' PDFs to: operations/admin/legrand-netatmo-docs/")


if __name__ == "__main__":
    main()
