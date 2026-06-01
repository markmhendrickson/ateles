#!/usr/bin/env python3
"""
Extract Twilio credentials using browser automation.
Opens Twilio Console, navigates to Account Info, and extracts credentials.
"""

import asyncio
import sys
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("ERROR: Playwright not installed.")
    print("Install with: pip install playwright && playwright install chromium")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent.parent


async def extract_credentials():
    """Extract Twilio credentials from browser."""
    async with async_playwright() as p:
        print("Launching browser...")
        browser = await p.chromium.launch(
            headless=False
        )  # Show browser so user can login
        context = await browser.new_context()
        page = await context.new_page()

        print("Navigating to Twilio Console...")
        await page.goto(
            "https://console.twilio.com/us1/develop/account/settings/general"
        )

        print("\n" + "=" * 60)
        print("Please log in to Twilio Console if prompted")
        print("Waiting for page to load...")
        print("=" * 60)

        # Wait for page to load (user may need to login)
        await page.wait_for_load_state("networkidle", timeout=60000)

        # Try to extract Account SID
        print("\nExtracting Account SID...")
        account_sid = None

        # Method 1: Look for Account SID in page content
        try:
            # Wait for account info section
            await page.wait_for_selector("text=/AC[a-zA-Z0-9]{32}/", timeout=10000)
            account_sid_elements = await page.query_selector_all(
                "text=/AC[a-zA-Z0-9]{32}/"
            )
            if account_sid_elements:
                account_sid = await account_sid_elements[0].inner_text()
                account_sid = account_sid.strip()
        except Exception as e:
            print(f"Could not find Account SID in page text: {e}")

        # Method 2: Try to find in page source
        if not account_sid:
            page_content = await page.content()
            import re

            sid_match = re.search(r"AC[a-zA-Z0-9]{32}", page_content)
            if sid_match:
                account_sid = sid_match.group(0)

        # Method 3: Try to extract from localStorage
        if not account_sid:
            try:
                await context.storage_state()
                # Check cookies/localStorage
                account_sid = await page.evaluate(
                    """
                    () => {
                        for (let i = 0; i < localStorage.length; i++) {
                            const key = localStorage.key(i);
                            if (key && key.includes('account') && key.includes('sid')) {
                                const value = localStorage.getItem(key);
                                if (value && value.startsWith('AC')) {
                                    return value;
                                }
                            }
                        }
                        return null;
                    }
                """
                )
            except Exception as e:
                print(f"Could not check localStorage: {e}")

        print("\n" + "=" * 60)
        print("Credentials Extraction Results")
        print("=" * 60)

        if account_sid:
            print(f"\n✓ Found Account SID: {account_sid}")
        else:
            print("\n⚠️  Account SID not found automatically")
            print("   Please copy it manually from the page")

        print("\n⚠️  Auth Token must be retrieved manually:")
        print("   1. On the Account Info page, click 'Show' next to Auth Token")
        print("   2. Copy the token")

        # Keep browser open for user to manually copy auth token
        print("\n" + "=" * 60)
        print("Browser will stay open for 60 seconds")
        print("Please copy the Auth Token from the page")
        print("=" * 60)

        await asyncio.sleep(60)

        # Try to get auth token if user clicked show
        auth_token = None
        try:
            # Look for revealed auth token
            auth_elements = await page.query_selector_all(
                'input[type="text"], code, pre'
            )
            for elem in auth_elements:
                text = await elem.inner_text()
                if text and len(text) > 20 and not text.startswith("AC"):
                    # Might be auth token
                    auth_token = text.strip()
                    break
        except Exception:
            pass

        await browser.close()

        # Save to .env if we got both
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

            print("\nNow you can run:")
            print("  python execution/scripts/debug_twilio_sms.py")

        return account_sid, auth_token


if __name__ == "__main__":
    account_sid, auth_token = asyncio.run(extract_credentials())
