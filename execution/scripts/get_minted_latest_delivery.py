#!/usr/bin/env python3
"""
Get recipients from the latest Minted card delivery.

This script authenticates with Minted.com and queries the API to find
the most recent delivery and list all recipients who were sent cards.

Adapted from minted-export scripts for authentication pattern.

Usage:
    python execution/scripts/get_minted_latest_delivery.py [email] [password]
"""

import json
import os
import sys
from pathlib import Path
from time import sleep

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

# URL for minted login page
URL = "https://www.minted.com/login"

# Get credentials (same pattern as minted-export scripts)
if len(sys.argv) >= 3:
    minted_email = sys.argv[1]
    minted_password = sys.argv[2]
else:
    try:
        minted_email = os.environ["minted_email"]
    except KeyError:
        minted_email = input("Enter your minted.com email address: ")
    try:
        minted_password = os.environ["minted_password"]
    except KeyError:
        import getpass

        minted_password = getpass.getpass("Enter your minted.com password: ")

# Webdriver options
options = Options()
options.add_argument("--headless")
service = Service(ChromeDriverManager().install())
driver = Chrome(service=service, options=options)

try:
    print("Authenticating with Minted...")
    driver.get(URL)

    # Selenium handles login form (same pattern as minted-export)
    email_elem = driver.find_element(By.XPATH, '//*[@id="identifierMNTD"]')
    email_elem.send_keys(minted_email)
    password_elem = driver.find_element(By.XPATH, '//*[@id="password"]')
    password_elem.send_keys(minted_password)
    login_submit = driver.find_element(
        By.XPATH, '//*[@id="__next"]/div[3]/div/form/div[2]/div[1]/button'
    )
    login_submit.click()

    sleep(5)  # Wait for login to complete

    # Obtain cookies from selenium session (same pattern as minted-export)
    cookies = {c["name"]: c["value"] for c in driver.get_cookies()}

    print("Fetching delivery information...")

    # Try various API endpoints to find delivery/order information
    # Using the same cookie-based authentication pattern as minted-export
    endpoints_to_try = [
        "https://addressbook.minted.com/api/orders/",
        "https://addressbook.minted.com/api/deliveries/",
        "https://addressbook.minted.com/api/shipments/",
        "https://www.minted.com/api/orders/",
        "https://www.minted.com/api/deliveries/",
        "https://www.minted.com/api/shipments/",
        "https://addressbook.minted.com/api/addressbook/orders/",
        "https://addressbook.minted.com/api/addressbook/deliveries/",
    ]

    delivery_data = None
    working_endpoint = None

    for endpoint in endpoints_to_try:
        try:
            response = requests.get(
                endpoint,
                cookies=cookies,
                timeout=30,
            )
            if response.status_code == 200:
                data = response.json()
                # Check if this looks like delivery/order data
                if isinstance(data, list | dict) and len(data) > 0:
                    working_endpoint = endpoint
                    delivery_data = data
                    print(f"✓ Found data at: {endpoint}")
                    break
        except Exception:
            continue

    # Process delivery data
    if delivery_data:
        print(f"\n{'=' * 60}")
        print("Latest Delivery Information")
        print(f"{'=' * 60}\n")

        # Handle different response formats
        if isinstance(delivery_data, list):
            # Assume first item is latest, or sort by date
            latest = delivery_data[0]
            if len(delivery_data) > 1:
                # Try to find the most recent by date
                try:
                    latest = max(
                        delivery_data,
                        key=lambda x: x.get(
                            "created_at", x.get("date", x.get("order_date", ""))
                        ),
                    )
                except Exception:
                    latest = delivery_data[0]
        elif isinstance(delivery_data, dict):
            latest = delivery_data

        # Extract recipients
        recipients = []
        if "recipients" in latest:
            recipients = latest["recipients"]
        elif "addresses" in latest:
            recipients = latest["addresses"]
        elif "contacts" in latest:
            recipients = latest["contacts"]
        elif "items" in latest:
            # Items might contain recipient info
            for item in latest["items"]:
                if "recipient" in item:
                    recipients.append(item["recipient"])
                elif "address" in item:
                    recipients.append(item["address"])

        # Print delivery info
        print(
            f"Delivery Date: {latest.get('created_at', latest.get('date', latest.get('order_date', 'Unknown')))}"
        )
        print(
            f"Order ID: {latest.get('id', latest.get('order_id', latest.get('order_number', 'Unknown')))}"
        )
        print(f"Status: {latest.get('status', 'Unknown')}")
        print(f"\nRecipients ({len(recipients)}):")
        print(f"{'-' * 60}")

        for i, recipient in enumerate(recipients, 1):
            name = recipient.get(
                "name",
                recipient.get("full_name", recipient.get("recipient_name", "Unknown")),
            )
            address = recipient.get("address", recipient.get("address1", ""))
            if recipient.get("address2"):
                address += f", {recipient['address2']}"
            city = recipient.get("city", "")
            state = recipient.get("state", "")
            zip_code = recipient.get("zip", recipient.get("zip_code", ""))

            print(f"{i}. {name}")
            if address:
                print(f"   {address}")
            if city or state or zip_code:
                location = ", ".join(filter(None, [city, state, zip_code]))
                print(f"   {location}")
            print()

        if len(recipients) == 0:
            print("No recipients found in delivery data.")
            print("\nRaw delivery data structure:")
            print(json.dumps(latest, indent=2))
    else:
        print("❌ Could not retrieve delivery information.")
        print("\nTroubleshooting:")
        print(
            "1. Check if you have any recent orders/deliveries in your Minted account"
        )
        print("2. The API endpoint structure may have changed")
        print(
            "3. Try accessing https://www.minted.com/addressbook/my-account/finalize/0 manually"
        )
        print("   and check the browser's Network tab for API calls")

except Exception as e:
    print(f"❌ Error: {e}", file=sys.stderr)
    import traceback

    traceback.print_exc()
    sys.exit(1)
finally:
    driver.close()
