#!/usr/bin/env python3
"""
Send a test SMS via Twilio.

Uses credentials from .env (or fetches from 1Password if not found).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent.parent


def get_twilio_credentials():
    """Get Twilio credentials from .env or 1Password, save to .env if fetched."""
    env_file = PROJECT_ROOT / ".env"

    # First, try loading from .env
    load_dotenv(env_file)
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if account_sid and auth_token:
        return account_sid, auth_token

    # If not in .env, fetch from 1Password
    print("Twilio credentials not found in .env, fetching from 1Password...")

    try:
        result = subprocess.run(
            [
                "op",
                "item",
                "get",
                "Twilio (for Twilio)",
                "--vault",
                "Private",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        item = json.loads(result.stdout)

        account_sid = None
        auth_token = None

        for field in item.get("fields", []):
            label = field.get("label", "").lower().strip()
            if label == "account sid":
                account_sid = field.get("value", "")
            elif label == "auth token":
                auth_token = field.get("value", "")

        if account_sid and auth_token:
            # Save to .env for future use
            save_to_env(account_sid, auth_token)
            print("✓ Credentials saved to .env file for future use")
            return account_sid, auth_token
        else:
            print("ERROR: Could not find Account SID or Auth Token in 1Password")
            sys.exit(1)

    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to get credentials from 1Password: {e.stderr}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


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


def send_sms(account_sid, auth_token, to_number, from_number, message):
    """Send SMS via Twilio API."""
    import base64
    import urllib.parse
    import urllib.request

    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

    data = urllib.parse.urlencode(
        {"From": from_number, "To": to_number, "Body": message}
    ).encode("utf-8")

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode())

            if result.get("sid"):
                print("✓ SMS sent successfully!")
                print(f"  Message SID: {result.get('sid')}")
                print(f"  Status: {result.get('status')}")
                print(f"  From: {result.get('from')}")
                print(f"  To: {result.get('to')}")
                print(f"  Body: {result.get('body')}")
                return True
            else:
                print("ERROR: Failed to send SMS")
                print(f"Response: {result}")
                return False
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"ERROR: HTTP {e.code} - {e.reason}")
        print(f"Response: {error_body}")
        return False
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback

        traceback.print_exc()
        return False


def main():
    """Main function."""
    import argparse

    parser = argparse.ArgumentParser(description="Send test SMS via Twilio")
    parser.add_argument(
        "--to",
        default=os.getenv("TWILIO_TEST_PHONE", ""),
        help="Recipient phone number (set TWILIO_TEST_PHONE env var or use --to)",
    )
    parser.add_argument(
        "--from",
        dest="from_number",
        help="Sender phone number (default: uses same as --to)",
    )
    parser.add_argument(
        "--message", default="Test SMS from Twilio API", help="Message to send"
    )
    args = parser.parse_args()

    to_number = args.to
    from_number = (
        args.from_number or to_number
    )  # Default to same number if not specified

    account_sid, auth_token = get_twilio_credentials()

    print("Sending test SMS:")
    print(f"  From: {from_number}")
    print(f"  To: {to_number}")
    print(f"  Message: {args.message}")
    print()

    success = send_sms(account_sid, auth_token, to_number, from_number, args.message)

    if success:
        print("\n✓ Test SMS sent! Check the recipient number for the message.")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
