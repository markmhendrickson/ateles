#!/usr/bin/env python3
"""
Extract Twilio credentials using existing Chrome profile (already signed in).

This script:
1. Connects to your existing Chrome profile (which should already be signed into Twilio)
2. Navigates to Account Info page
3. Extracts Account SID and Auth Token
4. Saves to .env file and runs debug script

Works with Chrome already running by using remote debugging or a separate profile copy.
"""

import asyncio
import platform
import re
import sys
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("ERROR: Playwright not installed.")
    print("Install with: pip install playwright && playwright install chromium")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent.parent
AUTH_STATE_DIR = PROJECT_ROOT / "playwright" / ".auth"
AUTH_STATE_DIR.mkdir(parents=True, exist_ok=True)


def find_chrome_profile():
    """
    Find Chrome/Chromium user data directory.

    Returns path to Chrome profile directory or None if not found.
    """
    system = platform.system()
    home = Path.home()

    chrome_data_paths = []

    if system == "Darwin":  # macOS
        chrome_data_paths = [
            home / "Library/Application Support/Google/Chrome",
            home / "Library/Application Support/Chromium",
        ]
    elif system == "Linux":
        chrome_data_paths = [
            home / ".config/google-chrome",
            home / ".config/chromium",
        ]
    elif system == "Windows":
        chrome_data_paths = [
            Path.home() / "AppData/Local/Google/Chrome/User Data",
        ]

    for chrome_data in chrome_data_paths:
        if chrome_data.exists():
            return chrome_data

    return None


