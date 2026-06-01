#!/usr/bin/env python3
"""
Quick script to fill in the Cloudflare DNS target field for tunnel.

This script is designed to work when the DNS form is already open.
"""

import asyncio

from playwright.async_api import async_playwright

TUNNEL_TARGET = "64cffaf9-7704-4d12-9b35-436c31be34f6.cfargotunnel.com"


async def fill_dns_target():
    """Fill in the target field in the open Cloudflare DNS form."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        # Get all pages (in case form is in existing tab)
        pages = context.pages
        if len(pages) > 1:
            # Use the most recent page (likely the form)
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
                        # Check if it's visible
                        is_visible = await target_input.is_visible()
                        if is_visible:
                            print(f"Found target field with selector: {selector}")
                            # Click to focus
                            await target_input.click()
                            await page.wait_for_timeout(300)
                            # Clear and fill
                            await target_input.fill("")
                            await page.wait_for_timeout(200)
                            await target_input.fill(TUNNEL_TARGET)
                            await page.wait_for_timeout(500)

                            # Verify
                            value = await target_input.input_value()
                            if TUNNEL_TARGET in value:
                                print(f"✓ Target field filled: {TUNNEL_TARGET}")
                                target_filled = True

                                # Look for Save button and click it
                                await page.wait_for_timeout(1000)
                                save_selectors = [
                                    'button:has-text("Save")',
                                    'button[type="submit"]',
                                    'button:has-text("Continue")',
                                ]

                                for save_selector in save_selectors:
                                    try:
                                        save_button = page.locator(save_selector).first
                                        if await save_button.count() > 0:
                                            is_visible = await save_button.is_visible()
                                            if is_visible:
                                                print("Clicking Save button...")
                                                await save_button.click()
                                                await page.wait_for_timeout(3000)
                                                print("✓ DNS record saved!")
                                                break
                                    except:
                                        continue

                                break
                except Exception:
                    continue

            if not target_filled:
                print("⚠ Could not find target field automatically")
                print(f"Please manually enter: {TUNNEL_TARGET}")
                print("Waiting 60 seconds...")
                await page.wait_for_timeout(60000)
            else:
                print("\n✓ DNS record configuration complete!")
                print("Waiting 10 seconds for you to verify...")
                await page.wait_for_timeout(10000)

        except Exception as e:
            print(f"Error: {e}")
            print("Browser will remain open")
            await page.wait_for_timeout(30000)

        finally:
            print("You can close the browser when done.")


if __name__ == "__main__":
    asyncio.run(fill_dns_target())
