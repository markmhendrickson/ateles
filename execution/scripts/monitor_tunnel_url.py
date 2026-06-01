#!/usr/bin/env python3
"""
Monitor Cloudflare Tunnel URL and automatically update Twilio webhook when URL changes.

This script watches the tunnel logs for URL changes and updates the Twilio phone number
webhook URL automatically. Runs as a background service via LaunchAgent.
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from scripts.config import get_data_dir

# Configuration
DATA_DIR = get_data_dir()
LOGS_DIR = DATA_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
TUNNEL_LOG = LOGS_DIR / "cloudflare_twilio_sms_tunnel.log"
TUNNEL_ERROR_LOG = LOGS_DIR / "cloudflare_twilio_sms_tunnel.error.log"
STATE_FILE = LOGS_DIR / "tunnel_url_state.json"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Configure logging
MONITOR_LOG = LOGS_DIR / "tunnel_url_monitor.log"


def setup_logging(debug: bool = False):
    """Configure logging."""
    log_level = logging.DEBUG if debug else logging.INFO

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    logger = logging.getLogger("tunnel_url_monitor")
    logger.setLevel(log_level)
    logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler
    file_handler = logging.FileHandler(MONITOR_LOG)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def get_tunnel_url() -> str | None:
    """Extract current tunnel URL from logs."""
    log_files = [TUNNEL_LOG, TUNNEL_ERROR_LOG]

    for log_file in log_files:
        if not log_file.exists():
            continue

        try:
            with open(log_file) as f:
                content = f.read()
                # Look for Cloudflare tunnel URL
                matches = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", content)
                if matches:
                    return matches[-1]  # Return most recent URL
        except Exception:
            continue

    return None


def get_last_known_url() -> str | None:
    """Get last known URL from state file."""
    if not STATE_FILE.exists():
        return None

    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
            return state.get("url")
    except Exception:
        return None


def save_url_state(url: str):
    """Save current URL to state file."""
    state = {"url": url, "last_updated": time.time()}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def update_twilio_webhook_url(webhook_url: str, logger: logging.Logger) -> bool:
    """Update Twilio phone number webhook URL using Twilio API."""
    import os

    try:
        from twilio.rest import Client

        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        phone_number = os.getenv("TWILIO_PHONE_NUMBER", "+16503198857")

        if not account_sid or not auth_token:
            logger.error("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set")
            return False

        client = Client(account_sid, auth_token)

        # Find phone number
        incoming_phone_numbers = client.incoming_phone_numbers.list(
            phone_number=phone_number
        )

        if not incoming_phone_numbers:
            logger.error(f"Phone number {phone_number} not found in Twilio account")
            return False

        phone_number_sid = incoming_phone_numbers[0].sid

        # Update webhook URL
        client.incoming_phone_numbers(phone_number_sid).update(
            sms_url=webhook_url, sms_method="POST"
        )

        logger.info(f"✓ Updated Twilio webhook URL to: {webhook_url}")
        return True

    except Exception as e:
        logger.error(f"Failed to update Twilio webhook URL: {e}", exc_info=True)
        return False


def monitor_tunnel_url(check_interval: int = 30, logger: logging.Logger = None):
    """Monitor tunnel URL and update Twilio when it changes."""
    if logger is None:
        logger = setup_logging()

    logger.info("Starting tunnel URL monitor...")
    logger.info(f"Checking every {check_interval} seconds")
    logger.info(f"State file: {STATE_FILE}")

    last_known_url = get_last_known_url()
    if last_known_url:
        logger.info(f"Last known URL: {last_known_url}")
    else:
        logger.info("No previous URL found - will update on first detection")

    consecutive_failures = 0
    max_failures = 10

    while True:
        try:
            current_url = get_tunnel_url()

            if current_url:
                consecutive_failures = 0

                if current_url != last_known_url:
                    logger.info(f"Tunnel URL changed: {last_known_url} → {current_url}")

                    webhook_url = f"{current_url}/webhook/twilio/sms"

                    if update_twilio_webhook_url(webhook_url, logger):
                        save_url_state(current_url)
                        last_known_url = current_url
                        logger.info("✓ Successfully updated Twilio webhook URL")
                    else:
                        logger.warning(
                            "⚠ Failed to update Twilio webhook URL - will retry on next check"
                        )
                else:
                    logger.debug(f"Tunnel URL unchanged: {current_url}")
            else:
                consecutive_failures += 1
                if consecutive_failures <= max_failures:
                    logger.debug(
                        f"Tunnel URL not found in logs (attempt {consecutive_failures}/{max_failures})"
                    )
                else:
                    logger.warning(
                        f"Tunnel URL not found after {max_failures} attempts - tunnel may not be running"
                    )
                    consecutive_failures = 0  # Reset counter to avoid spam

            time.sleep(check_interval)

        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
            break
        except Exception as e:
            logger.error(f"Error in monitor loop: {e}", exc_info=True)
            time.sleep(check_interval)


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Monitor Cloudflare Tunnel URL and update Twilio webhook"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Check interval in seconds (default: 30)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--once", action="store_true", help="Check once and exit (for testing)"
    )
    args = parser.parse_args()

    logger = setup_logging(debug=args.debug)

    if args.once:
        # One-time check
        current_url = get_tunnel_url()
        last_known_url = get_last_known_url()

        if current_url:
            logger.info(f"Current tunnel URL: {current_url}")
            if current_url != last_known_url:
                logger.info(f"URL changed from: {last_known_url}")
                webhook_url = f"{current_url}/webhook/twilio/sms"
                if update_twilio_webhook_url(webhook_url, logger):
                    save_url_state(current_url)
            else:
                logger.info("URL unchanged")
        else:
            logger.warning("Tunnel URL not found in logs")
    else:
        # Continuous monitoring
        monitor_tunnel_url(check_interval=args.interval, logger=logger)


if __name__ == "__main__":
    main()