async def extract_credentials_from_chrome():
    """
    Extract Twilio credentials using existing Chrome profile.

    Since Chrome might be running, we'll:
    1. Try to connect via remote debugging (if enabled)
    2. Or use a temporary profile copy
    3. Or use the profile directly (if Chrome is closed)
    """
    chrome_profile = find_chrome_profile()

    if not chrome_profile:
        print("⚠️  Chrome profile not found.")
        print("\nTo use this method:")
        print("1. Install Chrome/Chromium browser")
        print("2. Sign in to Twilio Console in Chrome")
        print("3. Run this script")
        sys.exit(1)

    print(f"✓ Found Chrome profile at: {chrome_profile}")

    # Check if Chrome is running
    chrome_running = False
    if platform.system() == "Darwin":
        result = asyncio.create_subprocess_exec(
            "pgrep",
            "-f",
            "Google Chrome",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc = await result
        stdout, _ = await proc.communicate()
        chrome_running = len(stdout) > 0

    if chrome_running:
        print("⚠️  Chrome is currently running.")
        print("\nOptions:")
        print("1. Close Chrome and run this script again (recommended)")
        print(
            "2. Use remote debugging (requires Chrome restart with --remote-debugging-port)"
        )
        print(
            "3. Use the browser console script instead: scripts/extract_twilio_creds_bookmarklet.js"
        )
        print("\nTrying to use profile anyway (may fail if Chrome has it locked)...")

    async with async_playwright() as p:
        print("\nLaunching browser with your Chrome profile...")

        # Try to use persistent context with existing profile
        # This will fail if Chrome is using the profile
        try:
            profile_path = chrome_profile / "Default"
            if not profile_path.exists():
                # Try Profile 1, Profile 2, etc.
                profiles = list(chrome_profile.glob("Profile *"))
                if profiles:
                    profile_path = profiles[0]  # Use first profile
                    print(f"  Using profile: {profile_path.name}")

            browser = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_path.parent),
                channel="chrome" if platform.system() != "Linux" else None,
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    f"--profile-directory={profile_path.name}",
                ],
                viewport={"width": 1920, "height": 1080},
            )

            # Get first page
            pages = browser.pages
            if pages:
                page = pages[0]
            else:
                page = await browser.new_page()

        except Exception as e:
            print(f"⚠️  Could not use existing profile (Chrome may be running): {e}")
            print("\nTrying alternative: Launch new browser and navigate to Twilio...")
            print("(You'll need to sign in manually)")

            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
            )
            page = await context.new_page()

        # Navigate to Twilio Account Info
        print("\nNavigating to Twilio Account Info...")
        try:
            await page.goto(
                "https://console.twilio.com/us1/develop/account/settings/general",
                wait_until="domcontentloaded",
                timeout=60000,
            )

            # Wait for page to load
            await asyncio.sleep(3)

            # Check if we need to login
            current_url = page.url
            if "login" in current_url.lower() or "sign" in current_url.lower():
                print("\n⚠️  Not logged in. Please sign in to Twilio in the browser.")
                print("Waiting 60 seconds for you to sign in...")
                await asyncio.sleep(60)

                # Check again
                current_url = page.url
                if "login" in current_url.lower():
                    print("⚠️  Still not logged in. Please sign in manually.")
                    print("Browser will stay open for 2 minutes...")
                    await asyncio.sleep(120)

        except Exception as e:
            print(f"⚠️  Error navigating: {e}")
            print("You can navigate manually in the browser")

        # Extract credentials
        print("\n" + "=" * 60)
        print("Extracting Credentials")
        print("=" * 60)

        account_sid = None
        auth_token = None

        # Get page content
        try:
            page_source = await page.content()
            await page.evaluate("() => document.body.innerText")

            # Find Account SID
            sid_pattern = r"AC[a-zA-Z0-9]{32}"
            sid_matches = re.findall(sid_pattern, page_source)
            if sid_matches:
                unique_sids = list(set(sid_matches))
                account_sid = unique_sids[0]
                print(f"✓ Found Account SID: {account_sid}")

            # Try to find Auth Token (long alphanumeric, not starting with AC)
            # First, check if "Show" button needs to be clicked
            try:
                # Look for button that reveals auth token
                show_buttons = await page.query_selector_all("button, a, span")
                for btn in show_buttons:
                    text = await btn.inner_text()
                    if text and ("show" in text.lower() or "reveal" in text.lower()):
                        # Check if it's near "auth token" or "auth" text
                        try:
                            await btn.click()
                            await asyncio.sleep(1)  # Wait for token to appear
                        except Exception:
                            pass
            except Exception:
                pass

            # Now look for auth token
            page_text_after = await page.evaluate("() => document.body.innerText")
            token_pattern = r"[a-zA-Z0-9]{32,}"
            all_tokens = re.findall(token_pattern, page_text_after)

            # Filter tokens
            for token in all_tokens:
                if (
                    len(token) >= 32
                    and not token.startswith("AC")
                    and not token.startswith("http")
                    and " " not in token
                    and token != account_sid
                ):
                    auth_token = token
                    print(f"✓ Found Auth Token: {auth_token[:20]}...")
                    break

            # Also check input fields
            if not auth_token:
                inputs = await page.query_selector_all(
                    'input[type="text"], input[type="password"], code, pre'
                )
                for inp in inputs:
                    try:
                        value = (
                            await inp.input_value()
                            if await inp.get_attribute("tagName") == "INPUT"
                            else await inp.inner_text()
                        )
                        if (
                            value
                            and len(value) >= 32
                            and not value.startswith("AC")
                            and not value.startswith("http")
                        ):
                            auth_token = value.strip()
                            print(f"✓ Found Auth Token in field: {auth_token[:20]}...")
                            break
                    except Exception:
                        continue

        except Exception as e:
            print(f"⚠️  Error extracting credentials: {e}")

        # If not found, prompt user
        if not account_sid:
            print("\n⚠️  Account SID not found automatically")
            print("Please look at the browser window and copy it manually")
            print("(It should be visible on the Account Info page)")
            # Keep browser open for manual copy
            print("\nBrowser will stay open for 30 seconds...")
            await asyncio.sleep(30)

        if not auth_token:
            print("\n⚠️  Auth Token not found automatically")
            print("Please click 'Show' next to Auth Token in the browser")
            print("Then copy it manually")
            # Keep browser open for manual copy
            print("\nBrowser will stay open for 30 seconds...")
            await asyncio.sleep(30)

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

                result = subprocess.run(
                    [
                        sys.executable,
                        str(
                            PROJECT_ROOT
                            / "execution"
                            / "scripts"
                            / "debug_twilio_sms.py"
                        ),
                    ],
                    cwd=str(PROJECT_ROOT),
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

        # Keep browser open briefly
        print("\nClosing browser in 5 seconds...")
        await asyncio.sleep(5)
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(extract_credentials_from_chrome())
    except KeyboardInterrupt:
        print("\n\nScript interrupted by user")
        sys.exit(0)
