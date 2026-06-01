#!/usr/bin/env python3
"""
Verify and ensure DNS record exists in Cloudflare for tunnel routing.

Since the domain is in Cloudflare with Full DNS, we need to ensure
the CNAME record exists in Cloudflare's DNS (not just DNSimple).
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).parent.parent.parent


async def verify_dns_record():
    """Verify DNS record exists in Cloudflare dashboard."""
    domain = "neotoma.io"
    hostname = "dev.neotoma.io"
    tunnel_target = "64cffaf9-7704-4d12-9b35-436c31be34f6.cfargotunnel.com"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        try:
            print(f"Navigating to Cloudflare DNS for {domain}...")
            await page.goto(
                f"https://dash.cloudflare.com/?to=/:account/zones/{domain}/dns",
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(3000)

            # Look for DNS records table
            print("Checking for existing DNS records...")

            # Check if dev.neotoma.io record exists
            record_exists = False
            try:
                # Look for the record in the table
                record_rows = page.locator(f"text={hostname}")
                if await record_rows.count() > 0:
                    print(f"✓ Found DNS record for {hostname}")
                    record_exists = True
                else:
                    print(f"DNS record for {hostname} not found")
            except:
                pass

            # Check if we're on the add/edit record page (form is visible)
            form_visible = False
            try:
                # Check if target input is visible (means form is open)
                target_inputs = page.locator(
                    'input[placeholder*="www.example.com"], input[label*="Target"], input:has-text("Target")'
                )
                if await target_inputs.count() > 0:
                    form_visible = True
                    print("✓ DNS record form is already open")
            except:
                pass

            if not record_exists or form_visible:
                if form_visible:
                    print(f"Filling in existing form: {hostname} -> {tunnel_target}")
                else:
                    print(f"Adding DNS record: {hostname} -> {tunnel_target}")

                # Look for "Add record" button
                add_record_selectors = [
                    'button:has-text("Add record")',
                    'a:has-text("Add record")',
                    '[aria-label*="Add record"]',
                ]

                for selector in add_record_selectors:
                    try:
                        add_button = page.locator(selector).first
                        if await add_button.count() > 0:
                            await add_button.click()
                            await page.wait_for_timeout(2000)
                            break
                    except:
                        continue

                # Fill in CNAME record
                # Type dropdown - select CNAME
                type_selectors = [
                    'select[name="type"]',
                    'select[aria-label*="Type"]',
                    ".record-type-select",
                ]

                for selector in type_selectors:
                    try:
                        type_select = page.locator(selector).first
                        if await type_select.count() > 0:
                            await type_select.select_option("CNAME")
                            await page.wait_for_timeout(1000)
                            break
                    except:
                        continue

                # Name field - enter "dev"
                name_selectors = [
                    'input[name="name"]',
                    'input[placeholder*="name"]',
                    'input[aria-label*="Name"]',
                ]

                for selector in name_selectors:
                    try:
                        name_input = page.locator(selector).first
                        if await name_input.count() > 0:
                            await name_input.fill("dev")
                            await page.wait_for_timeout(500)
                            break
                    except:
                        continue

                # Target field - enter tunnel target
                # Wait a bit for the form to fully load
                await page.wait_for_timeout(1000)

                target_selectors = [
                    'input[name="target"]',
                    'input[name="content"]',
                    'input[placeholder*="target"]',
                    'input[placeholder*="www.example.com"]',
                    'input[aria-label*="Target"]',
                    'input[label*="Target"]',
                    'div:has-text("Target") + div input',
                    'div:has-text("Target (required)") + div input',
                ]

                target_filled = False
                for selector in target_selectors:
                    try:
                        target_input = page.locator(selector).first
                        count = await target_input.count()
                        if count > 0:
                            # Clear any existing value
                            await target_input.click()
                            await page.wait_for_timeout(200)
                            await target_input.fill("")
                            await page.wait_for_timeout(200)
                            await target_input.fill(tunnel_target)
                            await page.wait_for_timeout(500)
                            # Verify it was filled
                            value = await target_input.input_value()
                            if tunnel_target in value:
                                print(f"✓ Target field filled: {tunnel_target}")
                                target_filled = True
                                break
                    except Exception as e:
                        print(f"  Trying next selector (error: {e})")
                        continue

                if not target_filled:
                    print("⚠ Could not fill target field automatically")
                    print(f"  Please manually enter: {tunnel_target}")
                    await page.wait_for_timeout(5000)

                # Enable proxy (orange cloud) - look for proxy toggle
                proxy_selectors = [
                    'input[type="checkbox"][name*="proxy"]',
                    'input[type="checkbox"][aria-label*="proxy"]',
                    ".proxy-toggle",
                ]

                for selector in proxy_selectors:
                    try:
                        proxy_toggle = page.locator(selector).first
                        if await proxy_toggle.count() > 0:
                            checked = await proxy_toggle.is_checked()
                            if not checked:
                                await proxy_toggle.check()
                                await page.wait_for_timeout(500)
                            break
                    except:
                        continue

                # Save button
                save_selectors = [
                    'button:has-text("Save")',
                    'button[type="submit"]',
                    'button:has-text("Add")',
                ]

                for selector in save_selectors:
                    try:
                        save_button = page.locator(selector).first
                        if await save_button.count() > 0:
                            await save_button.click()
                            await page.wait_for_timeout(3000)
                            print("✓ DNS record added")
                            break
                    except:
                        continue

            print("\nDNS verification complete.")
            print("Waiting 30 seconds for you to verify in the browser...")
            await page.wait_for_timeout(30000)

        except Exception as e:
            print(f"Error: {e}")
            print("Browser will remain open for manual verification")
            await page.wait_for_timeout(30000)

        finally:
            print("You can close the browser when done.")
            # Don't auto-close - let user verify


if __name__ == "__main__":
    asyncio.run(verify_dns_record())
