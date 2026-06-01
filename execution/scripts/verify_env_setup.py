#!/usr/bin/env python3
"""
Verify environment variable setup for STX balance scripts.

This script checks:
1. Required Python packages are installed
2. .env file exists and contains required variables
3. Environment variables are accessible

Run this before attempting to fetch STX balances.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# Color codes for terminal output
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"


def check_package(package_name: str) -> bool:
    """Check if a Python package is installed."""
    try:
        __import__(package_name.replace("-", "_"))
        return True
    except ImportError:
        return False


def main() -> int:
    print("=" * 80)
    print("Environment Setup Verification")
    print("=" * 80)
    print()

    # Check Python packages
    print("1. Checking Python packages...")
    packages = ["requests", "dotenv", "pandas", "pyarrow"]
    package_status = {}

    for pkg in packages:
        installed = check_package(pkg)
        package_status[pkg] = installed
        status_icon = f"{GREEN}✓{RESET}" if installed else f"{RED}✗{RESET}"
        display_name = "python-dotenv" if pkg == "dotenv" else pkg
        print(f"   {status_icon} {display_name}")

    print()

    # Check .env file
    print("2. Checking .env file...")
    repo_root = Path(__file__).resolve().parent.parent.parent
    dotenv_path = repo_root / ".env"

    if dotenv_path.exists():
        print(f"   {GREEN}✓{RESET} .env file exists at {dotenv_path}")

        # Try to load it
        if package_status.get("dotenv"):
            from dotenv import load_dotenv

            load_dotenv(dotenv_path)
    else:
        print(f"   {YELLOW}⚠{RESET} .env file not found at {dotenv_path}")
        print("   Run: python execution/scripts/op_sync_env_from_1password.py")

    print()

    # Check environment variables
    print("3. Checking environment variables...")
    env_vars = {
        "COINBASE_API_KEY": "Coinbase API key",
        "COINBASE_API_SECRET": "Coinbase API secret",
        "COINBASE_API_PASSPHRASE": "Coinbase passphrase (optional)",
        "HIRO_PLATFORM_API_KEY": "Hiro Platform API key (optional)",
    }

    all_set = True
    for var, description in env_vars.items():
        value = os.getenv(var)
        is_optional = "(optional)" in description

        if value:
            # Show first 4 chars only
            masked = value[:4] + "..." if len(value) > 4 else "***"
            print(f"   {GREEN}✓{RESET} {var}: {masked}")
        elif is_optional:
            print(f"   {YELLOW}⚠{RESET} {var}: Not set (optional)")
        else:
            print(f"   {RED}✗{RESET} {var}: Not set")
            all_set = False

    print()
    print("=" * 80)

    # Summary
    if not all(package_status.values()):
        print(f"{RED}✗ Missing packages{RESET}")
        print("  Run: pip install -r scripts/requirements.txt")
        print()
        return 1

    if not all_set:
        print(f"{YELLOW}⚠ Missing required environment variables{RESET}")
        print("  Run: python execution/scripts/op_sync_env_from_1password.py")
        print("  Or manually set: COINBASE_API_KEY, COINBASE_API_SECRET")
        print()
        return 1

    print(f"{GREEN}✓ All checks passed!{RESET}")
    print()
    print("You can now run:")
    print("  python execution/scripts/stx_fetch_onchain_balances.py")
    print("  python execution/scripts/stx_fetch_coinbase_stx_balances.py")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
