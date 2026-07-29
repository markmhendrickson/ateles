#!/usr/bin/env python3
from __future__ import annotations

"""
File naming convention checker.

Enforces file naming conventions from foundation-config.yaml:
- Python files: snake_case
- Shell scripts: kebab-case.sh
- Report files: specific patterns with dates
"""

import re
import sys
from pathlib import Path

# File naming patterns from foundation-config.yaml
PATTERNS = {
    # Python files: snake_case
    "**/*.py": [
        (
            r"^[a-z_][a-z0-9_]*\.py$",
            "Python files must use snake_case: example_script.py",
        ),
    ],
    # Shell scripts: kebab-case.sh
    "**/*.sh": [
        (
            r"^[a-z][a-z0-9-]*\.sh$",
            "Shell scripts must use kebab-case: example-script.sh",
        ),
    ],
    # Report files in finance directory
    "strategy/operations/finance/*-report-*.md": [
        (
            r"^btc-liquidity-regime-report-\d{4}-\d{2}-\d{2}\.md$",
            "BTC liquidity regime report format: btc-liquidity-regime-report-YYYY-MM-DD.md",
        ),
        (
            r"^altcoin-liquidity-regime-report-\d{4}-\d{2}-\d{2}\.md$",
            "Altcoin liquidity regime report format: altcoin-liquidity-regime-report-YYYY-MM-DD.md",
        ),
    ],
    # Quarterly portfolio reviews
    "strategy/operations/finance/quarterly-portfolio-review-*.md": [
        (
            r"^quarterly-portfolio-review-\d{4}-Q[1-4]\.md$",
            "Quarterly review format: quarterly-portfolio-review-YYYY-QX.md",
        ),
    ],
}


# Exceptions: files that don't need to match patterns
EXCEPTIONS = [
    "__init__.py",
    "__main__.py",
    "__pycache__",
    ".pyc",
    "setup.py",  # Common exception
    "conftest.py",  # pytest
]

# Path prefixes to exclude entirely (third-party libraries, venvs, etc.)
EXCLUDED_PATH_PREFIXES = [
    "execution/.venv",
    "execution/venv",
    ".venv",
]

# Specific files that are grandfathered (pre-existing hyphenated names)
GRANDFATHERED_FILES = {
    "execution/scripts/migrate-all-truth-repos.py",
    "execution/scripts/migrate-execution-data-to-datadir.py",
    "execution/scripts/pdf-field-debug.py",
    # Pre-existing snake_case shell scripts
    "execution/scripts/setup_neotoma_api_launchagent.sh",
    "execution/daemons/formica/load_ateles_repo_env.sh",
    "execution/scripts/manual_import_voice_memos.sh",
    "execution/scripts/setup_formica_launchagent.sh",
    "execution/scripts/install_formica_launchd_from_ateles_env.sh",
    "execution/scripts/run_neotoma_identity_proxy.sh",
    "execution/scripts/run_neotoma_api_prod_launchd.sh",
    "execution/scripts/archive/parquet/setup_parquet_mcp_tunnel.sh",
    "execution/scripts/run_formica_launchd.sh",
    "execution/scripts/archive/parquet/setup_parquet_mcp_cloudflare_tunnel.sh",
    "scripts/setup_claude_skills.sh",
}


def matches_pattern(
    filepath: Path, pattern_glob: str, rules: list[tuple[str, str]]
) -> str | None:
    """Check if file matches any rule for its pattern."""
    # Check if file matches the glob pattern
    if not filepath.match(pattern_glob):
        return None

    filename = filepath.name

    # Check exceptions
    if any(
        filename.startswith(except_prefix) or filename.endswith(except_suffix)
        for except_prefix in EXCEPTIONS
        for except_suffix in EXCEPTIONS
    ):
        return None

    # Check against rules
    for regex, message in rules:
        if re.match(regex, filename):
            return None  # Matches a valid pattern

    # No pattern matched
    if rules:
        return rules[0][1]  # Return first error message
    return f"File {filename} does not match required naming pattern"


def check_file(filepath: str) -> list[str]:
    """Check a single file for naming violations."""
    path = Path(filepath)
    violations = []

    # Check against all patterns
    for pattern_glob, rules in PATTERNS.items():
        error = matches_pattern(path, pattern_glob, rules)
        if error:
            violations.append(f"{filepath}: {error}")

    return violations


def main():
    """Main entry point for the linter."""
    # Pre-commit passes filenames via stdin or as arguments
    if len(sys.argv) > 1:
        files = sys.argv[1:]
    else:
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
        files = [f for f in result.stdout.strip().split("\n") if f]

    if not files:
        sys.exit(0)

    all_violations = []
    for filepath in files:
        if any(filepath.startswith(prefix) for prefix in EXCLUDED_PATH_PREFIXES):
            continue
        if filepath in GRANDFATHERED_FILES:
            continue
        violations = check_file(filepath)
        all_violations.extend(violations)

    if all_violations:
        print("ERROR: File naming convention violations:\n", file=sys.stderr)
        for violation in all_violations:
            print(f"  {violation}", file=sys.stderr)
        print(
            "\nSee foundation-config.yaml and docs/policies/agent-workflow-requirements.md for naming conventions.",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
