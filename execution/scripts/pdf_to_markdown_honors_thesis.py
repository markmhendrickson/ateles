#!/usr/bin/env python3
"""
Extract Bowdoin Honors thesis PDF to markdown for HTML rendering on the website.
Outputs structured markdown with ## chapter headings and ### subsection headings.
Usage:
  python3 execution/scripts/pdf_to_markdown_honors_thesis.py [pdf_path] [output_path]
Defaults: PDF from data-backups path, output to website src/content/honors-thesis-body.md
"""

import re
import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    print("Error: Missing pypdf. Install with: pip install pypdf")
    sys.exit(1)

# Known main chapter/section titles (sentence case in output)
CHAPTER_PATTERNS = [
    r"^Introduction\s*$",
    r"^Chapter\s+1\s*[–\-]\s*(.+)$",
    r"^Chapter\s+2\s*[–\-]\s*(.+)$",
    r"^Chapter\s+3\s*[–\-]\s*(.+)$",
    r"^Chapter\s+4\s*[–\-]\s*(.+)$",
    r"^Chapter\s+5\s*[–\-]\s*(.+)$",
    r"^Conclusion\s*$",
    r"^List of Abbreviations\s*$",
    r"^Bibliography\s*$",
    r"^CONTENTS\s*$",
]


# Subsection headings: lines that look like section titles (short, no period at end, title case)
# We'll treat lines that are short (< 80 chars), don't end with ., and are not a single number
def looks_like_subsection(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 80:
        return False
    if re.match(r"^\d+$", line):
        return False
    if line.endswith("."):
        return False
    # Roman numerals at start (e.g. "I. Introduction" already handled as chapter)
    if re.match(r"^[IVX]+\.\s+", line) and len(line) < 60:
        return True
    # Title-style phrase (multiple words, capitalized)
    words = line.split()
    if len(words) >= 2 and len(words) <= 12 and line[0].isupper():
        return True
    return False


def clean_page_text(text: str) -> str:
    """Remove page markers and footer page numbers."""
    # "-- N of 152 --"
    text = re.sub(r"\s*--\s*\d+\s+of\s+\d+\s+--\s*", "\n", text)
    # Standalone small numbers at end of line (footer page numbers)
    text = re.sub(r"\n\s*(\d+)\s*\n", "\n", text)
    # Roman numeral page (e.g. "iv", "v")
    text = re.sub(r"\n\s*(iv|v|vi)\s*\n", "\n", text, flags=re.IGNORECASE)
    return text


def extract_markdown(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    blocks: list[str] = []
    current_paragraph: list[str] = []
    in_contents = False
    contents_end_page = 5  # Skip title/contents pages for body

    for page_num, page in enumerate(reader.pages, 1):
        raw = page.extract_text() or ""
        raw = clean_page_text(raw)
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

        for i, line in enumerate(lines):
            # Skip title page and contents
            if page_num <= 4:
                if "CONTENTS" in line:
                    in_contents = True
                continue
            if in_contents and page_num <= contents_end_page:
                continue

            # Main chapter heading
            if re.match(r"^Introduction\s*$", line, re.IGNORECASE):
                if current_paragraph:
                    blocks.append(" ".join(current_paragraph))
                    current_paragraph = []
                blocks.append("\n## Introduction\n")
                continue
            for pat in CHAPTER_PATTERNS:
                if "CONTENTS" in pat:
                    continue
                m = re.match(pat, line, re.IGNORECASE)
                if m:
                    if current_paragraph:
                        blocks.append(" ".join(current_paragraph))
                        current_paragraph = []
                    title = (
                        m.group(1).strip() if m.lastindex and m.lastindex >= 1 else line
                    )
                    blocks.append(f"\n## {title}\n")
                    continue

            # Subsection (e.g. "Schopenhauer's Criticism of the Basis Given to Ethics by Kant")
            if looks_like_subsection(line) and not line.isdigit():
                # Avoid treating single short lines mid-paragraph as headings; require newline context
                if current_paragraph and len(current_paragraph) > 2:
                    blocks.append(" ".join(current_paragraph))
                    current_paragraph = []
                if current_paragraph and len(current_paragraph) <= 2:
                    blocks.append(" ".join(current_paragraph))
                    current_paragraph = []
                blocks.append(f"\n### {line}\n")
                continue

            # Footnote ref (e.g. "Stanford Encyclopedia...") - keep as paragraph
            current_paragraph.append(line)

        # End of page: flush paragraph
        if current_paragraph and page_num > contents_end_page:
            # Join with space; next page might continue same paragraph
            pass

    if current_paragraph:
        blocks.append(" ".join(current_paragraph))

    out = "\n".join(blocks)
    # Normalize multiple newlines
    out = re.sub(r"\n{4,}", "\n\n\n", out)
    # Paragraphs: ensure double newline between
    out = re.sub(r"\n(?=##)", "\n\n", out)
    out = re.sub(r"\n(?=###)", "\n\n", out)
    return out.strip()


def main():
    # execution/scripts -> execution dir is parent, ateles repo root is parent.parent
    script_dir = Path(__file__).resolve().parent
    execution_dir = script_dir.parent
    ateles_root = execution_dir.parent
    default_pdf = (
        ateles_root
        / "Documents"
        / "data-backups"
        / "data copy"
        / "imports"
        / "bowdoin"
        / "Government"
        / "Honors Project"
        / "Honors Project.pdf"
    )
    default_out = (
        execution_dir
        / "website"
        / "markmhendrickson"
        / "react-app"
        / "src"
        / "content"
        / "honors-thesis-body.md"
    )

    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_pdf
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else default_out

    if not pdf_path.exists():
        print(f"Error: PDF not found: {pdf_path}")
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    md = extract_markdown(pdf_path)
    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote {len(md)} chars to {out_path}")


if __name__ == "__main__":
    main()
