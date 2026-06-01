#!/usr/bin/env python3
"""
Debug Twilio SMS-to-Email Forwarding Configuration

Checks phone number configuration, webhooks, and recent SMS logs to diagnose
why SMS messages aren't being forwarded to email.
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from twilio.base.exceptions import TwilioException, TwilioRestException
    from twilio.rest import Client
except ImportError:
    print("ERROR: Twilio Python SDK not installed.")
    print("Install it with: pip install twilio")
    sys.exit(1)

from dotenv import load_dotenv

# Load .env from project root
PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Phone number to debug
PHONE_NUMBER = "+16503198857"


def get_twilio_client(
    account_sid: str | None = None, auth_token: str | None = None
) -> Client | None:
    """Get Twilio client from environment variables or arguments."""
    account_sid = account_sid or os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = auth_token or os.getenv("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        print("ERROR: Twilio credentials not found.")
        print("\nUsage options:")
        print("  1. Set in .env file:")
        print("     TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        print("     TWILIO_AUTH_TOKEN=your_auth_token_here")
        print("\n  2. Export as environment variables:")
        print("     export TWILIO_ACCOUNT_SID=AC...")
        print("     export TWILIO_AUTH_TOKEN=...")
        print("\n  3. Pass as command-line arguments:")
        print(
            "     python execution/scripts/debug_twilio_sms.py --account-sid AC... --auth-token ..."
        )
        return None

    try:
        client = Client(account_sid, auth_token)
        # Test connection by fetching account info
        account = client.api.accounts(account_sid).fetch()
        print(f"✓ Connected to Twilio account: {account.friendly_name}")
        return client
    except TwilioException as e:
        print(f"ERROR: Failed to connect to Twilio: {e}")
        return None


def check_phone_number(client: Client, phone_number: str) -> dict[str, Any] | None:
    """Check phone number configuration."""
    print(f"\n{'=' * 60}")
    print(f"Checking phone number: {phone_number}")
    print(f"{'=' * 60}")

    try:
        # Fetch phone number details
        incoming_numbers = client.incoming_phone_numbers.list(phone_number=phone_number)

        if not incoming_numbers:
            print(f"❌ Phone number {phone_number} not found in this Twilio account")
            print("\nPossible reasons:")
            print("  - Number is in a different Twilio account")
            print("  - Number has been released/ported away")
            print("  - Number format is incorrect (should include country code)")
            return None

        number = incoming_numbers[0]
        print(f"✓ Found phone number: {number.phone_number}")
        print(f"  Friendly Name: {number.friendly_name or 'N/A'}")
        print(f"  Status: {number.status}")

        # Check messaging configuration
        print("\n📱 Messaging Configuration:")
        print(f"  SMS URL: {number.sms_url or '❌ NOT CONFIGURED'}")
        print(f"  SMS Method: {number.sms_method or 'N/A'}")
        print(
            f"  Status Callback URL: {getattr(number, 'status_callback', None) or '❌ NOT CONFIGURED'}"
        )
        print(
            f"  Status Callback Method: {getattr(number, 'status_callback_method', None) or 'N/A'}"
        )

        # Check voice configuration (for completeness)
        print("\n📞 Voice Configuration:")
        print(f"  Voice URL: {number.voice_url or '❌ NOT CONFIGURED'}")
        print(f"  Voice Method: {number.voice_method or 'N/A'}")

        # Check capabilities
        print("\n🔧 Capabilities:")
        print(
            f"  SMS Enabled: {'✓' if number.capabilities.get('SMS', False) else '❌'}"
        )
        print(
            f"  Voice Enabled: {'✓' if number.capabilities.get('Voice', False) else '❌'}"
        )
        print(
            f"  MMS Enabled: {'✓' if number.capabilities.get('MMS', False) else '❌'}"
        )

        return {
            "number": number,
            "sms_url": number.sms_url,
            "sms_method": number.sms_method,
            "status_callback_url": getattr(number, "status_callback", None),
            "capabilities": number.capabilities,
        }

    except TwilioRestException as e:
        print(f"❌ Error fetching phone number: {e}")
        return None


def check_recent_messages(client: Client, phone_number: str, hours: int = 24) -> None:
    """Check recent SMS messages for this phone number."""
    print(f"\n{'=' * 60}")
    print(f"Recent SMS Messages (last {hours} hours)")
    print(f"{'=' * 60}")

    try:
        # Get messages from the last N hours
        since = datetime.utcnow() - timedelta(hours=hours)

        # Get messages TO this number (incoming)
        incoming = client.messages.list(to=phone_number, date_sent_after=since)

        # Get messages FROM this number (outgoing)
        outgoing = client.messages.list(from_=phone_number, date_sent_after=since)

        all_messages = list(incoming) + list(outgoing)
        all_messages.sort(key=lambda m: m.date_sent or datetime.min, reverse=True)

        if not all_messages:
            print(f"⚠️  No messages found in the last {hours} hours")
            print("\nThis could mean:")
            print("  - No SMS messages were sent to this number")
            print("  - Messages are older than the time window")
            print("  - Messages are in a different Twilio account")
            return

        print(f"✓ Found {len(all_messages)} message(s)\n")

        for i, msg in enumerate(all_messages[:10], 1):  # Show last 10
            direction = "→ INCOMING" if msg.to == phone_number else "← OUTGOING"
            status_icon = (
                "✓"
                if msg.status == "delivered"
                else "⚠️"
                if msg.status == "failed"
                else "⏳"
            )

            print(f"{i}. {status_icon} {direction}")
            print(f"   From: {msg.from_}")
            print(f"   To: {msg.to}")
            print(f"   Status: {msg.status}")
            print(f"   Date: {msg.date_sent}")
            if msg.error_code:
                print(f"   ⚠️  Error Code: {msg.error_code}")
                print(f"   ⚠️  Error Message: {msg.error_message}")
            if msg.body:
                body_preview = msg.body[:50] + "..." if len(msg.body) > 50 else msg.body
                print(f"   Body: {body_preview}")
            print()

        if len(all_messages) > 10:
            print(f"... and {len(all_messages) - 10} more messages")

    except TwilioRestException as e:
        print(f"❌ Error fetching messages: {e}")


def check_webhook_accessibility(sms_url: str | None) -> None:
    """Check if webhook URL is accessible."""
    if not sms_url:
        print("\n⚠️  No SMS webhook URL configured - SMS forwarding cannot work")
        return

    print(f"\n{'=' * 60}")
    print("Testing Webhook URL Accessibility")
    print(f"{'=' * 60}")
    print(f"Webhook URL: {sms_url}")

    import urllib.error
    import urllib.request

    try:
        # Try to access the webhook URL
        req = urllib.request.Request(sms_url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as response:
            status = response.getcode()
            print(f"✓ Webhook URL is accessible (HTTP {status})")
    except urllib.error.HTTPError as e:
        print(f"⚠️  Webhook returned HTTP {e.code}: {e.reason}")
        print("   This might be expected if the endpoint requires POST")
    except urllib.error.URLError as e:
        print(f"❌ Webhook URL is NOT accessible: {e.reason}")
        print("\nPossible issues:")
        print("  - Webhook server is not running")
        print("  - URL is incorrect")
        print("  - Firewall/network blocking access")
        print("  - Webhook endpoint requires authentication")
    except Exception as e:
        print(f"❌ Error testing webhook: {e}")


def main():
    """Main debugging function."""
    import argparse

    parser = argparse.ArgumentParser(description="Debug Twilio SMS-to-Email Forwarding")
    parser.add_argument("--account-sid", help="Twilio Account SID")
    parser.add_argument("--auth-token", help="Twilio Auth Token")
    args = parser.parse_args()

    print("Twilio SMS-to-Email Forwarding Debug Tool")
    print("=" * 60)

    client = get_twilio_client(args.account_sid, args.auth_token)
    if not client:
        return 1

    # Check phone number configuration
    config = check_phone_number(client, PHONE_NUMBER)

    if config:
        # Check webhook accessibility
        check_webhook_accessibility(config.get("sms_url"))

        # Check recent messages
        check_recent_messages(client, PHONE_NUMBER, hours=24)

        # Summary and recommendations
        print(f"\n{'=' * 60}")
        print("Summary & Recommendations")
        print(f"{'=' * 60}")

        if not config.get("sms_url"):
            print("\n❌ CRITICAL: No SMS webhook URL configured!")
            print("   To enable SMS-to-email forwarding:")
            print("   1. Configure a webhook URL in Twilio Console")
            print("   2. Webhook should handle POST requests with SMS data")
            print("   3. Webhook should forward SMS to email")
        elif not config.get("capabilities", {}).get("SMS"):
            print("\n❌ CRITICAL: SMS capability not enabled for this number!")
            print("   Enable SMS in Twilio Console → Phone Numbers → [Your Number]")
        else:
            print("\n✓ Phone number configuration looks OK")
            print("   If SMS forwarding still doesn't work:")
            print("   1. Check webhook server is running and accessible")
            print("   2. Check webhook logs for errors")
            print("   3. Verify email service (SendGrid/SMTP) is configured")
            print("   4. Check email delivery logs")

    return 0


if __name__ == "__main__":
    sys.exit(main())
