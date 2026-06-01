#!/usr/bin/env python3
"""
Get Twilio credentials from 1Password and run debug script.

Retrieves Account SID and Auth Token from 1Password item "Twilio (for Twilio)"
and saves them to .env file, then runs the debug script.
"""

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent


def get_1password_item(item_name="Twilio (for Twilio)", vault="Private"):
    """Get 1Password item by name."""
    try:
        cmd = ["op", "item", "get", item_name, "--vault", vault, "--format", "json"]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=30
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to retrieve item from 1Password: {e.stderr}")
        print("\nMake sure:")
        print("  1. 1Password CLI is installed: brew install --cask 1password-cli")
        print("  2. You're signed in: op signin")
        print("  3. Item exists: 'Twilio (for Twilio)' in 'Private' vault")
        sys.exit(1)
    except json.JSONDecodeError:
        print("ERROR: Invalid JSON from 1Password CLI")
        sys.exit(1)
    except FileNotFoundError:
        print("ERROR: 1Password CLI not found")
        print("Install with: brew install --cask 1password-cli")
        sys.exit(1)


def extract_credentials_from_item(item_data):
    """Extract Account SID and Auth Token from 1Password item."""
    account_sid = None
    auth_token = None
    item_data.get("id")
    item_data.get("vault", {}).get("name", "Private")

    # Look through fields - match exact labels from 1Password item
    for field in item_data.get("fields", []):
        label = field.get("label", "").lower().strip()
        value = field.get("value", "")
        reference = field.get("reference", "")

        # Account SID - exact match for "account sid"
        if label == "account sid":
            account_sid = value
        # Auth Token - exact match for "auth token"
        elif label == "auth token":
            auth_token = value

    # If values weren't found in item data, try reading via op read using references
    if not account_sid or not auth_token:
        for field in item_data.get("fields", []):
            label = field.get("label", "").lower().strip()
            reference = field.get("reference", "")

            if label == "account sid" and reference and not account_sid:
                try:
                    result = subprocess.run(
                        ["op", "read", reference],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=10,
                    )
                    account_sid = result.stdout.strip()
                except Exception:
                    pass

            elif label == "auth token" and reference and not auth_token:
                try:
                    result = subprocess.run(
                        ["op", "read", reference],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=10,
                    )
                    auth_token = result.stdout.strip()
                except Exception:
                    pass

    return account_sid, auth_token


def save_to_env(account_sid, auth_token):
    """Save credentials to .env file."""
    env_file = PROJECT_ROOT / ".env"
    env_lines = []

    if env_file.exists():
        with open(env_file) as f:
            env_lines = f.readlines()

    # Remove existing Twilio entries
    env_lines = [
        line
        for line in env_lines
        if not line.strip().startswith("TWILIO_ACCOUNT_SID")
        and not line.strip().startswith("TWILIO_AUTH_TOKEN")
    ]

    # Add new entries
    if env_lines and not env_lines[-1].endswith("\n"):
        env_lines.append("\n")
    env_lines.append("\n# Twilio API Credentials\n")
    env_lines.append(f"TWILIO_ACCOUNT_SID={account_sid}\n")
    env_lines.append(f"TWILIO_AUTH_TOKEN={auth_token}\n")

    with open(env_file, "w") as f:
        f.writelines(env_lines)

    print(f"✓ Saved credentials to {env_file}")


def main():
    """Main function."""
    print("=" * 60)
    print("Getting Twilio Credentials from 1Password")
    print("=" * 60)

    # Get item from 1Password
    print("\nRetrieving 'Twilio (for Twilio)' from 1Password...")
    item_data = get_1password_item("Twilio (for Twilio)", "Private")

    # Extract credentials
    print("Extracting Account SID and Auth Token...")
    account_sid, auth_token = extract_credentials_from_item(item_data)

    if not account_sid:
        print("\n⚠️  Account SID not found in 1Password item")
        print("\nAvailable fields:")
        for field in item_data.get("fields", []):
            label = field.get("label", "unnamed")
            print(f"  - {label}")
        sys.exit(1)

    if not auth_token:
        print("\n⚠️  Auth Token not found in 1Password item")
        print("\nAvailable fields:")
        for field in item_data.get("fields", []):
            label = field.get("label", "unnamed")
            print(f"  - {label}")
        sys.exit(1)

    # Validate Account SID format (should start with "AC")
    if not account_sid.startswith("AC"):
        print("\n⚠️  WARNING: Account SID doesn't look correct!")
        print(f"   Found: {account_sid}")
        print(
            "   Expected: Should start with 'AC' (e.g., ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx)"
        )
        print("\n   The value in 1Password may be incorrect.")
        print(
            "   Please verify in Twilio Console: https://console.twilio.com/us1/develop/account/settings/general"
        )
        print(
            "   Then update the 'account sid' field in 1Password item 'Twilio (for Twilio)'"
        )
        sys.exit(1)

    print(f"✓ Found Account SID: {account_sid}")
    print(f"✓ Found Auth Token: {auth_token[:20]}...")

    # Save to .env
    save_to_env(account_sid, auth_token)

    # Run debug script
    print("\n" + "=" * 60)
    print("Running Twilio SMS Debug Script")
    print("=" * 60)

    debug_script = PROJECT_ROOT / "execution" / "scripts" / "debug_twilio_sms.py"
    result = subprocess.run([sys.executable, str(debug_script)], cwd=str(PROJECT_ROOT))

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
