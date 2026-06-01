#!/usr/bin/env python3
"""
Extract text from image-based PDF using Google Cloud Vision API.

Usage:
    python extract_pdf_ocr_vision.py <pdf_path> [--output <txt_path>]

Requirements:
    pip install google-cloud-vision pypdf pillow

Setup:
    1. Enable Vision API in Google Cloud Console
    2. Create service account and download JSON key
    3. Set GOOGLE_APPLICATION_CREDENTIALS env var or use gcloud auth
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

try:
    import io

    from google.cloud import vision
    from PIL import Image
    from pypdf import PdfReader
except ImportError as e:
    print(
        "Error: Missing required library. Install with: pip install google-cloud-vision pypdf pillow"
    )
    print(f"Missing: {e}")
    sys.exit(1)


def pdf_to_images(pdf_path: Path):
    """Convert PDF pages to images."""
    try:
        from pdf2image import convert_from_path

        return convert_from_path(str(pdf_path))
    except ImportError:
        # Fallback: try using pypdf + PIL (lower quality but no external deps)
        print("Warning: pdf2image not available. Using basic PDF rendering.")
        print("For better results: pip install pdf2image poppler")

        reader = PdfReader(str(pdf_path))
        images = []
        for page_num, page in enumerate(reader.pages, 1):
            # This is a simplified approach - pdf2image is better
            print(f"Page {page_num}: Basic extraction may have limited quality")
            # For now, we'll need pdf2image for proper image conversion
            raise ImportError("pdf2image required for PDF to image conversion")
        return images


def extract_text_with_vision(image, client: vision.ImageAnnotatorClient) -> str:
    """Extract text from image using Vision API."""
    # Convert PIL Image to bytes
    img_byte_arr = io.BytesIO()
    image.save(img_byte_arr, format="PNG")
    img_byte_arr.seek(0)

    # Create Vision API image
    vision_image = vision.Image(content=img_byte_arr.read())

    # Perform text detection
    response = client.text_detection(image=vision_image)
    texts = response.text_annotations

    if texts:
        # First annotation contains all detected text
        return texts[0].description
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="Extract text from image-based PDF using Google Cloud Vision API"
    )
    parser.add_argument("input", type=Path, help="Path to input PDF file")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Path to output text file (default: same as PDF with .txt extension)",
    )
    parser.add_argument(
        "--credentials",
        help="Path to Google Cloud service account JSON key (or set GOOGLE_APPLICATION_CREDENTIALS)",
    )

    args = parser.parse_args()

    input_path = args.input.expanduser().resolve()
    if not input_path.exists():
        print(f"Error: PDF not found: {input_path}")
        sys.exit(1)

    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else input_path.with_suffix(".txt")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Initialize Vision API client
    # Try multiple authentication methods
    client = None

    # Method 1: Explicit credentials file
    if args.credentials:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(
            Path(args.credentials).expanduser().resolve()
        )
        try:
            client = vision.ImageAnnotatorClient()
        except Exception as e:
            print(f"Error with credentials file: {e}")

    # Method 2: Application default credentials (gcloud auth)
    if not client:
        try:
            client = vision.ImageAnnotatorClient()
        except Exception as e1:
            # Method 3: Try using OAuth credentials from Gmail setup
            oauth_path = Path.home() / ".gmail-mcp" / "gcp-oauth.keys.json"
            if oauth_path.exists() and not args.credentials:
                print("Attempting to use OAuth credentials from Gmail setup...")
                try:
                    # For Vision API, we still need a service account
                    # OAuth credentials won't work directly
                    print(
                        "Note: OAuth credentials require service account for Vision API"
                    )
                    print("Creating service account is recommended")
                    raise e1
                except Exception:
                    pass

            print(f"Error initializing Vision API client: {e1}")
            print("\nSetup options:")
            print("1. Use application default credentials (recommended):")
            print("   gcloud auth application-default login")
            print("   (Uses same project as Gmail: personal-412209)")
            print("\n2. Create service account in same project:")
            print(
                "   - Go to: https://console.cloud.google.com/iam-admin/serviceaccounts?project=personal-412209"
            )
            print("   - Create service account, download JSON key")
            print("   - Use: --credentials <path-to-key.json>")
            print("\n3. Install gcloud CLI for easiest setup:")
            print("   https://cloud.google.com/sdk/docs/install")
            sys.exit(1)

    # Convert PDF to images
    print(f"Converting PDF to images: {input_path}")
    try:
        images = pdf_to_images(input_path)
    except ImportError as e:
        print(f"\nError: {e}")
        print("\nInstall pdf2image for PDF conversion:")
        print("  macOS: brew install poppler")
        print("  pip install pdf2image")
        sys.exit(1)

    # Extract text from each page
    all_text = []
    for i, image in enumerate(images, 1):
        print(f"Extracting text from page {i}/{len(images)}...")
        text = extract_text_with_vision(image, client)
        if text:
            all_text.append(f"=== PAGE {i} ===\n{text}\n")
        else:
            all_text.append(f"=== PAGE {i} ===\n(No text detected)\n")

    # Write output
    output_text = "\n\n".join(all_text)
    output_path.write_text(output_text, encoding="utf-8")
    print(f"\nText extracted to: {output_path}")
    print(f"Total characters: {len(output_text)}")


if __name__ == "__main__":
    main()
