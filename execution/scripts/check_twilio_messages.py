#!/usr/bin/env python3
"""
Check Twilio SMS messages for verification codes.

Fetches credentials from .env (if available) or 1Password, saves to .env for future use.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
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


def check_messages(account_sid, auth_token, phone_number=None, minutes=15):
    """Check Twilio messages for verification codes.

    Args:
        phone_number: Phone number to check (defaults to TWILIO_TEST_PHONE env var)
    """
    if phone_number is None:
        phone_number = os.getenv("TWILIO_TEST_PHONE", "")
        if not phone_number:
            print(
                "Error: Phone number required. Set TWILIO_TEST_PHONE env var or pass --phone"
            )
            sys.exit(1)
    """Check Twilio messages for verification codes."""
    import base64
    import re
    import urllib.request

    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()

    # Check recent messages
    since = (datetime.utcnow() - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json?To={phone_number}&DateSent%3E={since}"

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {credentials}")

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())

            if data.get("messages"):
                print(
                    f"Found {len(data['messages'])} message(s) in last {minutes} minutes:\n"
                )
                for msg in data["messages"]:
                    body = msg.get("body", "")
                    from_num = msg.get("from", "")
                    date = msg.get("date_sent", "")
                    status = msg.get("status", "")

                    print(f"From: {from_num}")
                    print(f"Date: {date}")
                    print(f"Status: {status}")
                    print(f"Body: {body}")

                    # Check if it looks like a verification code
                    body_lower = body.lower()
                    if any(
                        keyword in body_lower
                        for keyword in [
                            "verification",
                            "code",
                            "whatsapp",
                            "meta",
                            "telegram",
                            "verify",
                        ]
                    ):
                        print("*** POTENTIAL VERIFICATION CODE ***")
                    if re.search(r"\b\d{4,8}\b", body):
                        print("*** CONTAINS NUMERIC CODE ***")
                    print("---")
            else:
                print(f"No messages in the last {minutes} minutes")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


def main():
    """Main function."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Check Twilio SMS messages for verification codes"
    )
    parser.add_argument(
        "--phone",
        type=str,
        default=None,
        help="Phone number to check (defaults to TWILIO_TEST_PHONE env var)",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=15,
        help="Number of minutes to look back (default: 15)",
    )
    args = parser.parse_args()

    account_sid, auth_token = get_twilio_credentials()
    check_messages(
        account_sid, auth_token, phone_number=args.phone, minutes=args.minutes
    )


if __name__ == "__main__":
    main()
