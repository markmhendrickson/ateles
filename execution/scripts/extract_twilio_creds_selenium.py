#!/usr/bin/env python3
"""
Extract Twilio credentials using Selenium browser automation.
Opens Twilio Console, navigates to Account Info, and extracts credentials.
"""

import sys
import time
from pathlib import Path

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC  # noqa: N812
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    print("ERROR: Selenium not installed.")
    print("Install with: pip install selenium")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent.parent


def extract_credentials():
    """Extract Twilio credentials from browser."""
    print("Launching browser...")

    # Setup Chrome options
    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")
    # Don't run headless so user can see and interact

    try:
        driver = webdriver.Chrome(options=chrome_options)
    except Exception as e:
        print(f"ERROR: Could not start Chrome browser: {e}")
        print("\nMake sure ChromeDriver is installed:")
        print("  brew install chromedriver")
        print("  OR download from: https://chromedriver.chromium.org/")
        return None, None

    try:
        print("Navigating to Twilio Console...")
        driver.get("https://console.twilio.com/us1/develop/account/settings/general")

        print("\n" + "=" * 60)
        print("Please log in to Twilio Console if prompted")
        print("Waiting for page to load...")
        print("=" * 60)

        # Wait for page to load
        time.sleep(5)

        # Wait for account info to load
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception:
            print("Page loaded (may need manual login)")

        # Give user time to login if needed
        print("\nIf you see a login page, please log in now...")
        print("Waiting 15 seconds for login...")
        time.sleep(15)

        # Try to extract Account SID
        print("\nExtracting Account SID...")
        account_sid = None

        # Method 1: Look for Account SID in page source
        page_source = driver.page_source
        import re

        sid_matches = re.findall(r"AC[a-zA-Z0-9]{32}", page_source)
        if sid_matches:
            # Get unique matches
            unique_sids = list(set(sid_matches))
            if unique_sids:
                account_sid = unique_sids[0]
                print(f"Found Account SID in page: {account_sid}")

        # Method 2: Try to find in visible text
        if not account_sid:
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
                sid_match = re.search(r"AC[a-zA-Z0-9]{32}", body_text)
                if sid_match:
                    account_sid = sid_match.group(0)
                    print(f"Found Account SID in visible text: {account_sid}")
            except Exception:
                pass

        # Method 3: Try localStorage
        if not account_sid:
            try:
                storage_items = driver.execute_script(
                    """
                    var items = {};
                    for (var i = 0; i < localStorage.length; i++) {
                        var key = localStorage.key(i);
                        items[key] = localStorage.getItem(key);
                    }
                    return items;
                """
                )
                for key, value in storage_items.items():
                    if value and value.startswith("AC") and len(value) == 34:
                        account_sid = value
                        print(f"Found Account SID in localStorage: {account_sid}")
                        break
            except Exception as e:
                print(f"Could not check localStorage: {e}")

        print("\n" + "=" * 60)
        print("Credentials Extraction Results")
        print("=" * 60)

        if account_sid:
            print(f"\n✓ Found Account SID: {account_sid}")
        else:
            print("\n⚠️  Account SID not found automatically")
            print("   Please look at the browser window and copy it manually")
            print("   It should be visible on the Account Info page")

        print("\n⚠️  Auth Token must be retrieved manually:")
        print("   1. On the Account Info page, look for 'Auth Token'")
        print("   2. Click 'Show' or 'Reveal' next to it")
        print("   3. Copy the token")

        # Keep browser open for user to manually copy auth token
        print("\n" + "=" * 60)
        print("Browser will stay open for 30 seconds")
        print("Please copy the Auth Token from the page")
        print("Press Enter in this terminal when done, or wait 30 seconds")
        print("=" * 60)

        # Wait for user input or timeout
        import select
        import sys

        if sys.stdin.isatty():
            # Try to wait for Enter key
            try:
                import termios
                import tty

                old_settings = termios.tcgetattr(sys.stdin)
                tty.setraw(sys.stdin.fileno())
                print("\nPress Enter to continue (or wait 30 seconds)...")
                start_time = time.time()
                while time.time() - start_time < 30:
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        if sys.stdin.read(1) == "\r" or sys.stdin.read(1) == "\n":
                            break
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            except Exception:
                time.sleep(30)
        else:
            time.sleep(30)

        # Try to find auth token if it was revealed
        auth_token = None
        if account_sid:
            try:
                # Look for input fields or text that might contain auth token
                inputs = driver.find_elements(By.TAG_NAME, "input")
                for inp in inputs:
                    value = inp.get_attribute("value")
                    if value and len(value) > 20 and not value.startswith("AC"):
                        auth_token = value
                        break

                # Also check for code/pre elements
                code_elements = driver.find_elements(By.TAG_NAME, "code")
                for code in code_elements:
                    text = code.text.strip()
                    if text and len(text) > 20 and not text.startswith("AC"):
                        auth_token = text
                        break
            except Exception:
                pass

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
                env_lines.append("TWILIO_AUTH_TOKEN=REPLACE_WITH_TOKEN_FROM_CONSOLE\n")

            with open(env_file, "w") as f:
                f.writelines(env_lines)

            print(f"\n✓ Saved Account SID to {env_file}")
            if auth_token:
                print(f"✓ Saved Auth Token to {env_file}")
            else:
                print(f"⚠️  Please add Auth Token to {env_file} manually")
                print(
                    f"   Or run: python execution/scripts/debug_twilio_sms.py --account-sid {account_sid} --auth-token YOUR_TOKEN"
                )

            print("\nNow you can run:")
            print("  python execution/scripts/debug_twilio_sms.py")

        return account_sid, auth_token

    finally:
        print("\nClosing browser in 5 seconds...")
        time.sleep(5)
        driver.quit()


if __name__ == "__main__":
    account_sid, auth_token = extract_credentials()
    if account_sid:
        print("\n✓ Extraction complete!")
        if not auth_token:
            print(
                "⚠️  Remember to add Auth Token to .env file or pass it via --auth-token"
            )
