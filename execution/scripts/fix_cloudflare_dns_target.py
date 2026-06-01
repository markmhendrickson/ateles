#!/usr/bin/env python3
"""
Fix the Cloudflare DNS target field with the correct tunnel address.
"""

import asyncio

from playwright.async_api import async_playwright

CORRECT_TARGET = "64cffaf9-7704-4d12-9b35-436c31be34f6.cfargotunnel.com"


async def fix_dns_target():
    """Fix the DNS target field in the open Cloudflare form."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()

        # Try to connect to existing browser or create new page
        try:
            # Connect to existing browser if possible
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            context = (
                browser.contexts[0] if browser.contexts else await browser.new_context()
            )
        except:
            # Create new browser if can't connect
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()

        # Get all pages
        pages = context.pages
        if not pages:
            page = await context.new_page()
            await page.goto(
                "https://dash.cloudflare.com/", wait_until="domcontentloaded"
            )
        else:
            # Use the most recent page (likely the DNS form)
            page = pages[-1]

        try:
            print("Looking for DNS target input field...")
            await page.wait_for_timeout(2000)

            # Try multiple selectors for the target field
            target_selectors = [
                'input[placeholder*="www.example.com"]',
                'input[placeholder*="E.g. www.example.com"]',
                'input[label*="Target"]',
                'div:has-text("Target (required)") input',
                'div:has-text("Target") input',
                'input[name="target"]',
                'input[name="content"]',
            ]

            target_filled = False
            for selector in target_selectors:
                try:
                    target_input = page.locator(selector).first
                    count = await target_input.count()
                    if count > 0:
                        is_visible = await target_input.is_visible()
                        if is_visible:
                            current_value = await target_input.input_value()
                            print(f"Current target value: {current_value}")

                            if CORRECT_TARGET not in current_value:
                                print("Fixing target field...")
                                # Click to focus and select all
                                await target_input.click()
                                await page.wait_for_timeout(300)
                                await page.keyboard.press("Meta+a")  # Cmd+A on Mac
                                await page.wait_for_timeout(200)
                                # Type the correct value
                                await target_input.fill(CORRECT_TARGET)
                                await page.wait_for_timeout(500)

                                # Verify
                                new_value = await target_input.input_value()
                                if CORRECT_TARGET in new_value:
                                    print(f"✓ Target field fixed: {CORRECT_TARGET}")
                                    target_filled = True

                                    # Click Save button
                                    await page.wait_for_timeout(1000)
                                    save_selectors = [
                                        'button:has-text("Save")',
                                        'button[type="submit"]',
                                        'button:has-text("Continue")',
                                    ]

                                    for save_selector in save_selectors:
                                        try:
                                            save_button = page.locator(
                                                save_selector
                                            ).first
                                            if await save_button.count() > 0:
                                                is_visible = (
                                                    await save_button.is_visible()
                                                )
                                                if is_visible:
                                                    print("Clicking Save button...")
                                                    await save_button.click()
                                                    await page.wait_for_timeout(3000)
                                                    print(
                                                        "✓ DNS record saved with correct target!"
                                                    )
                                                    break
                                        except:
                                            continue

                                    break
                            else:
                                print("✓ Target field already has correct value")
                                target_filled = True
                                break
                except Exception as e:
                    print(f"  Trying next selector (error: {e})")
                    continue

            if not target_filled:
                print("⚠ Could not find or fix target field automatically")
                print(f"Please manually update to: {CORRECT_TARGET}")
                print("Waiting 60 seconds...")
                await page.wait_for_timeout(60000)
            else:
                print("\n✓ DNS record configuration complete!")
                print("Waiting 10 seconds for you to verify...")
                await page.wait_for_timeout(10000)

        except Exception as e:
            print(f"Error: {e}")
            import traceback

            traceback.print_exc()
            print("Browser will remain open")
            await page.wait_for_timeout(30000)

        finally:
            print("You can close the browser when done.")


if __name__ == "__main__":
    asyncio.run(fix_dns_target())
