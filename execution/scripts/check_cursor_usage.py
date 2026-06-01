#!/usr/bin/env python3
"""
Check Cursor IDE usage and quota via browser automation.

Uses Playwright to navigate to Cursor settings and extract usage information.
Follows standard browser automation pattern from reference/agent-context.md.

Usage:
    python scripts/check_cursor_usage.py

First run will open browser for login. Subsequent runs reuse saved auth state.
"""

import asyncio
import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("ERROR: Playwright not installed.")
    print("Install with: pip install playwright && playwright install chromium")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent
import sys

sys.path.insert(0, str(PROJECT_ROOT))
from scripts.config import DATA_DIR

AUTH_STATE_DIR = PROJECT_ROOT / "playwright" / ".auth"
AUTH_STATE_DIR.mkdir(parents=True, exist_ok=True)
AUTH_STATE_PATH = AUTH_STATE_DIR / "cursor_auth_state.json"

# Usage data directory (saved to repo)
USAGE_DATA_DIR = DATA_DIR / "usage"
USAGE_DATA_DIR.mkdir(parents=True, exist_ok=True)


async def check_cursor_usage():
    """Check Cursor usage and quota from account settings."""
    async with async_playwright() as p:
        print("Launching browser...")

        # Always use non-headless for Google sign-in compatibility
        # Google blocks automated/headless browsers
        headless = False

        # Launch with stealth settings to avoid detection
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ]

        # On macOS, --start-minimized doesn't work, so we'll minimize after launch
        # On Linux/Windows, --start-minimized should work
        if platform.system() != "Darwin":  # Not macOS
            launch_args.append("--start-minimized")
        else:
            # On macOS, position window off-screen initially
            launch_args.append("--window-position=-10000,-10000")

        browser = await p.chromium.launch(headless=headless, args=launch_args)

        # Create context with realistic browser fingerprint
        context_options = {
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "locale": "en-US",
            "timezone_id": "America/Los_Angeles",
        }

        # Load saved auth state if available
        if AUTH_STATE_PATH.exists():
            print("Loading saved authentication state...")
            print(f"  Using saved auth from: {AUTH_STATE_PATH}")
            try:
                context_options["storage_state"] = str(AUTH_STATE_PATH)
                context = await browser.new_context(**context_options)
                print("  ✓ Auth state loaded - you should be automatically signed in")
            except Exception as e:
                print(f"  ⚠️  Could not load auth state: {e}")
                print("  Creating new context - you'll need to sign in again")
                context = await browser.new_context(**context_options)
        else:
            print("No saved auth state found. Browser will open for login.")
            print(f"  After first sign-in, auth will be saved to: {AUTH_STATE_PATH}")
            context = await browser.new_context(**context_options)

        # Remove webdriver property to avoid detection
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """
        )

        page = await context.new_page()

        # Minimize window on macOS using AppleScript
        if platform.system() == "Darwin":  # macOS

            async def minimize_window():
                """Minimize browser window on macOS."""
                await asyncio.sleep(1)  # Wait for window to appear
                try:
                    # Try multiple process names and methods
                    apple_scripts = [
                        # Method 1: Minimize Chromium windows
                        'tell application "System Events" to tell process "Chromium" to set miniaturized of every window to true',
                        # Method 2: Minimize Google Chrome windows
                        'tell application "System Events" to tell process "Google Chrome" to set miniaturized of every window to true',
                        # Method 3: Find and minimize any Chrome/Chromium window
                        'tell application "System Events" to repeat with proc in (every process whose name contains "Chrome" or name contains "Chromium") do set miniaturized of every window of proc to true',
                    ]

                    for script in apple_scripts:
                        result = subprocess.run(
                            ["osascript", "-e", script],
                            check=False,
                            capture_output=True,
                            timeout=2,
                        )
                        if result.returncode == 0:
                            print("  ✓ Browser window minimized")
                            return

                    print(
                        "  ⚠️  Could not minimize window (may already be minimized or off-screen)"
                    )
                except Exception as e:
                    print(f"  ⚠️  Could not minimize window: {e}")
                    print("  (Window positioned off-screen)")

            # Run minimization in background
            asyncio.create_task(minimize_window())

        # Navigate to Cursor dashboard (where usage is displayed)
        print("\nNavigating to Cursor dashboard...")
        try:
            await page.goto(
                "https://cursor.com/dashboard",
                wait_until="domcontentloaded",  # More lenient than networkidle
                timeout=60000,
            )
            # Wait for dashboard to finish loading (it shows "Loading Dashboard..." initially)
            print("Waiting for dashboard to load...")
            try:
                # Wait for "Loading Dashboard..." text to disappear
                await page.wait_for_function(
                    "() => !document.body.innerText.includes('Loading Dashboard...')",
                    timeout=30000,
                )
            except Exception:
                # If that doesn't work, just wait a bit
                await asyncio.sleep(3)
        except Exception as e:
            print(f"Error navigating to dashboard: {e}")
            print("Trying alternative URLs...")
            # Fallback to settings page
            try:
                await page.goto(
                    "https://cursor.com/settings",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception as e2:
                print(f"Error navigating to settings: {e2}")
                try:
                    await page.goto(
                        "https://cursor.com",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                except Exception as e3:
                    print(f"Error navigating to cursor.com: {e3}")
                    print("Continuing anyway - page may still be loading...")

        # Wait for page to load and check if login is required
        print("Waiting for page to load...")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass  # Continue anyway if page is slow

        # Check if we're on a login page or need authentication
        page_url = page.url
        page_content = await page.content()

        # Check for Google sign-in rejection (common with automated browsers)
        if "accounts.google.com" in page_url and "rejected" in page_url:
            print("\n" + "=" * 60)
            print("⚠️  Google sign-in blocked automated browser")
            print("=" * 60)
            print("\nGoogle is blocking the automated browser for security.")
            print("Please complete sign-in manually in the browser window.")
            print("\nSteps:")
            print("1. Click 'Try again' in the browser")
            print("2. Complete Google sign-in manually")
            print("3. Navigate to Cursor settings if needed")
            print("4. The script will check every 10 seconds (up to 5 minutes)")
            print("\nWaiting for manual sign-in completion...")
            print("=" * 60)

            # Poll every 10 seconds for sign-in completion (up to 5 minutes)
            max_attempts = 30  # 30 attempts * 10 seconds = 5 minutes
            for attempt in range(max_attempts):
                await asyncio.sleep(10)
                current_url = page.url
                print(f"Checking sign-in status... ({attempt + 1}/{max_attempts})")

                # Check if we've moved away from Google rejection page
                if (
                    "accounts.google.com" not in current_url
                    or "rejected" not in current_url
                ):
                    print("✓ Sign-in completed, continuing...")
                    # Try to wait for load, but don't fail if it times out
                    try:
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=10000
                        )
                    except Exception:
                        pass  # Continue anyway if page is slow
                    break
            else:
                print("⚠️  Timeout: Sign-in not completed within 5 minutes")
                print("Continuing anyway - you can complete sign-in manually...")
                await asyncio.sleep(5)

        if "login" in page_url.lower() or "sign in" in page_content.lower():
            print("\n" + "=" * 60)
            print("Please log in to Cursor in the browser window")
            print("The script will check every 10 seconds (up to 5 minutes)")
            print("=" * 60)

            # Poll every 10 seconds for login completion (up to 5 minutes)
            max_attempts = 30  # 30 attempts * 10 seconds = 5 minutes
            for attempt in range(max_attempts):
                await asyncio.sleep(10)
                current_url = page.url
                print(f"Checking login status... ({attempt + 1}/{max_attempts})")

                # Check if we've moved away from login page
                if (
                    "login" not in current_url.lower()
                    and "signin" not in current_url.lower()
                ):
                    print("✓ Login completed, continuing...")
                    # Try to wait for load, but don't fail if it times out
                    try:
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=10000
                        )
                    except Exception:
                        pass  # Continue anyway if page is slow
                    break
            else:
                print("⚠️  Timeout: Login not completed within 5 minutes")
                print("Continuing anyway - you can complete login manually...")
                await asyncio.sleep(5)

        # Navigate to account/subscription page if not already there
        current_url = page.url
        if (
            "settings" not in current_url.lower()
            and "account" not in current_url.lower()
        ):
            print("Navigating to account settings...")
            try:
                # Try common navigation patterns
                await page.goto("https://cursor.com/settings/account", timeout=30000)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass  # Continue even if page is slow
            except Exception:
                try:
                    # Try clicking account/settings links
                    await page.click(
                        "text=/account|settings|subscription/i", timeout=5000
                    )
                    try:
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=15000
                        )
                    except Exception:
                        pass  # Continue even if page is slow
                except Exception:
                    pass

        # Save auth state after successful navigation (if we logged in)
        # Always save if we're not on a login/rejection page
        current_url_lower = page.url.lower()
        if (
            "login" not in current_url_lower
            and "signin" not in current_url_lower
            and "rejected" not in current_url_lower
            and "accounts.google.com" not in current_url_lower
        ):
            print("\nSaving authentication state for future runs...")
            try:
                await context.storage_state(path=str(AUTH_STATE_PATH))
                print(f"✓ Auth state saved to {AUTH_STATE_PATH}")
                print("  Future runs will reuse this authentication automatically")
            except Exception as e:
                print(f"⚠️  Warning: Could not save auth state: {e}")
                print("  You may need to sign in again on next run")

        # Extract usage information
        print("\n" + "=" * 60)
        print("Extracting usage information...")
        print("=" * 60)

        usage_data = {}

        # Method 1: Try to find usage/quota elements in page content
        try:
            # Look for common usage indicators
            page_text = await page.inner_text("body")

            # Look for patterns like "X / Y requests", "X% used", etc.
            import re

            # Pattern for "X / Y" format (common for usage/quota)
            usage_pattern = r"(\d+[\d,]*)\s*/\s*(\d+[\d,]*)"
            matches = re.findall(usage_pattern, page_text)
            if matches:
                # Filter for reasonable usage numbers (not dates, etc.)
                for match in matches:
                    num1, num2 = match
                    # Likely usage if both numbers are reasonable (not years like 2024)
                    if (
                        int(num1.replace(",", "")) < 1000000
                        and int(num2.replace(",", "")) < 1000000
                    ):
                        usage_data["usage_pattern"] = f"{num1} / {num2}"
                        break

            # Pattern for percentage with context
            percent_patterns = [
                r"(\d+)%\s*(?:used|remaining|of)",
                r"(?:used|usage)[:\s]*(\d+)%",
                r"(\d+)%\s*(?:complete|full)",
            ]
            for pattern in percent_patterns:
                percent_matches = re.findall(pattern, page_text, re.IGNORECASE)
                if percent_matches:
                    usage_data["percentage"] = percent_matches[0]
                    break

            # Look for request/usage numbers with context
            request_patterns = [
                r"(\d+[\d,]*)\s*(?:requests?|queries?|calls?)\s*(?:used|remaining|of|/)",
                r"(?:requests?|queries?|usage)[:\s]*(\d+[\d,]*)",
                r"(\d+[\d,]*)\s*/\s*(\d+[\d,]*)\s*(?:requests?|queries?|calls?)",
            ]
            for pattern in request_patterns:
                request_matches = re.findall(pattern, page_text, re.IGNORECASE)
                if request_matches:
                    usage_data["requests"] = (
                        request_matches[0]
                        if isinstance(request_matches[0], str)
                        else request_matches[0]
                    )
                    break

            # Look for subscription tier
            tier_patterns = [
                r"(Pro|Business|Ultra|Free)",
                r"subscription[:\s]+(\w+)",
                r"plan[:\s]+(\w+)",
                r"tier[:\s]+(\w+)",
            ]
            for pattern in tier_patterns:
                tier_match = re.search(pattern, page_text, re.IGNORECASE)
                if tier_match:
                    usage_data["tier"] = tier_match.group(1)
                    break

            # Look for billing cycle or reset date
            date_patterns = [
                r"(?:resets?|renews?|billing)[:\s]+([A-Z][a-z]+\s+\d{1,2})",
                r"([A-Z][a-z]+\s+\d{1,2})\s*(?:resets?|renews?)",
            ]
            for pattern in date_patterns:
                date_match = re.search(pattern, page_text, re.IGNORECASE)
                if date_match:
                    usage_data["reset_date"] = date_match.group(1)
                    break

        except Exception as e:
            print(f"Error extracting from page text: {e}")

        # Method 2: Try to extract from specific selectors and DOM elements
        try:
            # Look for usage/quota elements with more specific selectors
            usage_selectors = [
                '[data-testid*="usage"]',
                '[class*="usage"]',
                '[id*="usage"]',
                '[class*="quota"]',
                '[id*="quota"]',
                '[class*="request"]',
                '[class*="limit"]',
                '[class*="subscription"]',
                '[class*="plan"]',
                "text=/requests|usage|quota|limit/i",
                "progress",  # Progress bars often show usage
                '[role="progressbar"]',
            ]

            for selector in usage_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    if elements:
                        for elem in elements[:5]:  # Check first 5 matches
                            text = await elem.inner_text()
                            if text and len(text) < 200:  # Reasonable length
                                # Extract value attribute for progress bars
                                value = await elem.get_attribute("value")
                                max_value = await elem.get_attribute("max")
                                if value and max_value:
                                    usage_data[
                                        f"progress_{selector[:15]}"
                                    ] = f"{value}/{max_value}"
                                elif text.strip():
                                    usage_data[
                                        f"element_{selector[:15]}"
                                    ] = text.strip()
                except Exception:
                    continue

            # Try to find numbers that look like usage (e.g., "500 / 1000")
            try:
                all_text_elements = await page.query_selector_all("*")
                for elem in all_text_elements[:100]:  # Check first 100 elements
                    try:
                        text = await elem.inner_text()
                        if text and re.search(r"\d+\s*/\s*\d+", text):
                            # Check if parent context suggests usage
                            parent = await elem.evaluate_handle(
                                "el => el.parentElement"
                            )
                            if parent:
                                parent_text = (
                                    await parent.as_element().inner_text()
                                    if hasattr(parent, "as_element")
                                    else None
                                )
                                if parent_text and any(
                                    keyword in parent_text.lower()
                                    for keyword in [
                                        "usage",
                                        "request",
                                        "quota",
                                        "limit",
                                    ]
                                ):
                                    usage_data["usage_from_context"] = text.strip()
                                    break
                    except Exception:
                        continue
            except Exception:
                pass

        except Exception as e:
            print(f"Error extracting from selectors: {e}")

        # Method 2b: Check localStorage/sessionStorage for usage data
        try:
            storage_data = await page.evaluate(
                """
                () => {
                    const data = {};
                    // Check localStorage
                    for (let i = 0; i < localStorage.length; i++) {
                        const key = localStorage.key(i);
                        if (key && (key.includes('usage') || key.includes('quota') || key.includes('subscription'))) {
                            try {
                                data[`localStorage_${key}`] = JSON.parse(localStorage.getItem(key));
                            } catch {
                                data[`localStorage_${key}`] = localStorage.getItem(key);
                            }
                        }
                    }
                    // Check sessionStorage
                    for (let i = 0; i < sessionStorage.length; i++) {
                        const key = sessionStorage.key(i);
                        if (key && (key.includes('usage') || key.includes('quota') || key.includes('subscription'))) {
                            try {
                                data[`sessionStorage_${key}`] = JSON.parse(sessionStorage.getItem(key));
                            } catch {
                                data[`sessionStorage_${key}`] = sessionStorage.getItem(key);
                            }
                        }
                    }
                    return data;
                }
            """
            )
            if storage_data:
                usage_data["storage_data"] = storage_data
        except Exception as e:
            print(f"Error checking storage: {e}")

        # Method 3: Intercept and parse API responses for usage data
        api_responses = []
        parsed_api_data = {}

        async def handle_response(response):
            url = response.url
            # Focus on Cursor API endpoints that might contain usage data
            if "cursor.com/api" in url and any(
                keyword in url.lower()
                for keyword in [
                    "usage",
                    "quota",
                    "subscription",
                    "account",
                    "dashboard",
                    "auth/me",
                    "get-me",
                    "analytics",
                ]
            ):
                try:
                    # Only parse JSON responses
                    content_type = response.headers.get("content-type", "")
                    if "application/json" in content_type:
                        try:
                            json_data = await response.json()
                            api_responses.append(
                                {
                                    "url": url,
                                    "status": response.status,
                                    "data": json_data,
                                }
                            )

                            # Extract usage data from /api/usage-summary (primary source)
                            if "/api/usage-summary" in url:
                                if isinstance(json_data, dict):
                                    data = json_data.get("data", json_data)
                                    if isinstance(data, dict):
                                        # Extract membership type
                                        if "membershipType" in data:
                                            parsed_api_data["membership_type"] = data[
                                                "membershipType"
                                            ]

                                        # Extract billing cycle
                                        if "billingCycleStart" in data:
                                            parsed_api_data[
                                                "billing_cycle_start"
                                            ] = data["billingCycleStart"]
                                        if "billingCycleEnd" in data:
                                            parsed_api_data["billing_cycle_end"] = data[
                                                "billingCycleEnd"
                                            ]

                                        # Extract individual usage (plan and onDemand)
                                        if "individualUsage" in data:
                                            ind_usage = data["individualUsage"]
                                            if isinstance(ind_usage, dict):
                                                # Extract plan usage (main quota)
                                                if "plan" in ind_usage:
                                                    plan = ind_usage["plan"]
                                                    if isinstance(plan, dict):
                                                        parsed_api_data[
                                                            "usage_used"
                                                        ] = plan.get("used")
                                                        parsed_api_data[
                                                            "usage_limit"
                                                        ] = plan.get("limit")
                                                        parsed_api_data[
                                                            "usage_remaining"
                                                        ] = plan.get("remaining")
                                                        parsed_api_data[
                                                            "usage_included"
                                                        ] = plan.get(
                                                            "breakdown", {}
                                                        ).get("included")
                                                        parsed_api_data[
                                                            "usage_bonus"
                                                        ] = plan.get(
                                                            "breakdown", {}
                                                        ).get("bonus")
                                                        parsed_api_data[
                                                            "usage_total"
                                                        ] = plan.get(
                                                            "breakdown", {}
                                                        ).get("total")

                                                        # Extract plan percentages
                                                        if "totalPercentUsed" in plan:
                                                            parsed_api_data[
                                                                "total_percent_used"
                                                            ] = round(
                                                                plan["totalPercentUsed"]
                                                                * 100,
                                                                2,
                                                            )
                                                        if "autoPercentUsed" in plan:
                                                            parsed_api_data[
                                                                "auto_percent_used"
                                                            ] = round(
                                                                plan["autoPercentUsed"]
                                                                * 100,
                                                                2,
                                                            )
                                                        if "apiPercentUsed" in plan:
                                                            parsed_api_data[
                                                                "api_percent_used"
                                                            ] = round(
                                                                plan["apiPercentUsed"]
                                                                * 100,
                                                                2,
                                                            )

                                                # Extract on-demand usage
                                                if "onDemand" in ind_usage:
                                                    ondemand = ind_usage["onDemand"]
                                                    if isinstance(ondemand, dict):
                                                        parsed_api_data[
                                                            "ondemand_used"
                                                        ] = ondemand.get("used")
                                                        parsed_api_data[
                                                            "ondemand_limit"
                                                        ] = ondemand.get("limit")
                                                        parsed_api_data[
                                                            "ondemand_remaining"
                                                        ] = ondemand.get("remaining")

                                        # Extract limit type and unlimited flag
                                        if "limitType" in data:
                                            parsed_api_data["limit_type"] = data[
                                                "limitType"
                                            ]
                                        if "isUnlimited" in data:
                                            parsed_api_data["is_unlimited"] = data[
                                                "isUnlimited"
                                            ]

                                        # Extract display messages
                                        if "autoModelSelectedDisplayMessage" in data:
                                            parsed_api_data[
                                                "auto_usage_message"
                                            ] = data["autoModelSelectedDisplayMessage"]
                                        if "namedModelSelectedDisplayMessage" in data:
                                            parsed_api_data["api_usage_message"] = data[
                                                "namedModelSelectedDisplayMessage"
                                            ]

                            # Extract usage data from /api/usage endpoint
                            elif "/api/usage" in url and "?" in url:
                                if isinstance(json_data, dict):
                                    data = json_data.get("data", json_data)
                                    if isinstance(data, dict):
                                        if "numRequests" in data:
                                            parsed_api_data["num_requests"] = data[
                                                "numRequests"
                                            ]
                                        if "numRequestsTotal" in data:
                                            parsed_api_data[
                                                "num_requests_total"
                                            ] = data["numRequestsTotal"]

                            # Extract historical analytics data for last month calculation
                            elif "/api/dashboard/get-user-analytics" in url:
                                if isinstance(json_data, dict):
                                    data = json_data.get("data", json_data)
                                    if (
                                        isinstance(data, dict)
                                        and "dailyMetrics" in data
                                    ):
                                        parsed_api_data[
                                            "analytics_daily_metrics"
                                        ] = data["dailyMetrics"]

                            # Extract invoice cycles for date range calculation
                            elif "/api/dashboard/list-invoice-cycles" in url:
                                if isinstance(json_data, dict):
                                    data = json_data.get("data", json_data)
                                    if isinstance(data, dict) and "cycles" in data:
                                        parsed_api_data["invoice_cycles"] = data[
                                            "cycles"
                                        ]

                            # Extract monthly invoice data
                            elif "/api/dashboard/get-monthly-invoice" in url:
                                if isinstance(json_data, dict):
                                    data = json_data.get("data", json_data)
                                    if isinstance(data, dict):
                                        if "periodStartMs" in data:
                                            parsed_api_data[
                                                "invoice_period_start"
                                            ] = data["periodStartMs"]
                                        if "periodEndMs" in data:
                                            parsed_api_data[
                                                "invoice_period_end"
                                            ] = data["periodEndMs"]
                                        if "lastHardLimitCents" in data:
                                            parsed_api_data[
                                                "invoice_last_hard_limit_cents"
                                            ] = data["lastHardLimitCents"]

                            # Extract user info from /api/dashboard/get-me or /api/auth/me
                            elif (
                                "/api/dashboard/get-me" in url or "/api/auth/me" in url
                            ):
                                if isinstance(json_data, dict):
                                    data = json_data.get("data", json_data)
                                    if isinstance(data, dict):
                                        # Extract subscription/usage info
                                        for key in [
                                            "subscription",
                                            "plan",
                                            "tier",
                                            "usage",
                                            "quota",
                                            "requests",
                                            "limit",
                                        ]:
                                            if key in data:
                                                parsed_api_data[key] = data[key]

                                        # Look for nested subscription object
                                        if "subscription" in data and isinstance(
                                            data["subscription"], dict
                                        ):
                                            parsed_api_data[
                                                "subscription_details"
                                            ] = data["subscription"]

                                        # Look for usage/quota objects
                                        if "usage" in data and isinstance(
                                            data["usage"], dict
                                        ):
                                            parsed_api_data["usage_details"] = data[
                                                "usage"
                                            ]

                                        # Extract user info
                                        for key in ["email", "name", "id"]:
                                            if key in data:
                                                parsed_api_data[f"user_{key}"] = data[
                                                    key
                                                ]
                        except Exception as e:
                            # Response might not be JSON or might be empty
                            api_responses.append(
                                {"url": url, "status": response.status, "error": str(e)}
                            )
                    else:
                        api_responses.append(
                            {
                                "url": url,
                                "status": response.status,
                                "content_type": content_type,
                            }
                        )
                except Exception as e:
                    api_responses.append(
                        {"url": url, "status": response.status, "parse_error": str(e)}
                    )

        page.on("response", handle_response)

        # Navigate/reload to trigger API calls and find usage page
        print("Capturing API responses...")

        # Try multiple pages where usage might be displayed (dashboard is primary)
        usage_pages = [
            "https://cursor.com/dashboard",  # Primary - where usage is displayed
            "https://cursor.com/settings/account",
            "https://cursor.com/settings/subscription",
            "https://cursor.com/settings/billing",
            "https://cursor.com/settings",
            "https://cursor.com/account",
            "https://cursor.com/subscription",
        ]

        for page_url in usage_pages:
            try:
                print(f"  Trying: {page_url}")
                await page.goto(page_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)  # Wait for content to load

                # Check if this page has usage information
                page_text = await page.inner_text("body")
                if any(
                    keyword in page_text.lower()
                    for keyword in ["usage", "quota", "requests", "limit", "remaining"]
                ):
                    print(f"  ✓ Found usage-related content on {page_url}")
                    break
            except Exception:
                continue

        # Final wait for any API calls
        await asyncio.sleep(2)

        # Calculate usage for various date ranges from analytics data
        if "analytics_daily_metrics" in parsed_api_data:
            try:
                from datetime import datetime, timedelta

                daily_metrics = parsed_api_data["analytics_daily_metrics"]

                def calculate_usage_for_range(start_date, end_date, range_name):
                    """Calculate usage for a specific date range."""
                    # Convert to milliseconds (timestamp * 1000)
                    start_ms = int(start_date.timestamp() * 1000)
                    end_ms = int(
                        (end_date.timestamp() + timedelta(days=1).total_seconds() - 1)
                        * 1000
                    )

                    # Filter metrics for date range
                    range_metrics = [
                        m
                        for m in daily_metrics
                        if isinstance(m, dict)
                        and "date" in m
                        and start_ms <= int(m["date"]) <= end_ms
                    ]

                    if range_metrics:
                        # Sum up usage for the range
                        usage = {
                            "total_chat_requests": sum(
                                m.get("chatRequests", 0) for m in range_metrics
                            ),
                            "total_agent_requests": sum(
                                m.get("agentRequests", 0) for m in range_metrics
                            ),
                            "total_composer_requests": sum(
                                m.get("composerRequests", 0) for m in range_metrics
                            ),
                            "total_subscription_included_reqs": sum(
                                m.get("subscriptionIncludedReqs", 0)
                                for m in range_metrics
                            ),
                            "total_applies": sum(
                                m.get("totalApplies", 0) for m in range_metrics
                            ),
                            "total_accepts": sum(
                                m.get("totalAccepts", 0) for m in range_metrics
                            ),
                            "total_lines_added": sum(
                                m.get("linesAdded", 0) for m in range_metrics
                            ),
                            "total_lines_deleted": sum(
                                m.get("linesDeleted", 0) for m in range_metrics
                            ),
                            "days_with_usage": len(range_metrics),
                            "period_start": start_date.isoformat(),
                            "period_end": end_date.isoformat(),
                        }
                        parsed_api_data[range_name] = usage
                        print(
                            f"✓ Calculated {range_name}: {usage['total_subscription_included_reqs']} requests ({usage['days_with_usage']} days)"
                        )
                        return usage
                    return None

                # Calculate last month's usage
                now = datetime.now()
                first_day_this_month = datetime(now.year, now.month, 1)
                last_day_last_month = first_day_this_month - timedelta(days=1)
                first_day_last_month = datetime(
                    last_day_last_month.year, last_day_last_month.month, 1
                )

                calculate_usage_for_range(
                    first_day_last_month, last_day_last_month, "last_month_usage"
                )

                # Calculate usage from Nov 30 to yesterday
                # Note: November only has 30 days, so Nov 30 is the last day of November
                nov_30 = datetime(now.year, 11, 30)  # November 30
                yesterday = now - timedelta(days=1)
                yesterday = datetime(
                    yesterday.year, yesterday.month, yesterday.day
                )  # Start of yesterday

                # Only calculate if the range makes sense (Nov 30 to yesterday)
                if nov_30 <= yesterday:
                    calculate_usage_for_range(
                        nov_30, yesterday, "nov_30_to_yesterday_usage"
                    )

            except Exception as e:
                print(f"⚠️  Error calculating usage ranges: {e}")
                import traceback

                traceback.print_exc()

        # Merge parsed API data into usage_data
        if parsed_api_data:
            usage_data["api_parsed"] = parsed_api_data
            print(
                f"✓ Extracted data from API responses: {list(parsed_api_data.keys())}"
            )

        if api_responses:
            usage_data["api_endpoints"] = api_responses

        # Display results
        print("\n" + "=" * 60)
        print("Cursor Usage Information")
        print("=" * 60)
        print(f"\nTimestamp: {datetime.now().isoformat()}")
        print(f"Page URL: {page.url}")

        if usage_data:
            print("\nExtracted Data:")
            for key, value in usage_data.items():
                if key == "api_endpoints":
                    print(f"  {key}: {len(value)} API endpoints captured")
                elif key == "api_parsed":
                    print("\n  📊 Parsed API Data:")
                    for sub_key, sub_value in value.items():
                        if isinstance(sub_value, dict):
                            print(f"    {sub_key}:")
                            for k, v in sub_value.items():
                                print(f"      {k}: {v}")
                        else:
                            print(f"    {sub_key}: {sub_value}")
                else:
                    print(f"  {key}: {value}")
        else:
            print("\n⚠️  Could not automatically extract usage data.")
            print("   Please check the browser window for usage information.")
            print("   Common locations:")
            print("   - Settings > Account > Subscription")
            print("   - Settings > General > Account")
            print("   - Account dashboard")

        # Save screenshot for reference
        screenshot_path = (
            PROJECT_ROOT / "playwright" / ".auth" / "cursor_usage_screenshot.png"
        )
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"\n✓ Screenshot saved to {screenshot_path}")
        except Exception as e:
            print(f"Could not save screenshot: {e}")

        # Keep browser open for manual inspection
        print("\n" + "=" * 60)
        print("Browser will stay open for 30 seconds for manual inspection")
        print("You can also manually navigate to check usage if needed")
        print("=" * 60)
        await asyncio.sleep(30)

        await browser.close()

        return usage_data


if __name__ == "__main__":
    try:
        usage_data = asyncio.run(check_cursor_usage())
        if usage_data:
            timestamp = datetime.now()
            timestamp_str = timestamp.strftime("%Y-%m-%d-%H%M%S")

            # Prepare data structure
            data_to_save = {
                "timestamp": timestamp.isoformat(),
                "usage_data": usage_data,
            }

            # Save to auth directory (for quick access, gitignored)
            auth_output_path = (
                PROJECT_ROOT / "playwright" / ".auth" / "cursor_usage.json"
            )
            with open(auth_output_path, "w") as f:
                json.dump(data_to_save, f, indent=2)
            print(f"\n✓ Usage data saved to {auth_output_path}")

            # Save to repository data directory (timestamped for history)
            repo_output_path = USAGE_DATA_DIR / f"cursor_usage_{timestamp_str}.json"
            with open(repo_output_path, "w") as f:
                json.dump(data_to_save, f, indent=2)
            print(f"✓ Usage data saved to repository: {repo_output_path}")

            # Also save as "latest" for easy access
            latest_output_path = USAGE_DATA_DIR / "cursor_usage_latest.json"
            with open(latest_output_path, "w") as f:
                json.dump(data_to_save, f, indent=2)
            print(f"✓ Latest usage data saved to: {latest_output_path}")
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
