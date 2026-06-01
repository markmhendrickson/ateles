"""
Sync selected secrets from 1Password into a local .env file.

Design:
- Read a mapping of ENV_VAR -> 1Password reference.
  Each reference is an op:// URL: op://<vault>/<item>/<field>
- Use the `op` CLI to resolve each secret.
- Update (or append) the corresponding ENV_VAR entries in the target .env file.

Features:
- Automatic backup: Creates timestamped backup in .env.backups/ before modification
- Session check: Verifies 1Password CLI session before proceeding
- Security: NEVER prints secret values, only variable names
- Comprehensive: Supports all environment variables used across the repository

Safety:
- This script NEVER prints secret values or CLI output that might contain secrets.
- It only prints which keys were updated.
- Backups are stored in .env.backups/ (gitignored via .env.* pattern).
- Run this locally, not in CI, and never commit .env to git.

Requirements:
- 1Password CLI (`op`) installed and signed in (`op signin`).
- Python 3.9+ recommended.

Configuration:
- Mappings stored in $DATA_DIR/env_var_mappings/env_var_mappings.parquet
- Update mappings via MCP parquet server (add_record/update_record) or edit parquet directly
- Variables with "PLACEHOLDER_" prefix are skipped until configured
- To find references: op item get "<item-name>" --format=json

Usage:
    cd /Users/markmhendrickson/repos/ateles
    python execution/scripts/op_sync_env_from_1password.py           # uses .env in repo root
    python execution/scripts/op_sync_env_from_1password.py path/to/.env.custom
"""

from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from scripts.config import DATA_DIR

# Try to import MCP client for 1Password
try:
    from execution.scripts.onepassword_client import OnePasswordMCPClient

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


def load_env_mappings() -> dict[str, str]:
    """
    Load environment variable to 1Password op:// reference mappings from parquet file.

    Returns:
        Dictionary mapping env_var names to op:// references

    Configuration is stored in $DATA_DIR/env_var_mappings/env_var_mappings.parquet
    Update mappings via MCP parquet server or by editing the parquet file directly.
    """
    # Use DATA_DIR from config (respects DATA_DIR env var or defaults appropriately)
    mappings_file = DATA_DIR / "env_var_mappings" / "env_var_mappings.parquet"

    if not mappings_file.exists():
        raise FileNotFoundError(
            f"Environment variable mappings file not found: {mappings_file}\n"
            "Create it using the MCP parquet server or by running the migration script."
        )

    df = pd.read_parquet(mappings_file)
    env_to_op_ref: dict[str, str] = {}

    # Get current environment for environment-based keys
    current_env = os.getenv("ENVIRONMENT", "development").lower()

    for _, row in df.iterrows():
        env_var = row["env_var"]
        op_ref = row["op_reference"]

        # Skip placeholder values
        if pd.isna(op_ref) or str(op_ref).startswith("PLACEHOLDER_"):
            continue

        # Handle environment-based keys (e.g., OpenAI API key)
        if row.get("environment_based", False):
            # Only include if this row matches the current environment
            env_key = str(row.get("environment_key", "")).lower()
            if env_key != current_env:
                continue

        # For environment-based keys, we might have multiple rows (dev/prod)
        # Only add if not already present, or replace if this is the matching environment
        if env_var not in env_to_op_ref or row.get("environment_based", False):
            env_to_op_ref[env_var] = str(op_ref)

    return env_to_op_ref


