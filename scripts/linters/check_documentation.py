#!/usr/bin/env python3
"""
Documentation structure checker.

Checks markdown files for:
- Required sections (from foundation-config.yaml)
- RFC 2119 keywords (MUST, MUST NOT, etc.)
- Link validity (basic check)
"""

import re
import sys
from pathlib import Path

# Required sections from foundation-config.yaml
REQUIRED_SECTIONS = {
    "docs/": ["Purpose", "Scope"],
    "strategy/": ["Purpose"],  # Strategy docs typically need Purpose
}

# Paths intentionally exempt from active-doc structure requirements: archived /
# superseded material and imported operator notes that are deliberately not part
# of the Ateles reference-architecture doc set (see docs/documentation_plan.md).
EXCLUDED_PREFIXES = (
    "docs/archive/",
    "docs/runbooks/home-automation/",
)

# RFC 2119 keywords
RFC_2119_KEYWORDS = [
    "MUST",
    "MUST NOT",
    "SHALL",
    "SHALL NOT",
    "SHOULD",
    "SHOULD NOT",
    "MAY",
    "REQUIRED",
    "OPTIONAL",
]


def find_headers(content: str) -> set[str]:
    """Extract all markdown headers from content."""
    headers = set()
    # Match markdown headers (# Header, ## Header, etc.)
    pattern = r"^#+\s+(.+)$"
    for match in re.finditer(pattern, content, re.MULTILINE):
        header_text = match.group(1).strip()
        headers.add(header_text)
        # Also check for partial matches (e.g., "Purpose" in "Purpose and Scope")
        for word in header_text.split():
            headers.add(word)
    return headers


def check_required_sections(filepath: str, content: str) -> list[str]:
    """Check if required sections are present."""
    violations = []
    path = Path(filepath)

    # Archived / imported material is intentionally exempt from active-doc structure.
    if any(str(path).startswith(p) for p in EXCLUDED_PREFIXES):
        return []

    # Determine which sections are required for this file
    required = []
    for prefix, sections in REQUIRED_SECTIONS.items():
        if str(path).startswith(prefix):
            required.extend(sections)
            break

    if not required:
        return violations  # No requirements for this file

    headers = find_headers(content)

    for section in required:
        # Check if section exists (exact match or as part of header)
        found = False
        for header in headers:
            if section.lower() in header.lower() or header.lower() in section.lower():
                found = True
                break

        if not found:
            violations.append(
                f"Missing required section: '{section}' "
                f"(required for files in {prefix})"
            )

    return violations


def check_rfc_2119(content: str) -> bool:
    """Check if RFC 2119 keywords are present (for policy documents)."""
    content_upper = content.upper()
    for keyword in RFC_2119_KEYWORDS:
        if keyword in content_upper:
            return True
    return False


def check_links(content: str) -> list[str]:
    """Basic link validation (check format, not existence)."""
    violations = []

    # Find all markdown links [text](url)
    link_pattern = r"\[([^\]]+)\]\(([^\)]+)\)"

    for match in re.finditer(link_pattern, content):
        link_text = match.group(1)
        link_url = match.group(2)

        # Skip anchor links
        if link_url.startswith("#"):
            continue

        # Check for broken relative paths (basic check)
        if not link_url.startswith(("http://", "https://", "mailto:", "/")):
            # Relative path - check if it looks broken
            if ".." in link_url and link_url.count("../") > 3:
                violations.append(
                    f"Potentially broken relative link: [{link_text}]({link_url})"
                )

    return violations


def check_file(filepath: str) -> list[str]:
    """Check a single markdown file for documentation violations."""
    violations = []

    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return [f"Error reading file: {e}"]

    # Check required sections
    violations.extend(check_required_sections(filepath, content))

    # Check links (warnings only)
    link_issues = check_links(content)
    if link_issues:
        # Link issues are warnings, not errors
        for issue in link_issues:
            print(f"WARNING: {filepath}: {issue}", file=sys.stderr)

    return violations


def main():
    """Main entry point for the linter."""
    if len(sys.argv) < 2:
        # If no files provided, check all staged markdown files
        import subprocess

        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
        )
        files = [
            f
            for f in result.stdout.strip().split("\n")
            if f.endswith(".md")
            and f
            and (f.startswith("docs/") or f.startswith("strategy/"))
        ]
    else:
        files = sys.argv[1:]

    if not files:
        sys.exit(0)

    all_violations = []
    for filepath in files:
        violations = check_file(filepath)
        all_violations.extend(violations)

    if all_violations:
        print("ERROR: Documentation structure violations:\n", file=sys.stderr)
        for violation in all_violations:
            print(f"  {violation}", file=sys.stderr)
        print(
            "\nSee foundation-config.yaml for documentation requirements.",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
