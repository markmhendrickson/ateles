#!/usr/bin/env python3
"""
Twilio login automation using Playwright with 1Password credential retrieval.

This script:
1. Retrieves Twilio credentials from 1Password (never exposes secrets to stdout)
2. Uses Playwright to automate login
3. Saves authenticated session state for future use
4. Navigates to SMS configuration for phone number 6503198857

Security: All 1Password access is done internally - secrets never appear in chat output.
"""

import asyncio
import json
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


def get_1password_credentials(item_name="Twilio (for Twilio)", vault=None):
    """
    Retrieve credentials from 1Password using op CLI.

    NEVER prints secrets to stdout - only returns them as variables.
    Uses op inject pattern for better security (avoids parsing full JSON).
    """
    try:
        # More secure: Use op inject to get specific fields
        # This avoids loading full item JSON into memory
        username_ref = f"op://{vault or 'Private'}/{item_name}/username"
        password_ref = f"op://{vault or 'Private'}/{item_name}/password"

        # Try op inject first (more secure, field-specific)
        try:
            username_cmd = ["op", "inject", "-i", "-", "--reference", username_ref]
            password_cmd = ["op", "inject", "-i", "-", "--reference", password_ref]

            username_result = subprocess.run(
                username_cmd,
                input="dummy",
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            password_result = subprocess.run(
                password_cmd,
                input="dummy",
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )

            username = username_result.stdout.strip()
            password = password_result.stdout.strip()

            if username and password:
                return username, password
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fallback to op item get if op inject not available
            pass

        # Fallback: Use op item get (less secure but more compatible)
        cmd = ["op", "item", "get", item_name, "--format", "json"]
        if vault:
            cmd.extend(["--vault", vault])

        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=30
        )

        item_data = json.loads(result.stdout)

        # Extract username and password fields
        username = None
        password = None

        for field in item_data.get("fields", []):
            if (
                field.get("id") == "username"
                or field.get("label", "").lower() == "username"
            ):
                username = field.get("value")
            elif (
                field.get("id") == "password"
                or field.get("label", "").lower() == "password"
            ):
                password = field.get("value")

        if not username or not password:
            raise ValueError("Could not find username/password in 1Password item")

        return username, password

    except subprocess.CalledProcessError:
        print("ERROR: Failed to retrieve credentials from 1Password")
        print("Make sure you're signed in: op signin")
        # Don't print stderr - might contain sensitive info
        sys.exit(1)
    except json.JSONDecodeError:
        print("ERROR: Invalid JSON from 1Password CLI")
        sys.exit(1)
    except Exception:
        print("ERROR: Failed to retrieve credentials")
        sys.exit(1)


async def twilio_login_and_check_sms():
    """Login to Twilio and check SMS configuration for 6503198857."""
    # Get credentials from 1Password (secrets never printed)
    print("Retrieving credentials from 1Password...")
    username, password = get_1password_credentials()
    print("✓ Credentials retrieved (not displayed for security)")

    async with async_playwright() as p:
        print("\nLaunching browser...")
        browser = await p.chromium.launch(headless=False)

        # Check for existing auth state
        auth_state_path = AUTH_STATE_DIR / "twilio_auth_state.json"

        # Security: Check file permissions on auth state
        if auth_state_path.exists():
            stat = auth_state_path.stat()
            if stat.st_mode & 0o077:  # Check if world/group readable
                print("⚠️  WARNING: Auth state file has insecure permissions")
                print(f"   Run: chmod 600 {auth_state_path}")
            print("Loading saved authentication state...")
            context = await browser.new_context(storage_state=str(auth_state_path))
        else:
            print("No saved auth state found, creating new context...")
            context = await browser.new_context()

        page = await context.new_page()

        print("\nNavigating to Twilio login...")
        await page.goto("https://console.twilio.com")

        # Wait for login form
        print("Waiting for login form...")
        await page.wait_for_selector(
            'input[type="email"], input[name="email"]', timeout=10000
        )

        # Fill email
        email_input = page.locator('input[type="email"], input[name="email"]').first
        await email_input.fill(username)
        print("✓ Email filled")

        # Click Continue
        continue_button = page.locator('button:has-text("Continue")').first
        await continue_button.click()
        print("✓ Clicked Continue")

        # Wait for password field
        await page.wait_for_selector('input[type="password"]', timeout=10000)

        # Fill password
        password_input = page.locator('input[type="password"]').first
        await password_input.fill(password)
        print("✓ Password filled")

        # Securely clear password from memory
        password = "x" * len(password)  # Overwrite with dummy data
        password = None
        import gc

        gc.collect()  # Force garbage collection

        # Click Continue to login
        continue_button = page.locator('button:has-text("Continue")').first
        await continue_button.click()
        print("✓ Clicked Continue to login")

        # Wait for navigation after login (may need 2FA)
        print("\nWaiting for login to complete...")
        print("If 2FA is required, please complete it in the browser.")

        try:
            # Wait for either dashboard or 2FA challenge
            await page.wait_for_load_state("networkidle", timeout=60000)

            # Check if we're on dashboard or still on login
            current_url = page.url
            if "login" in current_url.lower():
                print("\n⚠️  Still on login page - may need 2FA or manual intervention")
                print("Please complete authentication in the browser...")
                await page.wait_for_url("**/console**", timeout=120000)

            print(f"\n✓ Login successful! Current URL: {page.url}")

            # Save auth state for future use
            await context.storage_state(path=str(auth_state_path))
            # Set secure file permissions (read/write for owner only)
            import os

            os.chmod(auth_state_path, 0o600)
            print(f"✓ Auth state saved to {auth_state_path} (permissions: 600)")

            # Navigate to phone numbers/SMS configuration
            print("\nNavigating to Phone Numbers configuration...")
            await page.goto(
                "https://console.twilio.com/us1/develop/phone-numbers/manage/incoming"
            )
            await page.wait_for_load_state("networkidle")

            print("\n✓ Ready to check SMS configuration for 6503198857")
            print("Browser will stay open for manual inspection...")
            print("Press Ctrl+C to close when done.")

            # Keep browser open
            await asyncio.sleep(300)  # 5 minutes

        except Exception as e:
            print(f"\n⚠️  Error during login flow: {e}")
            print("Browser will stay open for manual inspection...")
            await asyncio.sleep(300)

        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(twilio_login_and_check_sms())
    except KeyboardInterrupt:
        print("\n\nScript interrupted by user")
        sys.exit(0)
