#!/usr/bin/env python3
"""
Update Twilio phone number webhook URL using browser automation.

Reads tunnel URL from logs and updates Twilio Console webhook configuration.
"""

import asyncio
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
import sys

sys.path.insert(0, str(PROJECT_ROOT))
from scripts.config import get_data_dir

DATA_DIR = get_data_dir()
TUNNEL_LOG = DATA_DIR / "logs" / "cloudflare_twilio_sms_tunnel.log"


def get_tunnel_url() -> str | None:
    """Extract tunnel URL from logs."""
    # Check both log and error log files
    log_files = [
        TUNNEL_LOG,
        DATA_DIR / "logs" / "cloudflare_twilio_sms_tunnel.error.log",
    ]

    for log_file in log_files:
        if not log_file.exists():
            continue

        try:
            with open(log_file) as f:
                content = f.read()
                # Look for Cloudflare tunnel URL
                matches = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", content)
                if matches:
                    return matches[-1]  # Get most recent
        except Exception as e:
            print(f"Error reading {log_file}: {e}")

    return None


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
    else:
        return None

    extension_id = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"
    for base_path in search_paths:
        ext_path = base_path / extension_id
        if ext_path.exists():
            versions = sorted(ext_path.iterdir(), reverse=True)
            if versions:
                return versions[0]
    return None


async def update_twilio_webhook(webhook_url: str):
    """Update Twilio phone number webhook URL."""
    async with async_playwright() as p:
        print("Launching browser...")

        # Try to use 1Password extension
        extension_path = find_1password_extension()
        if extension_path:
            print(f"Using 1Password extension: {extension_path}")
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

        print("Navigating to Twilio Console...")
        await page.goto(
            "https://console.twilio.com/us1/develop/phone-numbers/manage/incoming",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        print("\n" + "=" * 60)
        print("Please log in to Twilio if prompted")
        print("Waiting for page to load...")
        print("=" * 60)

        # Wait for page to load
        await asyncio.sleep(5)

        # Check if we need to login
        current_url = page.url
        if "login" in current_url.lower():
            print("\n⚠️  Please log in to Twilio in the browser")
            print("Waiting 60 seconds for login...")
            await asyncio.sleep(60)

        # Find phone number +16503198857
        print("\nLooking for phone number +16503198857...")
        phone_found = False
        try:
            # Try multiple selectors for phone number
            selectors = [
                "text=/650.*319.*8857/",
                "text=/16503198857/",
                'a:has-text("650")',
                'a:has-text("319")',
            ]

            for selector in selectors:
                try:
                    await page.wait_for_selector(selector, timeout=5000)
                    await page.click(selector)
                    phone_found = True
                    print("✓ Found and clicked phone number")
                    await asyncio.sleep(3)
                    break
                except Exception:
                    continue
        except Exception as e:
            print(f"⚠️  Could not find phone number automatically: {e}")

        if not phone_found:
            print("⚠️  Please click on phone number +16503198857 in the browser")
            await asyncio.sleep(10)

        # Wait for phone number configuration page to load
        await asyncio.sleep(3)

        # Try to find and update webhook URL field
        print("\nLooking for 'A MESSAGE COMES IN' webhook URL field...")
        webhook_updated = False

        try:
            # Try to find the webhook URL input field
            # Common selectors for Twilio webhook fields
            url_selectors = [
                'input[name*="sms"]',
                'input[name*="webhook"]',
                'input[placeholder*="webhook"]',
                'input[type="url"]',
                'input[type="text"]',
            ]

            for selector in url_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    for elem in elements:
                        # Check if it's near "A MESSAGE COMES IN" text
                        try:
                            value = await elem.input_value()
                            if (
                                "sms-to-email" in value
                                or "twilio" in value.lower()
                                or not value
                            ):
                                # This might be the webhook URL field
                                await elem.fill(webhook_url)
                                print("✓ Updated webhook URL field")
                                webhook_updated = True
                                await asyncio.sleep(1)
                                break
                        except Exception:
                            continue
                    if webhook_updated:
                        break
                except Exception:
                    continue

            # Try to find and click Save button
            if webhook_updated:
                try:
                    save_selectors = [
                        'button:has-text("Save")',
                        'button[type="submit"]',
                        'button:has-text("Update")',
                    ]
                    for selector in save_selectors:
                        try:
                            await page.click(selector, timeout=3000)
                            print("✓ Clicked Save button")
                            await asyncio.sleep(2)
                            break
                        except Exception:
                            continue
                except Exception:
                    print("⚠️  Could not find Save button automatically")
        except Exception as e:
            print(f"⚠️  Could not update webhook URL automatically: {e}")

        if not webhook_updated:
            print("\n⚠️  Please manually update the webhook URL in the browser:")
            print("  1. Find 'A MESSAGE COMES IN' field")
            print(f"  2. Update URL to: {webhook_url}")
            print("  3. Ensure HTTP Method is set to POST")
            print("  4. Click Save")

        print("\nBrowser will stay open for 30 seconds for verification...")
        await asyncio.sleep(30)

        await browser.close()
        print("\n✓ Browser closed")


def main():
    """Main function."""
    print("=" * 60)
    print("Update Twilio Webhook URL")
    print("=" * 60)

    # Get tunnel URL
    tunnel_url = get_tunnel_url()

    if not tunnel_url:
        print("\n⚠️  Tunnel URL not found in logs")
        print("Waiting for tunnel to establish...")
        print("Checking logs...")

        # Wait a bit and check again
        import time

        for i in range(6):
            time.sleep(5)
            tunnel_url = get_tunnel_url()
            if tunnel_url:
                break
            print(f"  Still waiting... ({i + 1}/6)")

    if not tunnel_url:
        print("\n❌ Could not find tunnel URL")
        print("\nOptions:")
        print("  1. Check tunnel logs manually:")
        print(f"     tail -f {TUNNEL_LOG}")
        print("  2. Get URL from logs and update Twilio manually")
        print("  3. Run this script again once tunnel is established")
        return 1

    webhook_url = f"{tunnel_url}/webhook/twilio/sms"
    print(f"\n✓ Found tunnel URL: {tunnel_url}")
    print(f"✓ Webhook URL: {webhook_url}")

    # Update Twilio
    print("\nOpening browser to update Twilio webhook URL...")
    asyncio.run(update_twilio_webhook(webhook_url))

    return 0


if __name__ == "__main__":
    sys.exit(main())
