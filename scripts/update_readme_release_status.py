#!/usr/bin/env python3
"""
Update README.md Releases section when release status.md files change.

Reads release status from docs/releases/*/status.md (or configured path)
and updates the corresponding entries in README.md Releases section.
"""

import re
import sys
from pathlib import Path


def find_release_status_files():
    """Find all release status.md files"""
    releases_dir = Path("docs/releases")
    if not releases_dir.exists():
        return []

    status_files = list(releases_dir.glob("*/status.md"))
    return status_files


def parse_release_status(status_file):
    """Parse release version and status from status.md file"""
    content = status_file.read_text()

    # Extract release version from path (e.g. docs/releases/v1.0.0/status.md -> v1.0.0)
    version = status_file.parent.name

    # Extract status from the file (look for "Status:" or "Current status:" line)
    status = None
    for line in content.split("\n"):
        if re.match(r"^Status:\s*`?([a-z_]+)`?", line, re.IGNORECASE):
            match = re.match(r"^Status:\s*`?([a-z_]+)`?", line, re.IGNORECASE)
            status = match.group(1)
            break
        elif "status" in line.lower() and any(
            s in line
            for s in [
                "planning",
                "in_progress",
                "ready_for_deployment",
                "deployed",
                "completed",
            ]
        ):
            # Try to extract status from any line mentioning it
            for candidate in [
                "planning",
                "in_progress",
                "in_testing",
                "ready_for_deployment",
                "deployed",
                "completed",
            ]:
                if candidate in line:
                    status = candidate
                    break

    return version, status


def update_readme_releases(releases_data):
    """Update README.md Releases section with current status"""
    readme_path = Path("README.md")
    if not readme_path.exists():
        print("README.md not found", file=sys.stderr)
        return False

    content = readme_path.read_text()

    # Find Releases section (look for ## Releases or ### Releases header)
    releases_section_match = re.search(r"^##+ Releases.*$", content, re.MULTILINE)
    if not releases_section_match:
        # No Releases section in README
        print(
            "No Releases section found in README.md (this is okay if repo doesn't have releases)",
            file=sys.stderr,
        )
        return True

    # Update each release entry
    modified = False
    for version, status in releases_data.items():
        if status is None:
            continue

        # Find release entry line (e.g., "- **v1.0.0**: Description (`status`)")
        pattern = rf"(- \*\*{re.escape(version)}\*\*:.*)\(`[a-z_]+`\)"
        replacement = rf"\1(`{status}`)"

        new_content, count = re.subn(pattern, replacement, content)
        if count > 0:
            content = new_content
            modified = True
            print(f"✓ Updated {version} status to {status}", file=sys.stderr)

    if modified:
        readme_path.write_text(content)
        print("✓ README.md updated", file=sys.stderr)
        return True
    else:
        print(
            "No release entries updated (releases may not be in README yet)",
            file=sys.stderr,
        )
        return True


def main():
    # Find all release status files
    status_files = find_release_status_files()

    if not status_files:
        # No releases yet, that's okay
        print(
            "No release status files found (docs/releases/*/status.md)", file=sys.stderr
        )
        sys.exit(0)

    # Parse each status file
    releases_data = {}
    for status_file in status_files:
        version, status = parse_release_status(status_file)
        releases_data[version] = status

    # Update README
    if update_readme_releases(releases_data):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
