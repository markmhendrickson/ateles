#!/usr/bin/env python3
"""
Download Fidelity account statements using Playwright with 1Password browser extension.

This script:
1. Loads 1Password browser extension into Playwright
2. Relies on 1Password extension to auto-fill credentials (most secure approach)
3. Saves authenticated session state for future use
4. Navigates to Fidelity statements/documents section
5. Downloads the latest statement PDF

Security: Credentials never leave 1Password's secure storage - extension handles everything.

Usage:
    python execution/scripts/download_fidelity_statement.py [--account <account_ending>] [--period <YYYY-MM>]

Examples:
    python execution/scripts/download_fidelity_statement.py
    python execution/scripts/download_fidelity_statement.py --account 0208 --period 2025-12
"""

import argparse
import asyncio
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

PROJECT_ROOT = Path(__file__).parent.parent.parent
AUTH_STATE_DIR = PROJECT_ROOT / "playwright" / ".auth"
AUTH_STATE_DIR.mkdir(parents=True, exist_ok=True)

# Add project root to path for config
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from scripts.config import get_data_dir

    DATA_DIR = get_data_dir()
except RuntimeError:
    print("WARNING: DATA_DIR not set. Using default path.")
    DATA_DIR = Path.home() / "Documents" / "data"

STATEMENTS_DIR = DATA_DIR / "attachments" / "statements"
STATEMENTS_DIR.mkdir(parents=True, exist_ok=True)


def find_1password_extension():
    """
    Find 1Password browser extension path.

    Returns path to extension directory or None if not found.
    """
    system = platform.system()
    home = Path.home()

    # Common extension locations
    search_paths = []

    if system == "Darwin":  # macOS
        search_paths.extend(
            [
                home / "Library/Application Support/Google/Chrome/Default/Extensions",
                home / "Library/Application Support/Google/Chrome/Profile */Extensions",
                home / "Library/Application Support/Microsoft Edge/Default/Extensions",
                home
                / "Library/Application Support/BraveSoftware/Brave-Browser/Default/Extensions",
            ]
        )
    elif system == "Linux":
        search_paths.extend(
            [
                home / ".config/google-chrome/Default/Extensions",
                home / ".config/chromium/Default/Extensions",
            ]
        )
    elif system == "Windows":
        search_paths.extend(
            [
                Path.home()
                / "AppData/Local/Google/Chrome/User Data/Default/Extensions",
                Path.home()
                / "AppData/Local/Microsoft/Edge/User Data/Default/Extensions",
            ]
        )

    # Search for 1Password extension (ID: aeblfdkhhhdcdjpifhhbdiojplfjncoa)
    extension_id = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"

    for base_path in search_paths:
        if "*" in str(base_path):
            import glob

            for expanded in glob.glob(str(base_path)):
                ext_path = Path(expanded) / extension_id
                if ext_path.exists():
                    versions = sorted(ext_path.iterdir(), reverse=True)
                    if versions:
                        return versions[0]
        else:
            ext_path = base_path / extension_id
            if ext_path.exists():
                versions = sorted(ext_path.iterdir(), reverse=True)
                if versions:
                    return versions[0]

    return None


