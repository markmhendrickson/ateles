#!/usr/bin/env python3
"""
Download Ibercaja bank statements (extractos bancarios) using Playwright with 1Password browser extension.

This script:
1. Loads 1Password browser extension into Playwright
2. Relies on 1Password extension to auto-fill credentials (most secure approach)
3. Saves authenticated session state for future use
4. Navigates to Ibercaja online banking statements section
5. Downloads the extracto bancario PDF for the specified period

Security: Credentials never leave 1Password's secure storage - extension handles everything.

Usage:
    python execution/scripts/download_ibercaja_statement.py [--period <YYYY-MM>]

Examples:
    python execution/scripts/download_ibercaja_statement.py
    python execution/scripts/download_ibercaja_statement.py --period 2025-12
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
    from execution.scripts.config import get_data_dir

    DATA_DIR = get_data_dir()
except (ImportError, RuntimeError):
    try:
        from scripts.config import get_data_dir

        DATA_DIR = get_data_dir()
    except (ImportError, RuntimeError):
        print("WARNING: DATA_DIR not set. Using default path.")
        DATA_DIR = Path.home() / "Documents" / "data"

STATEMENTS_DIR = DATA_DIR / "attachments" / "statements"
STATEMENTS_DIR.mkdir(parents=True, exist_ok=True)


def find_1password_extension():
    """
    Find 1Password browser extension path.
    """
    system = platform.system()
    home = Path.home()
    search_paths = []

    if system == "Darwin":  # macOS
        search_paths.extend(
            [
                home / "Library/Application Support/Google/Chrome/Default/Extensions",
                home / "Library/Application Support/Google/Chrome/Profile */Extensions",
                home / "Library/Application Support/Microsoft Edge/Default/Extensions",
                home
                / "Library/Application Support/Microsoft Edge/Profile */Extensions",
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

    extension_id = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"  # 1Password extension ID

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


async def download_ibercaja_statement(period: str = None):
    """
    Downloads Ibercaja bank statements (extractos bancarios).
    """
    extension_path = find_1password_extension()
    if not extension_path:
        print("⚠️  1Password browser extension not found. Cannot proceed securely.")
        sys.exit(1)

    print(f"✓ Found 1Password extension at: {extension_path}")

    async with async_playwright() as p:
        print("\nLaunching browser with 1Password extension...")
        launch_args = [
            f"--disable-extensions-except={extension_path}",
            f"--load-extension={extension_path}",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ]
        if platform.system() == "Darwin":
            launch_args.append("--window-position=-10000,-10000")
        else:
            launch_args.append("--start-minimized")

        user_data_dir = AUTH_STATE_DIR / "browser_profile_ibercaja"
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            args=launch_args,
        )

        # Set viewport and locale for the persistent context
        await context.set_extra_http_headers({"Accept-Language": "es-ES,es;q=0.9"})

        auth_state_path = AUTH_STATE_DIR / "ibercaja_auth_state.json"

        if auth_state_path.exists():
            print("Loading saved authentication state...")
            try:
                # Load storage state into the persistent context
                with open(auth_state_path) as f:
                    import json

                    storage_state = json.load(f)
                await context.add_cookies(storage_state.get("cookies", []))
                print("  ✓ Auth state loaded - you should be automatically signed in")
            except Exception as e:
                print(f"  ⚠️  Could not load auth state: {e}")
                print("  Browser will open for login")
        else:
            print("No saved auth state found. Browser will open for login.")
            print(f"  After first sign-in, auth will be saved to: {auth_state_path}")

        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """
        )

        # Get the first page from persistent context (or create new one)
        pages = context.pages
        if pages:
            page = pages[0]
        else:
            page = await context.new_page()

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

        print("\nNavigating to Ibercaja online banking...")
        await page.goto(
            "https://www.ibercaja.es/", wait_until="domcontentloaded", timeout=60000
        )

        # Wait for login to complete
        print("Waiting for login to complete...")
        max_attempts = 30
        logged_in = False

        for attempt in range(max_attempts):
            await asyncio.sleep(10)
            current_url = page.url
            print(f"Checking login status... ({attempt + 1}/{max_attempts})")

            # Ibercaja typically redirects to a dashboard or account overview after login
            if "ibercaja.es" in current_url and (
                "cuenta" in current_url.lower()
                or "banca" in current_url.lower()
                or "login" not in current_url.lower()
            ):
                print("✓ Logged in successfully!")
                logged_in = True
                break

        if not logged_in:
            print("⚠️  Login timeout. Please log in manually...")
            await asyncio.sleep(30)

        # Navigate to Statements/Extractos section
        print("\nNavigating to Extractos Bancarios...")
        try:
            # Ibercaja typically has statements under "Cuentas" > "Extractos" or "Documentos"
            # Try common paths
            await page.goto(
                "https://www.ibercaja.es/banca-online/cuentas/extractos",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(3)

            # Alternative: Look for "Extractos" link in navigation
            try:
                await page.click('text="Extractos"', timeout=10000)
                await asyncio.sleep(3)
            except Exception:
                pass
        except Exception as e:
            print(f"⚠️  Error navigating to extractos: {e}")
            print("Please navigate manually to: Cuentas > Extractos Bancarios")
            await asyncio.sleep(10)

        # Set up download handler
        downloads = []

        async def handle_download(download):
            downloads.append(download)
            print(f"  Download started: {download.suggested_filename}")

        page.on("download", handle_download)

        # Find and download statement
        print("\nLooking for extracto bancario to download...")

        if period:
            print(f"  Looking for period: {period}")
            # Parse period to get month name in Spanish
            try:
                period_date = datetime.strptime(period, "%Y-%m")
                month_names_es = [
                    "enero",
                    "febrero",
                    "marzo",
                    "abril",
                    "mayo",
                    "junio",
                    "julio",
                    "agosto",
                    "septiembre",
                    "octubre",
                    "noviembre",
                    "diciembre",
                ]
                month_name = month_names_es[period_date.month - 1]
                year = period_date.year
                print(f"  Looking for: {month_name} {year}")
            except ValueError:
                print(f"  ⚠️  Invalid period format. Expected YYYY-MM, got: {period}")

        try:
            # Look for download link or button for the statement
            # Ibercaja may have different UI patterns - try common selectors
            selectors = [
                'a:has-text("Descargar")',
                'a:has-text("PDF")',
                'button:has-text("Descargar")',
                'a[href*="extracto"]',
                'a[href*="pdf"]',
            ]

            download_link = None
            for selector in selectors:
                try:
                    download_link = await page.wait_for_selector(selector, timeout=5000)
                    if download_link:
                        print(f"  ✓ Found download link with selector: {selector}")
                        break
                except Exception:
                    continue

            if not download_link:
                print("  ⚠️  Could not find download link automatically.")
                print(
                    "  Please click the download button/link manually in the browser."
                )
                await asyncio.sleep(30)  # Wait for manual download
            else:
                async with page.expect_download() as download_info:
                    await download_link.click()
                download = await download_info.value

                # Determine save path
                current_year = datetime.now().year
                save_dir = STATEMENTS_DIR / str(current_year)
                save_dir.mkdir(parents=True, exist_ok=True)

                filename_parts = ["ibercaja"]
                if period:
                    filename_parts.append(period.replace("-", "_"))  # e.g., 2025_12

                final_filename = "_".join(filename_parts) + ".pdf"
                save_path = save_dir / final_filename

                await download.save_as(save_path)
                print(f"✓ Extracto bancario downloaded to: {save_path}")
        except Exception as e:
            print(f"⚠️  Could not find or download extracto: {e}")
            print("Please check the Ibercaja website manually.")

        # Save auth state
        storage_state = await context.storage_state()
        with open(auth_state_path, "w") as f:
            import json

            json.dump(storage_state, f, indent=2)
        print(f"✓ Authentication state saved to: {auth_state_path}")

        await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download Ibercaja bank statements (extractos bancarios)."
    )
    parser.add_argument(
        "--period", help="Statement period in YYYY-MM format (e.g., 2025-12)"
    )
    args = parser.parse_args()
    asyncio.run(download_ibercaja_statement(args.period))
