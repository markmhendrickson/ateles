#!/usr/bin/env python3
"""
Security audit pre-commit hook.

Reads configuration from foundation-config.yaml and checks:
1. Protected paths (configured)
2. .env files (if enabled)
3. data/ directory (if enabled)
4. Sensitive file patterns (if enabled)

Gitleaks handles credential scanning separately.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print(
        "ERROR: PyYAML not installed. Install with: pip install PyYAML", file=sys.stderr
    )
    sys.exit(1)


def load_config():
    """Load security configuration from foundation-config.yaml"""
    config_path = Path("foundation-config.yaml")
    if not config_path.exists():
        print("ERROR: foundation-config.yaml not found", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    security_config = config.get("security", {}).get("pre_commit_audit", {})
    if not security_config.get("enabled", True):
        # Security audit disabled in config
        return None

    return security_config


def get_staged_files():
    """Get list of staged files"""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.split("\n") if line.strip()]


def get_unstaged_files():
    """Get list of unstaged files (new or modified)"""
    result = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return []

    files = []
    for line in result.stdout.split("\n"):
        if not line.strip():
            continue
        # Parse git status output: XY filename
        if len(line) >= 3:
            status = line[:2]
            filename = line[3:].strip()
            # Include added (A), modified (M), or untracked (??)
            if "A" in status or "M" in status or "?" in status:
                files.append(filename)
    return files


def check_protected_paths(files, protected_paths):
    """Check if any files match protected paths"""
    violations = []

    # Replace $DATA_DIR with actual pattern
    data_dir = os.getenv("DATA_DIR", "data")

    for path_pattern in protected_paths:
        # Skip .env* pattern here (handled by check_env_files)
        if path_pattern == ".env*" or path_pattern.startswith(".env"):
            continue

        # Convert config pattern to regex
        pattern = path_pattern.replace("$DATA_DIR/", f"{data_dir}/")
        pattern = pattern.replace("*", ".*")  # Convert glob * to regex .*

        # Match against files
        regex = re.compile(pattern)
        for file in files:
            if regex.search(file):
                violations.append((file, path_pattern))

    return violations


def check_env_files(files):
    """Check for .env files"""
    # Safe exceptions: example files and editor swap/backup files are not secrets
    allowed_prefixes = (".env.example", ".env.swp", ".env.bak", ".env.backups")
    violations = []
    for file in files:
        # Match files that start with .env or have /.env in path
        if file.startswith(".env") or "/.env" in file:
            if any(
                file.startswith(a) or ("/.env" in file and file.endswith(".example"))
                for a in allowed_prefixes
            ):
                continue
            violations.append(file)
    return violations


def check_data_directory(files):
    """Check for files in data/ directory"""
    data_dir = os.getenv("DATA_DIR", "data")
    violations = []
    for file in files:
        if file.startswith(f"{data_dir}/"):
            violations.append(file)
    return violations


def main():
    # Load configuration
    config = load_config()
    if config is None:
        # Security audit disabled
        sys.exit(0)

    # Get files to check
    staged_files = get_staged_files()
    unstaged_files = get_unstaged_files()
    all_files = list(set(staged_files + unstaged_files))

    if not all_files:
        sys.exit(0)

    violations_found = False

    # Check 1: Protected paths
    protected_paths = config.get("protected_paths", [])
    if protected_paths:
        violations = check_protected_paths(all_files, protected_paths)
        if violations:
            violations_found = True
            print(
                "❌ SECURITY VIOLATION: Files in protected paths detected!",
                file=sys.stderr,
            )
            print("", file=sys.stderr)
            for file, pattern in violations:
                print(f"  - {file} (matches: {pattern})", file=sys.stderr)
            print("", file=sys.stderr)
            print(
                "Protected paths are configured in foundation-config.yaml:",
                file=sys.stderr,
            )
            for path in protected_paths:
                print(f"  - {path}", file=sys.stderr)

    # Check 2: .env files (if enabled, default true)
    check_env = config.get("check_env_files", True)
    if check_env:
        env_violations = check_env_files(all_files)
        if env_violations:
            violations_found = True
            print("", file=sys.stderr)
            print("❌ SECURITY VIOLATION: .env files detected!", file=sys.stderr)
            print("", file=sys.stderr)
            for file in env_violations:
                print(f"  - {file}", file=sys.stderr)
            print("", file=sys.stderr)
            print(
                ".env files should not be committed (add to .gitignore)",
                file=sys.stderr,
            )

    # Check 3: data/ directory (if enabled, default true)
    check_data_dir = config.get("check_data_directory", True)
    if check_data_dir:
        data_violations = check_data_directory(all_files)
        if data_violations:
            violations_found = True
            print("", file=sys.stderr)
            print(
                "❌ SECURITY VIOLATION: Files in data/ directory detected!",
                file=sys.stderr,
            )
            print("", file=sys.stderr)
            for file in data_violations:
                print(f"  - {file}", file=sys.stderr)
            print("", file=sys.stderr)
            print(
                "data/ directory should not be committed (add to .gitignore)",
                file=sys.stderr,
            )

    if violations_found:
        print("", file=sys.stderr)
        print(
            "See foundation-config.yaml security.pre_commit_audit for configuration.",
            file=sys.stderr,
        )
        sys.exit(1)

    # All checks passed
    sys.exit(0)


if __name__ == "__main__":
    main()
