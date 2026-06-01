#!/usr/bin/env python3
"""
Diagnose filled PDF to see what text appears where.
"""

import sys

try:
    from pypdf import PdfReader
except ImportError:
    print("Error: Missing pypdf. Install with: pip install pypdf")
    sys.exit(1)


def diagnose_pdf(pdf_path: str):
    """Extract and display all text from PDF with page numbers."""
    reader = PdfReader(pdf_path)

    print(f"\n{'=' * 70}")
    print(f"Diagnosing: {pdf_path}")
    print(f"{'=' * 70}\n")

    for page_num, page in enumerate(reader.pages, 1):
        print(f"\n--- PAGE {page_num} ---")
        try:
            text = page.extract_text()
            if text:
                # Show first 500 chars of each page
                preview = text[:500].replace("\n", " ")
                print(preview)
                if len(text) > 500:
                    print(f"... ({len(text)} total characters)")
            else:
                print("(No text found)")
        except Exception as e:
            print(f"Error extracting text: {e}")

    print(f"\n{'=' * 70}\n")


if __name__ == "__main__":
    pdf_path = "operations/execution-plans/aigues-de-barcelona-filled-form-final.pdf"
    if len(sys.argv) > 1:
        pdf_path = sys.argv[1]

    diagnose_pdf(pdf_path)
