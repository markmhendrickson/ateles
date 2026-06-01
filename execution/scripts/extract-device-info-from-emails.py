#!/usr/bin/env python3
"""
Extract device information from Gmail attachments related to Legrand/Netatmo installation.
Downloads PDFs and extracts text content.
"""

import subprocess

# Note: This script would need Gmail API credentials to download attachments
# For now, we'll document the process and extract what we can from email content


def main():
    print("Device Information Extraction Script")
    print("=" * 50)
    print("\nTo extract device information from PDF attachments:")
    print("1. Download PDFs manually from Gmail")
    print("2. Use pdftotext to extract text")
    print("3. Search for device model numbers and specifications")
    print("\nKey documents to review:")
    print("- XEDEX Cert 20 Partidas PDF (Dec 2021)")
    print("- XEDEX Cert 19 Partidas PDF (Nov 2021)")
    print("- PC 28 Mecanismos Inteligentes PDF (Nov 2021)")
    print("- PC 28.1 Mecanismos Inteligentes PDF (Nov 2021)")
    print("- GRUPO KIAK PR-23-227 PDF (Nov 2023)")

    # Check if pdftotext is available
    result = subprocess.run(["which", "pdftotext"], capture_output=True)
    if result.returncode == 0:
        print(f"\npdftotext found at: {result.stdout.decode().strip()}")
    else:
        print("\npdftotext not found - install poppler-utils")


if __name__ == "__main__":
    main()
