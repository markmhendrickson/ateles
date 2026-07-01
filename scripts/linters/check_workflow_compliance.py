#!/usr/bin/env python3
"""
Workflow file compliance checker.

Checks that workflow files (reports, reviews) are in correct locations
as specified in docs/policies/agent-workflow-requirements.md.
"""

import re
import sys
from pathlib import Path

# Required locations for workflow files
REQUIRED_LOCATIONS = {
    "btc-liquidity-regime-report-*.md": "strategy/operations/finance/",
    "altcoin-liquidity-regime-report-*.md": "strategy/operations/finance/",
    "quarterly-portfolio-review-*.md": "strategy/operations/finance/",
}


def check_file_location(filepath: str) -> list[str]:
    """Check if file is in the correct location."""
    violations = []
    path = Path(filepath)
    filename = path.name

    # Check against required locations
    for pattern, expected_dir in REQUIRED_LOCATIONS.items():
        # Convert glob pattern to regex
        regex_pattern = pattern.replace("*", ".*")

        if re.match(regex_pattern, filename):
            # File matches pattern, check location
            Path(expected_dir) / filename
            if not str(path).startswith(expected_dir):
                violations.append(
                    f"File {filename} should be in {expected_dir}, found at {filepath}"
                )

    return violations


def main():
    """Main entry point for the linter."""
    if len(sys.argv) < 2:
        # If no files provided, check all staged files
        import subprocess

        result = subprocess.run(
            [
                "git",
                "diff",
                "--cached",
                "--name-only",
                "--diff-filter=A",
            ],  # Only added files
            capture_output=True,
            text=True,
        )
        files = [
            f for f in result.stdout.strip().split("\n") if f.endswith(".md") and f
        ]
    else:
        files = sys.argv[1:]

    if not files:
        sys.exit(0)

    all_violations = []
    for filepath in files:
        violations = check_file_location(filepath)
        all_violations.extend(violations)

    if all_violations:
        print("ERROR: Workflow file location violations:\n", file=sys.stderr)
        for violation in all_violations:
            print(f"  {violation}", file=sys.stderr)
        print(
            "\nSee docs/policies/agent-workflow-requirements.md for file location requirements.",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
