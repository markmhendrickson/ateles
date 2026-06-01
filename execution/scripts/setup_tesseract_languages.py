#!/usr/bin/env python3
"""
Setup Tesseract Language Data Files

Downloads Tesseract language data files to $DATA_DIR/tesseract/
for use with PDF OCR scripts.

Usage:
    python setup_tesseract_languages.py [--languages eng spa] [--force]
"""

import argparse
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlretrieve

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "execution" / "scripts"))

# Try to import get_data_dir
try:
    from config import get_data_dir
except ImportError:
    try:
        from scripts.config import get_data_dir
    except ImportError:
        print("Error: Could not import get_data_dir from config")
        print("  Make sure DATA_DIR is set in .env file")
        sys.exit(1)

# GitHub releases URL for Tesseract language data
TESSERACT_LANG_BASE_URL = "https://github.com/tesseract-ocr/tessdata/raw/main/"

# Common languages
COMMON_LANGUAGES = {
    "eng": "English",
    "spa": "Spanish",
    "fra": "French",
    "deu": "German",
    "ita": "Italian",
    "por": "Portuguese",
    "rus": "Russian",
    "chi_sim": "Chinese (Simplified)",
    "chi_tra": "Chinese (Traditional)",
    "jpn": "Japanese",
    "kor": "Korean",
}


def check_tesseract_installed():
    """Check if Tesseract is installed."""
    try:
        result = subprocess.run(
            ["tesseract", "--version"], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def download_language_file(
    lang_code: str, tesseract_dir: Path, force: bool = False
) -> bool:
    """
    Download a Tesseract language data file.

    Args:
        lang_code: Language code (e.g., 'eng', 'spa')
        tesseract_dir: Directory to save the file
        force: If True, overwrite existing file

    Returns:
        True if successful, False otherwise
    """
    output_file = tesseract_dir / f"{lang_code}.traineddata"

    if output_file.exists() and not force:
        print(f"  ✓ {lang_code}.traineddata already exists (use --force to overwrite)")
        return True

    url = f"{TESSERACT_LANG_BASE_URL}{lang_code}.traineddata"

    try:
        print(f"  Downloading {lang_code}.traineddata...")
        urlretrieve(url, output_file)

        if output_file.exists() and output_file.stat().st_size > 0:
            size_mb = output_file.stat().st_size / (1024 * 1024)
            print(f"    ✓ Downloaded ({size_mb:.2f} MB)")
            return True
        else:
            print("    ✗ Download failed: file is empty")
            if output_file.exists():
                output_file.unlink()
            return False

    except URLError as e:
        print(f"    ✗ Download failed: {e}")
        if output_file.exists():
            output_file.unlink()
        return False
    except Exception as e:
        print(f"    ✗ Error: {e}")
        if output_file.exists():
            output_file.unlink()
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Download Tesseract language data files"
    )
    parser.add_argument(
        "--languages",
        nargs="+",
        default=["eng", "spa"],
        help="Language codes to download (default: eng spa)",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    parser.add_argument("--list", action="store_true", help="List available languages")

    args = parser.parse_args()

    if args.list:
        print("Available languages:")
        for code, name in sorted(COMMON_LANGUAGES.items()):
            print(f"  {code:12} - {name}")
        return

    # Get data directory
    try:
        data_dir = get_data_dir()
        tesseract_dir = data_dir / "tesseract"
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Check if Tesseract is installed
    if not check_tesseract_installed():
        print("Warning: Tesseract not found in PATH")
        print("  Install with: brew install tesseract  # macOS")
        print("  Or: apt-get install tesseract-ocr  # Linux")
        response = input("\nContinue anyway? (y/N): ")
        if response.lower() != "y":
            sys.exit(1)

    # Create data directory
    tesseract_dir.mkdir(parents=True, exist_ok=True)
    print(f"Tesseract data directory: {tesseract_dir}")
    print()

    # Download language files
    success_count = 0
    for lang_code in args.languages:
        lang_name = COMMON_LANGUAGES.get(lang_code, lang_code)
        print(f"Processing {lang_code} ({lang_name})...")

        if download_language_file(lang_code, tesseract_dir, force=args.force):
            success_count += 1
        print()

    print(f"✓ Downloaded {success_count}/{len(args.languages)} language file(s)")
    print(f"\nLanguage files are in: {tesseract_dir}")
    print("Scripts will automatically use these files if available.")


if __name__ == "__main__":
    main()