async def download_fidelity_statement(account_ending: str = None, period: str = None):
    """
    Download Fidelity account statement using browser automation.

    Args:
        account_ending: Last 4 digits of account number (e.g., "0208")
        period: Statement period in YYYY-MM format (e.g., "2025-12")
    """
    # Find 1Password extension
    extension_path = find_1password_extension()

    if not extension_path:
        print("⚠️  1Password browser extension not found.")
        print("\nTo use this secure method:")
        print("1. Install 1Password browser extension in Chrome/Edge/Brave")
        print("2. Sign in to 1Password in the browser")
        print("3. Ensure 'Fidelity' item exists in 1Password")
        sys.exit(1)

    print(f"✓ Found 1Password extension at: {extension_path}")
    print("  Extension will handle credential auto-fill securely")

    async with async_playwright() as p:
        print("\nLaunching browser with 1Password extension...")

        # Launch with stealth settings
        launch_args = [
            f"--disable-extensions-except={extension_path}",
            f"--load-extension={extension_path}",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            f"--user-data-dir={AUTH_STATE_DIR / 'browser_profile'}",
        ]

        if platform.system() == "Darwin":
            launch_args.append("--window-position=-10000,-10000")
        else:
            launch_args.append("--start-minimized")

        browser = await p.chromium.launch(
            headless=False,
            args=launch_args,
        )

        # Create context with realistic browser fingerprint
        auth_state_path = AUTH_STATE_DIR / "fidelity_auth_state.json"
        context_options = {
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "locale": "en-US",
            "timezone_id": "America/Los_Angeles",
        }

        if auth_state_path.exists():
            print("Loading saved authentication state...")
            try:
                context_options["storage_state"] = str(auth_state_path)
                context = await browser.new_context(**context_options)
                print("  ✓ Auth state loaded - you should be automatically signed in")
            except Exception as e:
                print(f"  ⚠️  Could not load auth state: {e}")
                context = await browser.new_context(**context_options)
        else:
            print("No saved auth state found. Browser will open for login.")
            print(f"  After first sign-in, auth will be saved to: {auth_state_path}")
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

        # Minimize window on macOS
        if platform.system() == "Darwin":

            async def minimize_window():
                await asyncio.sleep(1)
                try:
                    apple_scripts = [
                        'tell application "System Events" to tell process "Chromium" to set miniaturized of every window to true',
                        'tell application "System Events" to tell process "Google Chrome" to set miniaturized of every window to true',
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
                except Exception:
                    pass

            asyncio.create_task(minimize_window())

        # Navigate to Fidelity login
        print("\nNavigating to Fidelity...")
        try:
            await page.goto(
                "https://www.fidelity.com", wait_until="domcontentloaded", timeout=60000
            )
        except Exception as e:
            print(f"⚠️  Error navigating: {e}")
            print("Continuing anyway...")

        # Wait for login (polling pattern)
        print("\n" + "=" * 60)
        print("Please log in to Fidelity if prompted")
        print("Waiting for login to complete...")
        print("=" * 60)

        max_attempts = 30  # 30 attempts * 10 seconds = 5 minutes
        logged_in = False

        for attempt in range(max_attempts):
            await asyncio.sleep(10)
            current_url = page.url
            print(f"Checking login status... ({attempt + 1}/{max_attempts})")

            # Check if we're logged in (Fidelity redirects to account page after login)
            if "fidelity.com" in current_url and (
                "account" in current_url.lower()
                or "portfolio" in current_url.lower()
                or "login" not in current_url.lower()
            ):
                print("✓ Logged in successfully!")
                logged_in = True
                break

        if not logged_in:
            print(
                "⚠️  Login timeout. Please log in manually and the script will continue..."
            )
            await asyncio.sleep(30)  # Give user time to log in manually

        # Save auth state
        await context.storage_state(path=str(auth_state_path))
        print(f"✓ Authentication state saved to: {auth_state_path}")

        # Navigate to Documents/Statements
        print("\nNavigating to Documents & Statements...")
        try:
            # Try direct navigation to documents page
            await page.goto(
                "https://www.fidelity.com/accounts-statements/statements",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(3)
        except Exception as e:
            print(f"⚠️  Error navigating to statements: {e}")
            print("Trying alternative navigation...")
            # Alternative: Navigate via menu
            try:
                await page.goto(
                    "https://www.fidelity.com", wait_until="domcontentloaded"
                )
                await asyncio.sleep(2)
                # Look for "Accounts & Trade" menu
                await page.click('text="Accounts & Trade"', timeout=10000)
                await asyncio.sleep(1)
                # Look for "Documents" or "Statements"
                await page.click('text="Documents"', timeout=10000)
                await asyncio.sleep(3)
            except Exception as e2:
                print(f"⚠️  Alternative navigation failed: {e2}")
                print("Please navigate manually to: Accounts & Trade > Documents")
                await asyncio.sleep(10)

        # Set up download handler
        downloads = []

        async def handle_download(download):
            downloads.append(download)
            print(f"  Download started: {download.suggested_filename}")

        page.on("download", handle_download)

        # Find and download statement
        print("\nLooking for statement to download...")

        if account_ending:
            print(f"  Looking for account ending in: {account_ending}")
        if period:
            print(f"  Looking for period: {period}")

        try:
            # Wait for statements list to load
            await page.wait_for_selector(
                'a[href*="statement"], a[href*="pdf"], button:has-text("Download")',
                timeout=30000,
            )

            # Try to find statement link/button
            # Fidelity typically has download buttons or links for each statement
            statement_selectors = [
                f'a:has-text("{period}")' if period else None,
                f'a:has-text("{account_ending}")' if account_ending else None,
                'a[href*="statement"]:has-text("Download")',
                'button:has-text("Download")',
                'a[href*=".pdf"]',
            ]

            downloaded = False
            for selector in statement_selectors:
                if selector:
                    try:
                        element = await page.query_selector(selector)
                        if element:
                            print(f"  Found statement element: {selector}")
                            async with page.expect_download() as download_info:
                                await element.click()
                            download = await download_info.value
                            downloaded = True
                            break
                    except Exception:
                        continue

            if not downloaded:
                # Try clicking the most recent statement (usually first in list)
                try:
                    print("  Attempting to download most recent statement...")
                    statement_link = await page.query_selector(
                        'a[href*="statement"]:first-of-type, a[href*=".pdf"]:first-of-type'
                    )
                    if statement_link:
                        async with page.expect_download() as download_info:
                            await statement_link.click()
                        download = await download_info.value
                        downloaded = True
                except Exception as e:
                    print(f"  ⚠️  Could not auto-download: {e}")
                    print("  Please download statement manually from the page")
                    await asyncio.sleep(30)  # Give user time to download manually

            if downloaded:
                # Wait for download to complete
                await asyncio.sleep(5)

                # Get downloaded file
                if downloads:
                    download = downloads[-1]
                    suggested_filename = download.suggested_filename

                    # Generate filename
                    year = datetime.now().year
                    if period:
                        year_month = period.replace("-", "_")
                        filename = f"fidelity_account_{account_ending or 'unknown'}_{year_month}.pdf"
                    else:
                        filename = f"fidelity_account_{account_ending or 'unknown'}_{datetime.now().strftime('%Y_%m')}.pdf"

                    # Save to statements directory
                    year_dir = STATEMENTS_DIR / str(year)
                    year_dir.mkdir(parents=True, exist_ok=True)
                    save_path = year_dir / filename

                    # Save download
                    await download.save_as(save_path)
                    print(f"\n✓ Statement downloaded to: {save_path}")
                    print(f"  Original filename: {suggested_filename}")

        except Exception as e:
            print(f"⚠️  Error downloading statement: {e}")
            print("Please download statement manually from the page")
            await asyncio.sleep(30)

        print("\nBrowser will stay open for 10 seconds for verification...")
        await asyncio.sleep(10)

        await browser.close()
        print("\n✓ Done")


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Download Fidelity account statements")
    parser.add_argument(
        "--account", type=str, help="Last 4 digits of account number (e.g., 0208)"
    )
    parser.add_argument(
        "--period", type=str, help="Statement period in YYYY-MM format (e.g., 2025-12)"
    )

    args = parser.parse_args()

    asyncio.run(
        download_fidelity_statement(account_ending=args.account, period=args.period)
    )


if __name__ == "__main__":
    main()
