#!/usr/bin/env python3
"""
Tesseract Configuration Helper

Manages Tesseract language data file paths and configuration.
If custom language files are needed, they should be placed in:
    $DATA_DIR/tesseract/

This module configures pytesseract to use custom language files if available,
otherwise falls back to system-installed Tesseract language data.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# Try to import get_data_dir, fall back to None if not available
try:
    from config import get_data_dir

    HAS_CONFIG = True
except ImportError:
    try:
        from scripts.config import get_data_dir

        HAS_CONFIG = True
    except ImportError:
        HAS_CONFIG = False


def get_tesseract_data_dir() -> Path:
    """
    Get Tesseract data directory path from DATA_DIR.

    Returns:
        Path to $DATA_DIR/tesseract/
    """
    if HAS_CONFIG:
        try:
            data_dir = get_data_dir()
            return data_dir / "tesseract"
        except RuntimeError:
            # DATA_DIR not set, return None
            return None
    return None


TESSERACT_DATA_DIR = get_tesseract_data_dir()


def configure_tesseract_data_path():
    """
    Configure TESSDATA_PREFIX environment variable if custom language files exist.

    Returns:
        Path to tesseract data directory if it exists and contains .traineddata files,
        None otherwise
    """
    tesseract_dir = get_tesseract_data_dir()
    if (
        tesseract_dir
        and tesseract_dir.exists()
        and any(tesseract_dir.glob("*.traineddata"))
    ):
        os.environ["TESSDATA_PREFIX"] = str(tesseract_dir)
        return tesseract_dir

    return None


def get_available_languages():
    """
    Get list of available Tesseract languages from custom data directory.

    Returns:
        List of language codes (e.g., ['eng', 'spa'])
    """
    tesseract_dir = get_tesseract_data_dir()
    if not tesseract_dir or not tesseract_dir.exists():
        return []

    languages = []
    for file in tesseract_dir.glob("*.traineddata"):
        lang = file.stem
        languages.append(lang)

    return sorted(languages)


def ensure_tesseract_data_dir():
    """Create tesseract data directory if it doesn't exist."""
    tesseract_dir = get_tesseract_data_dir()
    if tesseract_dir:
        tesseract_dir.mkdir(parents=True, exist_ok=True)
        return tesseract_dir
    raise RuntimeError("DATA_DIR not set. Cannot create tesseract data directory.")


if __name__ == "__main__":
    # Test configuration
    tesseract_dir = get_tesseract_data_dir()
    if tesseract_dir:
        print(f"Tesseract data dir: {tesseract_dir}")
    else:
        print("DATA_DIR not set. Cannot determine tesseract data directory.")
        sys.exit(1)

    data_dir = configure_tesseract_data_path()
    if data_dir:
        print(f"✓ Custom Tesseract data directory configured: {data_dir}")
        languages = get_available_languages()
        if languages:
            print(f"  Available languages: {', '.join(languages)}")
        else:
            print("  No language files found")
    else:
        print("ℹ Using system Tesseract language data")
        if tesseract_dir:
            print(
                f"  To use custom files, place .traineddata files in: {tesseract_dir}"
            )
