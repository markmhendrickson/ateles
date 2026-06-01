#!/usr/bin/env python3
"""
Diagnostic script to discover Minted API endpoints for delivery/order information.

This script will:
1. Authenticate with Minted
2. Try various API endpoints
3. Navigate to the finalize page and inspect network requests
4. Report findings to help identify the correct endpoints
"""

import json
import os
import sys
import time
from pathlib import Path

import requests
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

try:
    from scripts.credentials import get_credential_by_domain

    email, password = get_credential_by_domain("minted.com")
except ImportError:
    email = os.environ.get("minted_email") or input(
        "Enter your minted.com email address: "
    )
    password = os.environ.get("minted_password") or input(
        "Enter your minted.com password: "
    )

# Webdriver options
options = Options()
# Don't use headless so we can see what's happening
# options.add_argument("--headless")
options.add_argument("--enable-logging")
options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
service = Service(ChromeDriverManager().install())
driver = Chrome(service=service, options=options)

try:
    print("Authenticating with Minted...")
    URL = "https://www.minted.com/login"
    driver.get(URL)

    email_elem = driver.find_element(By.XPATH, '//*[@id="identifierMNTD"]')
    email_elem.send_keys(email)
    password_elem = driver.find_element(By.XPATH, '//*[@id="password"]')
    password_elem.send_keys(password)
    login_submit = driver.find_element(
        By.XPATH, '//*[@id="__next"]/div[3]/div/form/div[2]/div[1]/button'
    )
    login_submit.click()

    time.sleep(5)

    # Get cookies
    cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
    print(f"✓ Authenticated. Got {len(cookies)} cookies")

    # Try API endpoints
    print("\n" + "=" * 60)
    print("Testing API Endpoints")
    print("=" * 60)

    endpoints_to_test = [
        "https://addressbook.minted.com/api/contacts/contacts/?format=json",  # We know this works
        "https://addressbook.minted.com/api/orders/",
        "https://addressbook.minted.com/api/orders/?format=json",
        "https://addressbook.minted.com/api/deliveries/",
        "https://addressbook.minted.com/api/shipments/",
        "https://www.minted.com/api/orders/",
        "https://www.minted.com/api/orders/?format=json",
        "https://www.minted.com/api/deliveries/",
        "https://www.minted.com/api/shipments/",
        "https://addressbook.minted.com/api/addressbook/orders/",
        "https://addressbook.minted.com/api/addressbook/deliveries/",
    ]

    working_endpoints = []
    for endpoint in endpoints_to_test:
        try:
            response = requests.get(endpoint, cookies=cookies, timeout=10)
            print(f"\n{endpoint}")
            print(f"  Status: {response.status_code}")
            if response.status_code == 200:
                try:
                    data = response.json()
                    print(f"  ✓ Success! Response type: {type(data)}")
                    if isinstance(data, list):
                        print(f"  List length: {len(data)}")
                        if len(data) > 0:
                            print(
                                f"  First item keys: {list(data[0].keys()) if isinstance(data[0], dict) else 'N/A'}"
                            )
                    elif isinstance(data, dict):
                        print(f"  Dict keys: {list(data.keys())}")
                    working_endpoints.append(endpoint)
                except Exception:
                    print(f"  Response is not JSON (length: {len(response.text)})")
            else:
                print(f"  ✗ Failed with status {response.status_code}")
        except Exception as e:
            print(f"  ✗ Error: {e}")

    # Navigate to finalize page and capture network requests
    print("\n" + "=" * 60)
    print("Navigating to Finalize Page and Capturing Network Requests")
    print("=" * 60)

    finalize_urls = [
        "https://www.minted.com/addressbook/my-account/finalize/0",
        "https://addressbook.minted.com/my-account/finalize/0",
        "https://www.minted.com/addressbook/finalize",
    ]

    for finalize_url in finalize_urls:
        try:
            print(f"\nTrying: {finalize_url}")
            driver.get(finalize_url)
            time.sleep(5)

            # Get page title to confirm we're on the right page
            print(f"  Page title: {driver.title}")

            # Get performance logs (network requests)
            logs = driver.get_log("performance")
            api_requests = []
            for log in logs:
                try:
                    message = json.loads(log["message"])
                    if message["message"]["method"] == "Network.responseReceived":
                        url = message["message"]["params"]["response"]["url"]
                        if (
                            "api" in url.lower()
                            or "json" in url.lower()
                            or "order" in url.lower()
                            or "delivery" in url.lower()
                        ):
                            api_requests.append(url)
                except Exception:
                    pass

            if api_requests:
                print(f"  Found {len(api_requests)} potential API requests:")
                for req in set(api_requests):
                    print(f"    - {req}")

            # Try to find JSON data in page source
            page_source = driver.page_source
            import re

            json_matches = re.findall(
                r"window\.__[A-Z_]+__\s*=\s*({.+?});", page_source, re.DOTALL
            )
            if json_matches:
                print(f"  Found {len(json_matches)} JSON data structures in page")
                for i, match in enumerate(json_matches[:3]):  # Show first 3
                    try:
                        data = json.loads(match)
                        print(f"    Structure {i + 1} keys: {list(data.keys())[:10]}")
                    except Exception:
                        print(f"    Structure {i + 1}: Could not parse")

            # Look for recipient/address elements
            try:
                recipient_elements = driver.find_elements(
                    By.CSS_SELECTOR,
                    "[data-recipient], .recipient, [class*='recipient'], [class*='address'], [class*='delivery']",
                )
                if recipient_elements:
                    print(
                        f"  Found {len(recipient_elements)} potential recipient elements"
                    )
            except Exception:
                pass

        except Exception as e:
            print(f"  ✗ Error: {e}")

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Working API endpoints: {len(working_endpoints)}")
    for endpoint in working_endpoints:
        print(f"  ✓ {endpoint}")

    if not working_endpoints:
        print("\n⚠ No working endpoints found for orders/deliveries")
        print("  Suggestion: Check the browser Network tab manually when accessing:")
        print("  https://www.minted.com/addressbook/my-account/finalize/0")

except Exception as e:
    print(f"❌ Error: {e}", file=sys.stderr)
    import traceback

    traceback.print_exc()
finally:
    input("\nPress Enter to close browser...")
    driver.close()
