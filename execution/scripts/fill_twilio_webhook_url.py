#!/usr/bin/env python3
"""
Fill Twilio webhook URL field using browser automation.
Opens Twilio Console and fills in the complete webhook URL.
"""

import asyncio
import sys
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("ERROR: Playwright not installed.")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent.parent
WEBHOOK_URL = "https://summary-nevada-knit-pig.trycloudflare.com/webhook/twilio/sms"


def find_1password_extension():
    """Find 1Password browser extension path."""
    import platform

    home = Path.home()
    system = platform.system()

    if system == "Darwin":  # macOS
        search_paths = [
            home / "Library/Application Support/Google/Chrome/Default/Extensions",
            home
            / "Library/Application Support/BraveSoftware/Brave-Browser/Default/Extensions",
        ]
        extension_id = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"
        for base_path in search_paths:
            ext_path = base_path / extension_id
            if ext_path.exists():
                versions = sorted(ext_path.iterdir(), reverse=True)
                if versions:
                    return versions[0]
    return None


async def fill_webhook_url():
    """Fill webhook URL in Twilio Console."""
    async with async_playwright() as p:
        print("Launching browser...")

        # Try to use 1Password extension
        extension_path = find_1password_extension()
        if extension_path:
            browser = await p.chromium.launch(
                headless=False,
                args=[
                    f"--disable-extensions-except={extension_path}",
                    f"--load-extension={extension_path}",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
        else:
            browser = await p.chromium.launch(headless=False)

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        # Navigate to current page (user should already be on the config page)
        print("Waiting for webhook URL field...")
        await asyncio.sleep(2)

        # Try to find and fill the URL input field
        # Look for input fields that might contain the partial URL
        try:
            # Find input field with the partial URL
            inputs = await page.query_selector_all(
                'input[type="text"], input[type="url"]'
            )
            for inp in inputs:
                try:
                    value = await inp.input_value()
                    if "trycloudflare.com" in value or "webhook/twilio" in value:
                        # Found the webhook URL field
                        await inp.fill(WEBHOOK_URL)
                        print(f"✓ Filled webhook URL: {WEBHOOK_URL}")
                        await asyncio.sleep(1)

                        # Try to find and click Save button
                        try:
                            save_button = await page.query_selector(
                                'button:has-text("Save"), button[type="submit"]'
                            )
                            if save_button:
                                await save_button.click()
                                print("✓ Clicked Save button")
                                await asyncio.sleep(2)
                        except Exception:
                            print(
                                "⚠️  Please click 'Save configuration' button manually"
                            )

                        break
                except Exception:
                    continue
        except Exception as e:
            print(f"⚠️  Could not fill URL automatically: {e}")
            print(f"\nPlease manually paste this URL: {WEBHOOK_URL}")

        print("\nBrowser will stay open for 10 seconds for verification...")
        await asyncio.sleep(10)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(fill_webhook_url())
