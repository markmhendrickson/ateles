#!/usr/bin/env python3
"""
Automatically add domain to Cloudflare using browser automation.

This script uses Playwright to navigate the Cloudflare dashboard
and add the domain, handling authentication via 1Password extension.
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).parent.parent.parent


async def add_domain_to_cloudflare():
    """Add domain to Cloudflare via browser automation."""
    domain = "neotoma.io"

    async with async_playwright() as p:
        # Launch browser with 1Password extension
        # Find 1Password extension path (common locations)
        extension_paths = [
            Path.home()
            / "Library/Application Support/Google/Chrome/Default/Extensions/fhbjgbiflinjbdggehcddcbncdddomop",  # 1Password Chrome
            Path.home()
            / "Library/Application Support/1Password/1Password.app/Contents/MacOS/1Password",  # 1Password app
        ]

        # Try to find 1Password extension
        extension_path = None
        for path in extension_paths:
            if path.exists():
                # For Chrome extensions, find the versioned subdirectory
                if "Extensions" in str(path):
                    subdirs = [d for d in path.iterdir() if d.is_dir()]
                    if subdirs:
                        extension_path = str(subdirs[0])
                        break

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]

        if extension_path:
            browser_args.extend(
                [
                    f"--disable-extensions-except={extension_path}",
                    f"--load-extension={extension_path}",
                ]
            )

        browser = await p.chromium.launch(
            headless=False,
            args=browser_args,
        )

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
        )

        page = await context.new_page()

        try:
            print("Navigating to Cloudflare dashboard...")
            await page.goto(
                "https://dash.cloudflare.com/", wait_until="domcontentloaded"
            )

            # Wait for page to load
            await page.wait_for_timeout(2000)

            # Check if we need to log in
            sign_in_count = await page.locator("text=Sign in").count()
            if "login" in page.url.lower() or sign_in_count > 0:
                print("Already logged in or on login page")
                # If there's a "Sign in with Google" option, we can use that
                # Otherwise, user will need to authenticate manually
                await page.wait_for_timeout(
                    3000
                )  # Give time for auto-login or manual login

            # Look for "Add a Site" button
            print("Looking for 'Add a Site' button...")

            # Try multiple selectors for the "Add a Site" button
            add_site_selectors = [
                "text=Add a Site",
                "text=Add site",
                '[aria-label*="Add"]',
                'a[href*="add-site"]',
            ]

            add_site_button = None
            for selector in add_site_selectors:
                try:
                    add_site_button = page.locator(selector).first
                    if await add_site_button.count() > 0:
                        print(f"Found 'Add a Site' button with selector: {selector}")
                        break
                except:
                    continue

            if not add_site_button or await add_site_button.count() == 0:
                # Try navigating directly to add site page
                print("Button not found, navigating directly to add site page...")
                await page.goto(
                    "https://dash.cloudflare.com/add-site",
                    wait_until="domcontentloaded",
                )
                await page.wait_for_timeout(2000)
            else:
                await add_site_button.click()
                await page.wait_for_timeout(2000)

            # Enter domain name
            print(f"Entering domain: {domain}")
            domain_input_selectors = [
                'input[name="zone"]',
                'input[type="text"]',
                'input[placeholder*="example.com"]',
                'input[placeholder*="domain"]',
            ]

            domain_input = None
            for selector in domain_input_selectors:
                try:
                    domain_input = page.locator(selector).first
                    if await domain_input.count() > 0:
                        print(f"Found domain input with selector: {selector}")
                        break
                except:
                    continue

            if domain_input and await domain_input.count() > 0:
                await domain_input.fill(domain)
                await page.wait_for_timeout(1000)

                # Look for "Continue" or "Add Site" button
                continue_selectors = [
                    'button:has-text("Continue")',
                    'button:has-text("Add site")',
                    'button[type="submit"]',
                ]

                for selector in continue_selectors:
                    try:
                        continue_button = page.locator(selector).first
                        if await continue_button.count() > 0:
                            print("Clicking Continue...")
                            await continue_button.click()
                            await page.wait_for_timeout(3000)
                            break
                    except:
                        continue

                # Wait for plan selection or DNS setup
                print("Waiting for plan selection or DNS setup...")
                await page.wait_for_timeout(3000)

                # Look for "Free" plan button
                free_plan_selectors = [
                    'button:has-text("Free")',
                    "text=Free plan",
                    '[data-testid*="free"]',
                ]

                for selector in free_plan_selectors:
                    try:
                        free_button = page.locator(selector).first
                        if await free_button.count() > 0:
                            print("Selecting Free plan...")
                            await free_button.click()
                            await page.wait_for_timeout(2000)
                            break
                    except:
                        continue

                # Look for "Continue" or "Confirm" after plan selection
                confirm_selectors = [
                    'button:has-text("Continue")',
                    'button:has-text("Confirm")',
                    'button:has-text("Add site")',
                ]

                for selector in confirm_selectors:
                    try:
                        confirm_button = page.locator(selector).first
                        if await confirm_button.count() > 0:
                            print("Confirming...")
                            await confirm_button.click()
                            await page.wait_for_timeout(5000)
                            break
                    except:
                        continue

                print("✓ Domain addition process initiated")
                print("Waiting for setup to complete...")
                await page.wait_for_timeout(10000)

                # Check if we're on the domain overview page (success)
                if domain in page.url or "overview" in page.url.lower():
                    print(f"✓ Successfully added {domain} to Cloudflare!")
                    print(f"Current URL: {page.url}")
                else:
                    print(f"Domain setup in progress. Current URL: {page.url}")
                    print("Please check the browser to complete any remaining steps.")

            else:
                print("Could not find domain input field")
                print("Please manually add the domain in the browser window")
                print("Waiting 60 seconds for manual completion...")
                await page.wait_for_timeout(60000)

        except Exception as e:
            print(f"Error during automation: {e}")
            print("Browser window will remain open for manual completion")
            await page.wait_for_timeout(30000)

        finally:
            print("\nBrowser automation complete.")
            print("You can close the browser window when done.")
            # Don't close browser automatically - let user verify
            # await browser.close()


if __name__ == "__main__":
    print("Starting Cloudflare domain addition automation...")
    print("This will open a browser window to add neotoma.io to Cloudflare.")
    print()
    asyncio.run(add_domain_to_cloudflare())
