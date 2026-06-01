#!/usr/bin/env python3
"""
Export contacts from Minted.com Address Assistant.

This script uses Selenium to authenticate with Minted.com and exports all contacts
from the Address Assistant to CSV and XLSX formats in the data/imports directory.

Usage:
    python execution/scripts/export_minted_contacts.py [email] [password]

    Credentials are retrieved from 1Password by default (item title: "Minted.com").
    Fallback options:
    - Command-line arguments: python export_minted_contacts.py <email> <password>
    - Environment variables: minted_email, minted_password
    - Interactive prompt

The exported CSV can then be imported using:
    python execution/scripts/import_data.py contacts data/imports/YYYY-MM-DD_minted_contacts.csv --source minted
"""

import os
import sys
from datetime import datetime
from pathlib import Path
from time import sleep

import pandas as pd
import requests
from dotenv import load_dotenv
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

# Get project root (parent of scripts directory)
PROJECT_ROOT = Path(__file__).parent.parent.parent

# Try to import credential utility (optional - falls back to env vars if not available)
try:
    from scripts.credentials import get_credential, get_credential_by_domain

    HAS_CREDENTIALS_MODULE = True
except ImportError:
    HAS_CREDENTIALS_MODULE = False

# Load .env from project root (fallback)
load_dotenv(PROJECT_ROOT / ".env")

# Webdriver options; set to headless
options = Options()
options.add_argument("--headless")
service = Service(ChromeDriverManager().install())
driver = Chrome(service=service, options=options)

# URL for minted login page
URL = "https://www.minted.com/login"

# Get credentials: try 1Password first, then fall back to env vars, then prompt
if len(sys.argv) >= 3:
    # Command-line arguments take precedence
    minted_email = sys.argv[1]
    minted_password = sys.argv[2]
else:
    minted_email = None
    minted_password = None

    # Try 1Password first
    if HAS_CREDENTIALS_MODULE:
        try:
            minted_email, minted_password = get_credential_by_domain("minted.com")
        except Exception:
            # Fall back to other methods if 1Password lookup fails
            pass

    # Fall back to environment variables
    if not minted_email:
        try:
            minted_email = os.environ["minted_email"]
        except KeyError:
            pass

    if not minted_password:
        try:
            minted_password = os.environ["minted_password"]
        except KeyError:
            pass

    # Final fallback: interactive prompt
    if not minted_email:
        minted_email = input("Enter your minted.com email address: ")
    if not minted_password:
        import getpass

        minted_password = getpass.getpass("Enter your minted.com password: ")

try:
    driver.get(URL)

    # Selenium handles login form
    email_elem = driver.find_element(By.XPATH, '//*[@id="identifierMNTD"]')
    email_elem.send_keys(minted_email)
    password_elem = driver.find_element(By.XPATH, '//*[@id="password"]')
    password_elem.send_keys(minted_password)
    login_submit = driver.find_element(
        By.XPATH, '//*[@id="__next"]/div[3]/div/form/div[2]/div[1]/button'
    )
    login_submit.click()

    sleep(5)  # to load JS and be nice

    # Obtain cookies from selenium session
    cookies = {c["name"]: c["value"] for c in driver.get_cookies()}

    # Request address book contents as json
    response = requests.get(
        "https://addressbook.minted.com/api/contacts/contacts/?format=json",
        cookies=cookies,
        timeout=300,
    )
    response.raise_for_status()
    listings = response.json()

    # Create dataframe to hold addresses
    address_book = pd.DataFrame(listings)

    # Export to excel and csv in $DATA_DIR/imports directory
    timestamp = datetime.now().strftime("%Y-%m-%d")
    from scripts.config import get_data_dir

    imports_dir = get_data_dir() / "imports"
    imports_dir.mkdir(parents=True, exist_ok=True)

    excel_path = imports_dir / f"{timestamp}_minted_contacts.xlsx"
    csv_path = imports_dir / f"{timestamp}_minted_contacts.csv"

    address_book.to_excel(excel_path, index=False)
    address_book.to_csv(csv_path, index=False)

    print(f"✅ Successfully exported {len(address_book)} contacts")
    print(f"   CSV: {csv_path}")
    print(f"   XLSX: {excel_path}")
    print("\nTo import into contacts database:")
    print(
        f"   python execution/scripts/import_data.py contacts {csv_path} --source minted"
    )

except Exception as e:
    print(f"❌ Error exporting contacts: {e}", file=sys.stderr)
    import traceback

    traceback.print_exc()
    sys.exit(1)
finally:
    # Close selenium webdriver
    driver.close()
