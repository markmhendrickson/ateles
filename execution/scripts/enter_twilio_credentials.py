#!/usr/bin/env python3
"""
Simple script to enter Twilio credentials and save them.
Run this after authenticating in Twilio Console.
"""

import os
import sys
from getpass import getpass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

print("=" * 60)
print("Twilio Credentials Entry")
print("=" * 60)
print("\nIn your Twilio Console browser window:")
print("1. Go to: Account → Account Info")
print("   OR: Develop → API Keys")
print("\nYou need:")
print("  - Account SID (starts with AC...)")
print("  - Auth Token (click 'Show' to reveal)")
print()

account_sid = input("Account SID: ").strip()
auth_token = getpass("Auth Token: ").strip()

if not account_sid or not auth_token:
    print("\n❌ Both values are required")
    sys.exit(1)

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
if env_lines and not env_lines[-1].endswith("\n"):
    env_lines.append("\n")
env_lines.append("\n# Twilio API Credentials\n")
env_lines.append(f"TWILIO_ACCOUNT_SID={account_sid}\n")
env_lines.append(f"TWILIO_AUTH_TOKEN={auth_token}\n")

# Write back
with open(ENV_FILE, "w") as f:
    f.writelines(env_lines)

print(f"\n✓ Credentials saved to {ENV_FILE}")
print("\nNow running debug script...")
print("=" * 60)

# Run debug script
os.system(
    f"cd {PROJECT_ROOT} && source execution/venv/bin/activate && python execution/scripts/debug_twilio_sms.py"
)
