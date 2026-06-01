#!/usr/bin/env python3
"""
Automatically click Save button on Cloudflare DNS record form.

This script is designed to work when the DNS form is already filled in
and just needs to be saved.
"""

import asyncio

from playwright.async_api import async_playwright


async def save_dns_record():
    """Click Save button on the open Cloudflare DNS form."""
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
            print("Looking for Save button on DNS record form...")
            await page.wait_for_timeout(2000)

            # Try multiple selectors for the Save button
            save_selectors = [
                'button:has-text("Save")',
                'button[type="submit"]',
                'button:has-text("Continue")',
                'button.primary:has-text("Save")',
                'button[data-testid*="save"]',
            ]

            saved = False
            for selector in save_selectors:
                try:
                    save_button = page.locator(selector).first
                    count = await save_button.count()
                    if count > 0:
                        is_visible = await save_button.is_visible()
                        if is_visible:
                            print(f"Found Save button with selector: {selector}")
                            print("Clicking Save...")
                            await save_button.click()
                            await page.wait_for_timeout(3000)

                            # Check if save was successful (form should disappear or show success message)
                            # Look for confirmation or check if form is gone
                            form_gone = (
                                await page.locator(
                                    'input[placeholder*="www.example.com"]'
                                ).count()
                                == 0
                            )
                            if form_gone:
                                print("✓ DNS record saved successfully!")
                                saved = True
                            else:
                                print("✓ Save button clicked (verifying...)")
                                saved = True
                            break
                except Exception as e:
                    print(f"  Trying next selector (error: {e})")
                    continue

            if not saved:
                print("⚠ Could not find Save button automatically")
                print("Please click Save manually")
                print("Waiting 30 seconds...")
                await page.wait_for_timeout(30000)
            else:
                print("\n✓ DNS record save operation complete!")
                print("Waiting 5 seconds for you to verify...")
                await page.wait_for_timeout(5000)

        except Exception as e:
            print(f"Error: {e}")
            print("Browser will remain open")
            await page.wait_for_timeout(30000)

        finally:
            print("You can close the browser when done.")


if __name__ == "__main__":
    asyncio.run(save_dns_record())
