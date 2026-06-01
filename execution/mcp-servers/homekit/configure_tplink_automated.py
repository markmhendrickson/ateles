#!/usr/bin/env python3
"""
TP-Link Router Automated Configuration Script

This script uses browser automation to configure TP-Link routers for HomeKit mDNS.
Requires: pip install selenium

Usage:
    python3 configure_tplink_automated.py
"""

import sys
import time

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def check_selenium():
    """Check if Selenium is installed."""
    try:
        from selenium import webdriver

        return True
    except ImportError:
        print("❌ Selenium not installed. Install with:")
        print("   pip install selenium")
        print("   brew install chromedriver  # or geckodriver for Firefox")
        return False


def configure_tplink_router(username, password):
    """Configure TP-Link router for HomeKit mDNS."""

    if not check_selenium():
        return False

    print("Starting browser automation...")
    print("Note: This may not work with all TP-Link router models.")
    print("Router interface varies by model and firmware version.")
    print("")

    # Initialize browser (Chrome)
    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        driver = webdriver.Chrome(options=options)
    except Exception as e:
        print(f"❌ Could not start browser: {e}")
        print("Try installing chromedriver: brew install chromedriver")
        return False

    try:
        # Navigate to router
        print("Connecting to router...")
        driver.get("http://192.168.0.1")
        time.sleep(2)

        # Login
        print("Logging in...")
        try:
            username_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "userName"))
            )
            password_field = driver.find_element(By.ID, "pcPassword")

            username_field.send_keys(username)
            password_field.send_keys(password)

            login_button = driver.find_element(By.ID, "loginBtn")
            login_button.click()

            time.sleep(3)
        except TimeoutException:
            print("❌ Could not find login form. Router interface may be different.")
            print("   Please configure manually using the interactive script.")
            return False

        # Navigate to IGMP Snooping
        print("Configuring IGMP Snooping...")
        try:
            # Try to find Advanced menu
            advanced_menu = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//a[contains(text(), 'Advanced')]")
                )
            )
            advanced_menu.click()
            time.sleep(1)

            # Try to find Network submenu
            network_menu = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//a[contains(text(), 'Network')]")
                )
            )
            network_menu.click()
            time.sleep(1)

            # Try to find IGMP Snooping
            igmp_menu = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'IGMP')]"))
            )
            igmp_menu.click()
            time.sleep(1)

            # Enable IGMP Snooping
            igmp_toggle = driver.find_element(By.NAME, "igmpSnooping")
            if not igmp_toggle.is_selected():
                igmp_toggle.click()

            # Save
            save_button = driver.find_element(By.ID, "saveBtn")
            save_button.click()
            time.sleep(2)

            print("✅ IGMP Snooping enabled")

        except (TimeoutException, NoSuchElementException) as e:
            print(f"⚠️  Could not configure IGMP Snooping: {e}")
            print("   Router interface may be different. Please configure manually.")

        # Navigate to UPnP
        print("Configuring UPnP...")
        try:
            # Go back to Advanced
            advanced_menu = driver.find_element(
                By.XPATH, "//a[contains(text(), 'Advanced')]"
            )
            advanced_menu.click()
            time.sleep(1)

            # Find NAT Forwarding
            nat_menu = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//a[contains(text(), 'NAT Forwarding')]")
                )
            )
            nat_menu.click()
            time.sleep(1)

            # Find UPnP
            upnp_menu = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'UPnP')]"))
            )
            upnp_menu.click()
            time.sleep(1)

            # Enable UPnP
            upnp_toggle = driver.find_element(By.NAME, "upnpEnable")
            if not upnp_toggle.is_selected():
                upnp_toggle.click()

            # Save
            save_button = driver.find_element(By.ID, "saveBtn")
            save_button.click()
            time.sleep(2)

            print("✅ UPnP enabled")

        except (TimeoutException, NoSuchElementException) as e:
            print(f"⚠️  Could not configure UPnP: {e}")
            print("   Router interface may be different. Please configure manually.")

        print("")
        print("==========================================")
        print("Configuration complete!")
        print("==========================================")
        print("")
        print("⚠️  IMPORTANT: You need to restart the router manually:")
        print("   1. Navigate to: System Tools → Reboot")
        print("   2. Click 'Reboot'")
        print("   3. Wait 2-3 minutes")
        print("")
        print(
            "The browser will stay open for 30 seconds so you can restart the router."
        )
        print("Press Ctrl+C to close it earlier.")

        time.sleep(30)

        return True

    except Exception as e:
        print(f"❌ Error during configuration: {e}")
        return False

    finally:
        driver.quit()


if __name__ == "__main__":
    print("==========================================")
    print("TP-Link Router Automated Configuration")
    print("==========================================")
    print("")
    print("⚠️  WARNING: This script uses browser automation.")
    print("   It may not work with all TP-Link router models.")
    print("   Router interfaces vary by model and firmware.")
    print("")
    print("For best results, use the interactive script instead:")
    print("   ./configure_tplink_interactive.sh")
    print("")

    response = input("Continue with automated configuration? (y/N): ")
    if response.lower() != "y":
        print("Exiting. Use the interactive script instead.")
        sys.exit(0)

    username = input("Router admin username (default: admin): ").strip() or "admin"
    password = input("Router admin password: ").strip()

    if not password:
        print("❌ Password required")
        sys.exit(1)

    configure_tplink_router(username, password)
