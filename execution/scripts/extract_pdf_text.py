import argparse
import sys
from pathlib import Path

from pypdf import PdfReader

# Minimum characters from pypdf below which we try OCR (for image-only/scanned PDFs)
_OCR_FALLBACK_THRESHOLD = 50


def _extract_pdf_text_ocr(input_path: Path) -> str:
    """Extract text from image-based PDF using pdf2image + pytesseract."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError as e:
        raise SystemExit(
            "OCR fallback requires pdf2image and pytesseract. "
            "Install with: pip install pdf2image pytesseract && brew install poppler tesseract"
        ) from e
    try:
        _script_dir = Path(__file__).resolve().parent
        if str(_script_dir) not in sys.path:
            sys.path.insert(0, str(_script_dir))
        from tesseract_config import configure_tesseract_data_path

        configure_tesseract_data_path()
    except Exception:
        pass
    images = convert_from_path(str(input_path), dpi=200)
    chunks = []
    for img in images:
        page_text = pytesseract.image_to_string(img, lang="eng+spa").strip()
        if page_text:
            chunks.append(page_text)
    return "\n\n".join(chunks) if chunks else ""


def extract_pdf_text(input_path: Path, use_ocr_fallback: bool = True) -> str:
    input_path = Path(input_path).expanduser().resolve()
    reader = PdfReader(str(input_path))
    chunks = []
    for page in reader.pages:
        text = page.extract_text() or ""
        chunks.append(text.strip())
    text = "\n\n".join(chunk for chunk in chunks if chunk)
    if use_ocr_fallback and len(text.strip()) < _OCR_FALLBACK_THRESHOLD:
        if sys.stdout.isatty():
            print(
                "Little or no embedded text; trying OCR (pdf2image + pytesseract)...",
                file=sys.stderr,
            )
        try:
            text = _extract_pdf_text_ocr(input_path)
        except SystemExit:
            raise
        except Exception as e:
            print(f"OCR fallback failed: {e}", file=sys.stderr)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract text from a PDF into a UTF-8 .txt file."
    )
    parser.add_argument(
        "--input",
        "-i",
        required=True,
        help="Path to input PDF (e.g. data/attachments/porsche/cayenne-invoice.pdf)",
    )
    parser.add_argument(
        "--output",
        "-o",
        help=(
            "Path to output text file. If omitted, writes alongside PDF with .txt "
            "extension."
        ),
    )
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"Input PDF not found: {input_path}")

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else input_path.with_suffix(".txt")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    text = extract_pdf_text(input_path)
    output_path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
