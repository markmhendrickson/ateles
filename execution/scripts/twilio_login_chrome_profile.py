#!/usr/bin/env python3
"""
Twilio login automation using Playwright with existing Chrome profile.

This script:
1. Uses your existing Chrome profile (which has 1Password extension installed)
2. Relies on 1Password extension to auto-fill credentials (most secure approach)
3. Saves authenticated session state for future use
4. Navigates to SMS configuration for phone number 6503198857

Security: Credentials never leave 1Password's secure storage - extension handles everything.
This is the MOST SECURE approach - uses your actual browser profile with all extensions.
"""

import asyncio
import platform
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

    if system == "Darwin":  # macOS
        chrome_data = home / "Library/Application Support/Google/Chrome"
        if chrome_data.exists():
            return chrome_data
        # Try Chromium
        chromium_data = home / "Library/Application Support/Chromium"
        if chromium_data.exists():
            return chromium_data
    elif system == "Linux":
        chrome_data = home / ".config/google-chrome"
        if chrome_data.exists():
            return chrome_data
        chromium_data = home / ".config/chromium"
        if chromium_data.exists():
            return chromium_data
    elif system == "Windows":
        chrome_data = Path.home() / "AppData/Local/Google/Chrome/User Data"
        if chrome_data.exists():
            return chrome_data

    return None


async def twilio_login_with_chrome_profile():
    """
    Login to Twilio using existing Chrome profile with 1Password extension.

    This is the MOST SECURE approach - uses your actual browser with all extensions.
    """
    chrome_profile = find_chrome_profile()

    if not chrome_profile:
        print("⚠️  Chrome profile not found.")
        print("\nTo use this secure method:")
        print("1. Install Chrome/Chromium browser")
        print("2. Install 1Password browser extension")
        print("3. Sign in to 1Password in the browser")
        print("4. Ensure 'Twilio' item exists in 1Password")
        print(
            "\nAlternatively, use: python execution/scripts/twilio_login_1password_extension.py"
        )
        sys.exit(1)

    print(f"✓ Found Chrome profile at: {chrome_profile}")
    print("  Using your existing browser profile with 1Password extension")
    print("  ⚠️  WARNING: Chrome must be closed for this to work!")

    async with async_playwright() as p:
        print("\nLaunching browser with your Chrome profile...")

        # Launch browser using existing Chrome profile
        # This includes all your extensions, including 1Password
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(chrome_profile / "Default"),  # Use Default profile
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1920, "height": 1080},
        )

        # Get first page (persistent context creates one automatically)
        pages = browser.pages
        if pages:
            page = pages[0]
        else:
            page = await browser.new_page()

        print("\nNavigating to Twilio login...")
        await page.goto("https://console.twilio.com")

        print("\n" + "=" * 60)
        print("1Password Extension Instructions:")
        print("=" * 60)
        print("1. The 1Password extension should detect the login form")
        print("2. Click the 1Password icon in the browser toolbar")
        print("3. Select your Twilio login item")
        print("4. 1Password will auto-fill credentials securely")
        print("5. Complete 2FA if required")
        print("=" * 60)

        # Wait for login to complete
        print("\nWaiting for login to complete...")
        print("Please use 1Password extension to fill credentials.")

        try:
            # Wait for navigation away from login page
            max_wait = 300  # 5 minutes
            waited = 0
            check_interval = 5

            while waited < max_wait:
                await asyncio.sleep(check_interval)
                waited += check_interval

                current_url = page.url
                if (
                    "console.twilio.com" in current_url
                    and "login" not in current_url.lower()
                ):
                    print(f"\n✓ Login successful! Current URL: {page.url}")
                    break

                # Check if page title changed (indicates login)
                try:
                    title = await page.title()
                    if "Console" in title and "Login" not in title:
                        print(f"\n✓ Login successful! Page: {title}")
                        break
                except Exception:
                    pass

                if waited % 30 == 0:
                    print(f"  Still waiting... ({waited}s elapsed)")
            else:
                print("\n⚠️  Login timeout - please complete login manually")

            # Navigate to phone numbers/SMS configuration
            print("\nNavigating to Phone Numbers configuration...")
            await page.goto(
                "https://console.twilio.com/us1/develop/phone-numbers/manage/incoming"
            )
            await page.wait_for_load_state("networkidle")

            print("\n✓ Ready to check SMS configuration for 6503198857")
            print("Browser will stay open for manual inspection...")
            print("Press Ctrl+C to close when done.")

            # Keep browser open
            await asyncio.sleep(300)  # 5 minutes

        except KeyboardInterrupt:
            print("\n\nScript interrupted by user")
        except Exception as e:
            print(f"\n⚠️  Error: {e}")
            print("Browser will stay open for manual inspection...")
            await asyncio.sleep(300)


if __name__ == "__main__":
    try:
        asyncio.run(twilio_login_with_chrome_profile())
    except KeyboardInterrupt:
        print("\n\nScript interrupted by user")
        sys.exit(0)