def check_op_session() -> bool:
    """
    Check if 1Password CLI session is active (via MCP or CLI).

    Returns:
        True if session is active, False otherwise.

    Security: Never prints any output from `op whoami` to avoid exposing tokens.
    """
    # Try MCP first (preferred - persistent connection)
    if MCP_AVAILABLE:
        try:
            client = OnePasswordMCPClient()
            return client.check_session()
        except Exception:
            pass  # Fall through to CLI fallback

    # CLI fallback (original implementation)
    try:
        result = subprocess.run(
            ["op", "whoami"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def backup_env_file(env_path: Path) -> Path | None:
    """
    Create timestamped backup of .env file in .env.backups/ directory.

    Args:
        env_path: Path to .env file to backup

    Returns:
        Path to backup file if backup was created, None if file didn't exist

    Security: Never prints file contents, only paths.
    """
    if not env_path.exists():
        return None

    # Determine repo root (go up from execution/scripts/ to repo root)
    repo_root = Path(__file__).parent.parent
    backup_dir = repo_root / ".env.backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Create timestamped backup filename
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    backup_filename = f".env-{timestamp}"
    backup_path = backup_dir / backup_filename

    # Copy file contents
    backup_path.write_text(env_path.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"Backup created: {backup_path}")
    return backup_path


def op_read(ref: str) -> str:
    """
    Read a secret value from 1Password using MCP server (preferred) or CLI (fallback).

    Security: Error messages never include CLI output that might contain secrets.
    """
    # Try MCP first (preferred - persistent connection, no session expiration)
    if MCP_AVAILABLE:
        try:
            client = OnePasswordMCPClient()
            value = client.read_secret(ref)
            if not value:
                raise RuntimeError(f"Empty value returned for 1Password ref: {ref}")
            return value
        except Exception as e:
            # Fallback to CLI if MCP fails
            print(f"  WARNING: MCP failed, falling back to CLI: {e}")

    # CLI fallback (original implementation)
    try:
        result = subprocess.run(
            ["op", "read", ref],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:  # noqa: BLE001
        # SECURITY: Never include e.stderr or e.stdout in error message
        # as they might contain sensitive information
        raise RuntimeError(
            f"1Password error for {ref}. "
            f"Ensure 'op' is installed and signed in (run: op signin), or configure MCP server."
        ) from e

    value = result.stdout.rstrip("\n")
    if not value:
        raise RuntimeError(f"Empty value returned for 1Password ref: {ref}")
    return value


def load_env_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return text.splitlines()


def write_env_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def sync_env(target_path: Path) -> None:
    """
    Resolve all environment variable mappings via op, then update the .env file.

    Security: Only prints environment variable names, never values.
    Skips variables with PLACEHOLDER references that need to be configured.
    """
    print(f"Target .env file: {target_path}")

    # Load mappings from parquet file
    try:
        env_to_op_ref = load_env_mappings()
    except FileNotFoundError as e:
        print(f"\nERROR: {e}")
        return

    if not env_to_op_ref:
        print("\nWARNING: No environment variable mappings found in parquet file.")
        return

    env_lines = load_env_lines(target_path)
    # Maintain original file order; we just replace or append keys we manage.

    # Build index of existing keys
    key_to_index: dict[str, int] = {}
    for idx, line in enumerate(env_lines):
        if not line or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            continue
        k = line.split("=", 1)[0].strip()
        if k:
            key_to_index[k] = idx

    updated = []
    skipped = []
    environment = os.getenv("ENVIRONMENT", "development").lower()

    for env_key, op_ref in env_to_op_ref.items():
        # Skip placeholder values that need to be configured
        if op_ref.startswith("PLACEHOLDER_"):
            skipped.append(env_key)
            continue

        # Show environment info for environment-based keys
        if env_key == "OPENAI_API_KEY":
            print(f"- Resolving {env_key} (using {environment} key)...")
        else:
            print(f"- Resolving {env_key}...")

        try:
            value = op_read(op_ref)
        except RuntimeError as e:
            print(f"  WARNING: Failed to resolve {env_key}: {e}")
            continue

        new_line = f'{env_key}="{value}"'
        if env_key in key_to_index:
            idx = key_to_index[env_key]
            env_lines[idx] = new_line
        else:
            env_lines.append(new_line)
        updated.append(env_key)

    write_env_lines(target_path, env_lines)

    if updated:
        print("\nUpdated keys in .env (values NOT shown):")
        for k in updated:
            print(f"  - {k}")
    else:
        print("\nNo keys were updated.")

    if skipped:
        print("\nSkipped keys with PLACEHOLDER references (need configuration):")
        for k in skipped:
            print(f"  - {k}")
        print("\nTo configure these variables:")
        print('  1. Find the 1Password item: op item get "<item-name>" --format=json')
        print(
            f"  2. Update {DATA_DIR}/env_var_mappings/env_var_mappings.parquet with actual op:// references"
        )
        print(
            "  3. Use MCP parquet server (add_record/update_record) or edit parquet directly"
        )
        print("  4. Remove the PLACEHOLDER_ prefix from op_reference field")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync selected env vars from 1Password into a .env file.",
    )
    parser.add_argument(
        "env_path",
        nargs="?",
        help="Path to .env file (default: .env in repo root)",
    )
    args = parser.parse_args()

    if args.env_path:
        target = Path(args.env_path).expanduser()
    else:
        # Default to .env in current working directory
        target = Path(os.getcwd()) / ".env"

    # Step 1: Check 1Password session
    print("Checking 1Password session...")
    if not check_op_session():
        print("\nERROR: 1Password CLI is not authenticated.")
        print("Please sign in to 1Password:")
        print("  eval $(op signin)")
        print("\nOr if using desktop app integration:")
        print("  op signin")
        return 1
    print("✓ 1Password session active\n")

    # Step 2: Backup existing .env file
    print("Creating backup...")
    backup_path = backup_env_file(target)
    if backup_path:
        print(f"✓ Backup created: {backup_path}\n")
    else:
        print(f"No existing .env file to backup at: {target}\n")

    # Step 3: Sync environment variables
    print("Syncing environment variables from 1Password...\n")
    try:
        sync_env(target)
        print("\n✓ Sync completed successfully!")
        return 0
    except Exception as e:
        print(f"\n✗ Sync failed: {e}")
        if backup_path:
            print(f"\nYou can restore from backup: {backup_path}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
