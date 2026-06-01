#!/usr/bin/env python3
"""
Twilio login automation using Playwright with 1Password browser extension.

This script:
1. Loads 1Password browser extension into Playwright
2. Relies on 1Password extension to auto-fill credentials (most secure approach)
3. Saves authenticated session state for future use
4. Navigates to SMS configuration for phone number 6503198857

Security: Credentials never leave 1Password's secure storage - extension handles everything.

Best Practices (from agent-context.md):
- Uses domcontentloaded instead of networkidle for more reliable page loads
- Polling pattern for login detection (checks every 5 seconds)
- Realistic browser fingerprint to avoid detection
- Graceful error handling with fallbacks
- Minimizes browser window on macOS
- Removes webdriver property to avoid automation detection
- Secure file permissions (600) on auth state files
"""

import asyncio
import platform
import subprocess
import sys
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
        # Chrome
        search_paths.extend(
            [
                home / "Library/Application Support/Google/Chrome/Default/Extensions",
                home / "Library/Application Support/Google/Chrome/Profile */Extensions",
            ]
        )
        # Edge
        search_paths.extend(
            [
                home / "Library/Application Support/Microsoft Edge/Default/Extensions",
                home
                / "Library/Application Support/Microsoft Edge/Profile */Extensions",
            ]
        )
        # Brave
        search_paths.extend(
            [
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
        # Handle glob patterns
        if "*" in str(base_path):
            import glob

            for expanded in glob.glob(str(base_path)):
                ext_path = Path(expanded) / extension_id
                if ext_path.exists():
                    # Get latest version directory
                    versions = sorted(ext_path.iterdir(), reverse=True)
                    if versions:
                        return versions[0]
        else:
            ext_path = base_path / extension_id
            if ext_path.exists():
                # Get latest version directory
                versions = sorted(ext_path.iterdir(), reverse=True)
                if versions:
                    return versions[0]

    return None


async def twilio_login_with_1password_extension():
    """
    Login to Twilio using 1Password browser extension for auto-fill.

    This is the most secure approach - credentials never leave 1Password.
    """
    # Find 1Password extension
    extension_path = find_1password_extension()

    if not extension_path:
        print("⚠️  1Password browser extension not found.")
        print("\nTo use this secure method:")
        print("1. Install 1Password browser extension in Chrome/Edge/Brave")
        print("2. Sign in to 1Password in the browser")
        print("3. Ensure '1Password' item exists for Twilio")
        print(
            "\nAlternatively, use: python execution/scripts/twilio_login_playwright.py"
        )
        print("   (uses 1Password CLI - still secure but less convenient)")
        sys.exit(1)

    print(f"✓ Found 1Password extension at: {extension_path}")
    print("  Extension will handle credential auto-fill securely")

    async with async_playwright() as p:
        print("\nLaunching browser with 1Password extension...")

        # Launch with stealth settings to avoid detection
        launch_args = [
            f"--disable-extensions-except={extension_path}",
            f"--load-extension={extension_path}",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            f"--user-data-dir={AUTH_STATE_DIR / 'browser_profile'}",
        ]

        # On macOS, position window off-screen initially
        if platform.system() == "Darwin":
            launch_args.append("--window-position=-10000,-10000")
        else:
            launch_args.append("--start-minimized")

        browser = await p.chromium.launch(
            headless=False,
            args=launch_args,
        )

        # Create context with realistic browser fingerprint
        auth_state_path = AUTH_STATE_DIR / "twilio_auth_state.json"
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
                print("  Creating new context - you'll need to sign in again")
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

        # Minimize window on macOS using AppleScript
        if platform.system() == "Darwin":

            async def minimize_window():
                """Minimize browser window on macOS."""
                await asyncio.sleep(1)  # Wait for window to appear
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

        # Navigate to Twilio login
        print("\nNavigating to Twilio login...")
        try:
            await page.goto(
                "https://console.twilio.com",
                wait_until="domcontentloaded",  # More reliable than networkidle
                timeout=60000,
            )
        except Exception as e:
            print(f"⚠️  Error navigating to Twilio: {e}")
            print("Continuing anyway - page may still be loading...")

        print("\n" + "=" * 60)
        print("1Password Extension Instructions:")
        print("=" * 60)
        print("1. The 1Password extension should detect the login form")
        print("2. Click the 1Password icon in the browser toolbar")
        print("3. Select your Twilio login item")
        print("4. 1Password will auto-fill credentials securely")
        print("5. Complete 2FA if required")
        print("=" * 60)

        # Wait for login to complete using polling pattern (best practice)
        print("\nWaiting for login to complete...")
        print("Please use 1Password extension to fill credentials.")

        # Polling pattern: check every N seconds instead of single long wait
        max_attempts = 60  # 60 attempts * 5 seconds = 5 minutes
        check_interval = 5

        login_successful = False
        for attempt in range(max_attempts):
            await asyncio.sleep(check_interval)

            try:
                current_url = page.url
                # Check if we're logged in (not on login page)
                if (
                    "console.twilio.com" in current_url
                    and "login" not in current_url.lower()
                ):
                    print(f"\n✓ Login successful! Current URL: {page.url}")
                    login_successful = True
                    break

                # Check if page title changed (indicates login)
                title = await page.title()
                if (
                    "Console" in title
                    and "Login" not in title
                    and "Sign in" not in title
                ):
                    print(f"\n✓ Login successful! Page: {title}")
                    login_successful = True
                    break
            except Exception:
                # Continue polling even if page check fails
                pass

            if (attempt + 1) % 6 == 0:  # Every 30 seconds
                print(f"  Still waiting... ({(attempt + 1) * check_interval}s elapsed)")

        if not login_successful:
            print("\n⚠️  Login timeout - please complete login manually")
            print("Browser will stay open for manual completion...")

        if login_successful:
            # Navigate to phone numbers/SMS configuration
            print("\nNavigating to Phone Numbers configuration...")
            try:
                await page.goto(
                    "https://console.twilio.com/us1/develop/phone-numbers/manage/incoming",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                # Wait for page to be interactive
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass  # Continue even if page is slow
            except Exception as e:
                print(f"⚠️  Error navigating to phone numbers: {e}")
                print("You can navigate manually in the browser")

            print("\n✓ Ready to check SMS configuration for 6503198857")
            print("Browser will stay open for manual inspection...")
            print("Press Ctrl+C to close when done.")

            # Keep browser open
            try:
                await asyncio.sleep(300)  # 5 minutes
            except KeyboardInterrupt:
                print("\n\nScript interrupted by user")
        else:
            # Keep browser open even if login didn't complete
            try:
                await asyncio.sleep(300)
            except KeyboardInterrupt:
                print("\n\nScript interrupted by user")

        # Save auth state for future use
        try:
            auth_state_path = AUTH_STATE_DIR / "twilio_auth_state.json"
            await context.storage_state(path=str(auth_state_path))
            import os

            os.chmod(auth_state_path, 0o600)
            print(f"\n✓ Auth state saved to {auth_state_path}")
        except Exception as e:
            print(f"⚠️  Could not save auth state: {e}")

        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(twilio_login_with_1password_extension())
    except KeyboardInterrupt:
        print("\n\nScript interrupted by user")
        sys.exit(0)
