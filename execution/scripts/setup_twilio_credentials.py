#!/usr/bin/env python3
"""
Interactive script to set up Twilio credentials.
Opens browser to Twilio Console and prompts for credentials.
"""

import os
import webbrowser
from getpass import getpass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def open_twilio_console():
    """Open Twilio Console in browser."""
    print("Opening Twilio Console in your browser...")
    print("\nYou'll need:")
    print("  1. Account SID (starts with AC...)")
    print("  2. Auth Token")
    print("\nLocations in Twilio Console:")
    print("  - Account → Account Info (main credentials)")
    print("  - Develop → API Keys (if using API keys)")
    print()

    # Open API Keys page (most common)
    webbrowser.open(
        "https://console.twilio.com/us1/develop/account/keys-credentials/api-keys"
    )

    # Also open Account Info page
    print("Also opening Account Info page...")
    webbrowser.open("https://console.twilio.com/us1/develop/account/settings/general")


def get_credentials_interactive():
    """Get credentials from user input."""
    print("\n" + "=" * 60)
    print("Enter Twilio Credentials")
    print("=" * 60)

    account_sid = input("\nAccount SID (AC...): ").strip()
    if not account_sid.startswith("AC"):
        print("⚠️  Warning: Account SID should start with 'AC'")
        confirm = input("Continue anyway? (y/n): ").strip().lower()
        if confirm != "y":
            return None, None

    auth_token = getpass("Auth Token (hidden): ").strip()

    if not account_sid or not auth_token:
        print("❌ Both Account SID and Auth Token are required")
        return None, None

    return account_sid, auth_token


def save_to_env(account_sid: str, auth_token: str):
    """Save credentials to .env file."""
    # Read existing .env if it exists
    env_lines = []
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            env_lines = f.readlines()

    # Remove existing Twilio entries
    env_lines = [
        line
        for line in env_lines
        if not line.strip().startswith("TWILIO_ACCOUNT_SID")
        and not line.strip().startswith("TWILIO_AUTH_TOKEN")
    ]

    # Add new entries
    env_lines.append("\n# Twilio API Credentials\n")
    env_lines.append(f"TWILIO_ACCOUNT_SID={account_sid}\n")
    env_lines.append(f"TWILIO_AUTH_TOKEN={auth_token}\n")

    # Write back
    with open(ENV_FILE, "w") as f:
        f.writelines(env_lines)

    print(f"\n✓ Credentials saved to {ENV_FILE}")
    print("  (This file is in .gitignore and won't be committed)")


def test_credentials(account_sid: str, auth_token: str) -> bool:
    """Test if credentials work."""
    try:
        from twilio.rest import Client

        client = Client(account_sid, auth_token)
        account = client.api.accounts(account_sid).fetch()
        print("\n✓ Credentials verified!")
        print(f"  Account: {account.friendly_name}")
        return True
    except Exception as e:
        print(f"\n❌ Credential test failed: {e}")
        return False


def main():
    """Main function."""
    print("Twilio Credentials Setup")
    print("=" * 60)

    # Check if already set
    if ENV_FILE.exists():
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE)
        if os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN"):
            print("✓ Credentials already found in .env file")
            use_existing = input("Use existing credentials? (y/n): ").strip().lower()
            if use_existing == "y":
                print("\nRunning debug script with existing credentials...")
                os.system(
                    f"cd {PROJECT_ROOT} && python execution/scripts/debug_twilio_sms.py"
                )
                return

    # Open browser
    open_twilio_console()

    # Get credentials
    account_sid, auth_token = get_credentials_interactive()
    if not account_sid or not auth_token:
        print("\n❌ Setup cancelled")
        return

    # Test credentials
    print("\nTesting credentials...")
    if not test_credentials(account_sid, auth_token):
        retry = input("\nRetry? (y/n): ").strip().lower()
        if retry == "y":
            account_sid, auth_token = get_credentials_interactive()
            if account_sid and auth_token:
                test_credentials(account_sid, auth_token)
        else:
            print("Setup cancelled")
            return

    # Save credentials
    save_to_env(account_sid, auth_token)

    # Run debug script
    print("\n" + "=" * 60)
    print("Running debug script...")
    print("=" * 60)
    os.system(f"cd {PROJECT_ROOT} && python execution/scripts/debug_twilio_sms.py")


if __name__ == "__main__":
    main()
