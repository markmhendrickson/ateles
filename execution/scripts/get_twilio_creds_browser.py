#!/usr/bin/env python3
"""
Simple browser-based Twilio credential extractor.
Opens browser, waits for user to navigate and copy credentials, then extracts them.
"""

import re
import sys
import time
from pathlib import Path

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
except ImportError:
    print("ERROR: Selenium not installed.")
    print("Install with: pip install selenium")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent.parent


def extract_credentials():
    """Extract Twilio credentials from browser."""
    print("=" * 60)
    print("Twilio Credentials Extractor")
    print("=" * 60)
    print("\nThis will:")
    print("1. Open Chrome browser")
    print("2. Navigate to Twilio Console Account Info")
    print("3. Wait for you to log in and navigate")
    print("4. Extract Account SID and Auth Token")
    print("\nStarting browser...")

    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")

    try:
        driver = webdriver.Chrome(options=chrome_options)
    except Exception as e:
        print(f"ERROR: Could not start Chrome: {e}")
        print("\nTrying with default ChromeDriver path...")
        try:
            driver = webdriver.Chrome()
        except Exception:
            print("ERROR: ChromeDriver not found.")
            print("Install with: brew install chromedriver")
            return None, None

    try:
        # Navigate to Account Info page
        print("\nOpening Twilio Console Account Info page...")
        driver.get("https://console.twilio.com/us1/develop/account/settings/general")

        print("\n" + "=" * 60)
        print("INSTRUCTIONS:")
        print("=" * 60)
        print("1. If you see a login page, please log in")
        print("2. Navigate to: Account → Account Info")
        print("3. Make sure Account SID is visible on the page")
        print("4. Click 'Show' next to Auth Token to reveal it")
        print("5. Come back here and press Enter")
        print("=" * 60)
        print("\nWaiting for you to complete the steps above...")
        print("Press Enter when Account SID and Auth Token are visible in the browser")

        input("\nPress Enter when ready...")

        # Extract Account SID
        print("\nExtracting credentials from page...")
        account_sid = None
        auth_token = None

        # Get page source and text
        page_source = driver.page_source
        page_text = driver.find_element(By.TAG_NAME, "body").text

        # Find Account SID (format: AC followed by 32 alphanumeric chars)
        sid_pattern = r"AC[a-zA-Z0-9]{32}"
        sid_matches = re.findall(sid_pattern, page_source)
        if sid_matches:
            # Get unique matches, prefer the one that looks most like Account SID
            unique_sids = list(set(sid_matches))
            # Account SID is usually the first/longest one
            account_sid = unique_sids[0] if unique_sids else None
            if account_sid:
                print(f"✓ Found Account SID: {account_sid}")

        # Try to find Auth Token (long alphanumeric string, not starting with AC)
        # Auth tokens are usually 32+ characters
        token_pattern = r"[a-zA-Z0-9]{32,}"
        potential_tokens = re.findall(token_pattern, page_text)

        # Filter out Account SID and other common patterns
        for token in potential_tokens:
            if (
                not token.startswith("AC")
                and len(token) >= 32
                and token != account_sid
                and not token.startswith("http")
                and " " not in token
            ):
                # This might be the auth token
                auth_token = token
                print(f"✓ Found potential Auth Token: {token[:20]}...")
                break

        # Also check input fields for auth token
        if not auth_token:
            try:
                inputs = driver.find_elements(By.TAG_NAME, "input")
                for inp in inputs:
                    value = inp.get_attribute("value")
                    if (
                        value
                        and len(value) >= 32
                        and not value.startswith("AC")
                        and not value.startswith("http")
                    ):
                        auth_token = value
                        print(
                            f"✓ Found Auth Token in input field: {auth_token[:20]}..."
                        )
                        break
            except Exception:
                pass

        print("\n" + "=" * 60)
        print("Extraction Results")
        print("=" * 60)

        if account_sid:
            print(f"✓ Account SID: {account_sid}")
        else:
            print("⚠️  Account SID not found")
            print("   Please copy it manually from the browser")
            account_sid = input("Enter Account SID (or press Enter to skip): ").strip()

        if auth_token:
            print(f"✓ Auth Token: {auth_token[:20]}...")
        else:
            print("⚠️  Auth Token not found automatically")
            print("   Please copy it from the browser (click 'Show' if needed)")
            auth_token = input("Enter Auth Token (or press Enter to skip): ").strip()

        # Save to .env
        if account_sid:
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
            if auth_token:
                env_lines.append(f"TWILIO_AUTH_TOKEN={auth_token}\n")
            else:
                env_lines.append("TWILIO_AUTH_TOKEN=REPLACE_WITH_TOKEN\n")

            with open(env_file, "w") as f:
                f.writelines(env_lines)

            print(f"\n✓ Saved credentials to {env_file}")

            if account_sid and auth_token:
                print("\n" + "=" * 60)
                print("Running debug script...")
                print("=" * 60)
                import subprocess

                subprocess.run(
                    [
                        sys.executable,
                        str(
                            PROJECT_ROOT
                            / "execution"
                            / "scripts"
                            / "debug_twilio_sms.py"
                        ),
                    ]
                )
            else:
                print("\nTo run debug script:")
                if account_sid:
                    print(
                        f"  python execution/scripts/debug_twilio_sms.py --account-sid {account_sid} --auth-token YOUR_TOKEN"
                    )
                else:
                    print(
                        "  python execution/scripts/debug_twilio_sms.py --account-sid AC... --auth-token YOUR_TOKEN"
                    )

        return account_sid, auth_token

    finally:
        print("\nClosing browser...")
        time.sleep(2)
        driver.quit()


if __name__ == "__main__":
    account_sid, auth_token = extract_credentials()
