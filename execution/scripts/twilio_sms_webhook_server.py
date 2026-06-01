#!/usr/bin/env python3
"""
Twilio SMS Webhook Receiver Server

Receives SMS webhook events from Twilio and stores them in local parquet files.
Handles Twilio webhook signature verification.

Usage:
    python scripts/twilio_sms_webhook_server.py [--port 8080] [--host 0.0.0.0]

For local development with Cloudflare Tunnel:
    1. Start tunnel: ./scripts/setup_cloudflare_tunnel_simple.sh 8080
    2. Use tunnel URL as webhook endpoint in Twilio Console
    3. Update phone number SMS URL to point to webhook endpoint
"""

import argparse
import hashlib
import hmac
import logging
import os
import subprocess
import sys
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs

import pandas as pd
from flask import Flask, Response, request

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load Twilio credentials from .env
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

app = Flask(__name__)

from scripts.config import DATA_DIR

# Configure logging
LOGS_DIR = DATA_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
WEBHOOK_LOG_FILE = LOGS_DIR / "twilio_sms_webhook.log"
WEBHOOK_ERROR_LOG_FILE = LOGS_DIR / "twilio_sms_webhook.error.log"

# Data directory
MESSAGES_DIR = DATA_DIR / "messages"
MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
MESSAGES_FILE = MESSAGES_DIR / "messages.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def setup_webhook_logging(debug: bool = False):
    """Configure logging for webhook server."""
    log_level = logging.DEBUG if debug else logging.INFO

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger = logging.getLogger("twilio_sms_webhook")
    logger.setLevel(log_level)
    logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (all levels, with rotation)
    file_handler = RotatingFileHandler(
        WEBHOOK_LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Error file handler (ERROR+ only)
    error_handler = RotatingFileHandler(
        WEBHOOK_ERROR_LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)

    return logger


def verify_twilio_signature(request, auth_token: str) -> bool:
    """
    Verify Twilio webhook signature.

    Twilio signs webhooks with HMAC-SHA1 using your auth token.
    """
    signature = request.headers.get("X-Twilio-Signature")
    if not signature:
        return False

    # Build the URL that Twilio requested
    url = request.url

    # Get POST parameters
    params = {}
    if request.method == "POST":
        if (
            request.content_type
            and "application/x-www-form-urlencoded" in request.content_type
        ):
            params = request.form.to_dict()
        else:
            # Try to parse as form data
            try:
                params = parse_qs(request.get_data(as_text=True))
                params = {
                    k: v[0] if isinstance(v, list) and len(v) == 1 else v
                    for k, v in params.items()
                }
            except Exception:
                pass

    # Sort parameters
    sorted_params = sorted(params.items())

    # Build signature string
    signature_string = url
    for key, value in sorted_params:
        signature_string += key + value

    # Compute signature
    computed_signature = hmac.new(
        auth_token.encode("utf-8"), signature_string.encode("utf-8"), hashlib.sha1
    ).digest()

    # Compare (use constant-time comparison)
    return hmac.compare_digest(
        signature.encode("utf-8"), computed_signature.hexdigest()
    )


def create_snapshot(file_path: Path) -> Path:
    """Create a timestamped snapshot of a parquet file."""
    if not file_path.exists():
        return None

    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    filename = file_path.stem
    snapshot_path = SNAPSHOTS_DIR / f"{filename}-{timestamp}.parquet"

    # Copy file
    import shutil

    shutil.copy2(file_path, snapshot_path)

    return snapshot_path


def send_notification(title: str, message: str, subtitle: str = ""):
    """Send macOS notification that stays on screen until dismissed.

    Uses 'terminal-notifier' if available (supports persistent alerts via system settings),
    otherwise falls back to AppleScript 'display notification'.

    To make notifications persistent:
    1. Install terminal-notifier: brew install terminal-notifier
    2. System Settings > Notifications > terminal-notifier > Alert Style: Persistent
    """
    try:
        # Try terminal-notifier first (supports persistent notifications)
        terminal_notifier = subprocess.run(
            ["which", "terminal-notifier"], capture_output=True, timeout=2
        )

        if terminal_notifier.returncode == 0:
            # Use terminal-notifier for persistent notifications
            cmd = ["terminal-notifier", "-title", title]
            if subtitle:
                cmd.extend(["-subtitle", subtitle])
            cmd.extend(["-message", message])
            cmd.extend(["-group", "twilio_sms"])
            cmd.extend(
                ["-sender", "com.apple.Terminal"]
            )  # Helps with notification grouping
            cmd.extend(["-sound", "Glass"])  # SMS notification sound

            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        # Fallback to AppleScript (notifications will auto-dismiss but remain in Notification Center)
        message_escaped = (
            message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        )
        title_escaped = title.replace("\\", "\\\\").replace('"', '\\"')
        subtitle_escaped = (
            subtitle.replace("\\", "\\\\").replace('"', '\\"') if subtitle else ""
        )

        script = (
            f'display notification "{message_escaped}" with title "{title_escaped}"'
        )
        if subtitle_escaped:
            script += f' subtitle "{subtitle_escaped}"'
        script += ' sound name "Glass"'

        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        # Silently fail - notifications are optional
        pass


def save_message_to_parquet(message_data: dict, logger: logging.Logger) -> bool:
    """Save SMS message to parquet file."""
    try:
        # Create snapshot before modification
        if MESSAGES_FILE.exists():
            snapshot_path = create_snapshot(MESSAGES_FILE)
            if snapshot_path:
                logger.debug(f"Created snapshot: {snapshot_path}")

        # Generate message_id if not provided
        message_id = message_data.get("message_id")
        if not message_id:
            import uuid

            message_id = str(uuid.uuid4())[:16]
            message_data["message_id"] = message_id

        # Prepare data row
        row = {
            "message_id": message_id,
            "twilio_message_sid": message_data.get("MessageSid", ""),
            "direction": "inbound" if message_data.get("From") else "outbound",
            "from_number": message_data.get("From", ""),
            "to_number": message_data.get("To", ""),
            "body": message_data.get("Body", ""),
            "status": message_data.get("MessageStatus", "received"),
            "error_code": message_data.get("ErrorCode", ""),
            "error_message": message_data.get("ErrorMessage", ""),
            "num_media": int(message_data.get("NumMedia", 0)),
            "price": message_data.get("Price", ""),
            "price_unit": message_data.get("PriceUnit", ""),
            "date_sent": (
                pd.to_datetime(message_data.get("DateSent"), errors="coerce")
                if message_data.get("DateSent")
                else None
            ),
            "date_created": (
                pd.to_datetime(message_data.get("DateCreated"), errors="coerce")
                if message_data.get("DateCreated")
                else datetime.utcnow()
            ),
            "date_updated": (
                pd.to_datetime(message_data.get("DateUpdated"), errors="coerce")
                if message_data.get("DateUpdated")
                else datetime.utcnow()
            ),
            "account_sid": message_data.get("AccountSid", ""),
            "import_date": date.today(),
            "import_source": "webhook",
        }

        # Read existing data or create new
        if MESSAGES_FILE.exists():
            df = pd.read_parquet(MESSAGES_FILE)
            # Check for duplicates by twilio_message_sid
            if row["twilio_message_sid"] in df["twilio_message_sid"].values:
                logger.debug(
                    f"Message {row['twilio_message_sid']} already exists, skipping"
                )
                return True
        else:
            df = pd.DataFrame()

        # Append new row
        new_df = pd.DataFrame([row])
        df = pd.concat([df, new_df], ignore_index=True)

        # Write back
        df.to_parquet(MESSAGES_FILE, index=False)

        logger.info(
            f"Saved message {row['twilio_message_sid']} from {row['from_number']} to {row['to_number']}"
        )

        # Send macOS notification for inbound messages
        if row["direction"] == "inbound":
            from_number_display = row["from_number"]
            body_preview = (
                row["body"][:100] + "..." if len(row["body"]) > 100 else row["body"]
            )

            send_notification(
                title="📱 New SMS",
                subtitle=f"From: {from_number_display}",
                message=body_preview,
            )

        return True

    except Exception as e:
        logger.error(f"Error saving message to parquet: {e}", exc_info=True)
        return False


@app.route("/webhook/twilio/sms", methods=["POST"])
def twilio_sms_webhook():
    """Handle Twilio SMS webhook events."""
    logger = logging.getLogger("twilio_sms_webhook")

    # Verify signature if auth token is available
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if auth_token:
        if not verify_twilio_signature(request, auth_token):
            logger.warning("Invalid Twilio signature - request may be spoofed")
            # Continue anyway for now, but log the warning
    else:
        logger.warning("TWILIO_AUTH_TOKEN not set - skipping signature verification")

    # Get form data (Twilio sends as application/x-www-form-urlencoded)
    message_data = request.form.to_dict()

    logger.info(
        f"Received SMS webhook: From={message_data.get('From')} To={message_data.get('To')} Body={message_data.get('Body', '')[:50]}"
    )

    # Save to parquet
    if save_message_to_parquet(message_data, logger):
        # Return TwiML response (empty response is OK)
        return (
            Response(
                '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                mimetype="application/xml",
            ),
            200,
        )
    else:
        logger.error("Failed to save message")
        return (
            Response(
                '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                mimetype="application/xml",
            ),
            500,
        )


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "twilio_sms_webhook"}, 200


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Twilio SMS Webhook Server")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    # Setup logging
    logger = setup_webhook_logging(debug=args.debug)

    logger.info("=" * 60)
    logger.info("Twilio SMS Webhook Server")
    logger.info("=" * 60)
    logger.info(f"Listening on {args.host}:{args.port}")
    logger.info(f"Webhook endpoint: http://{args.host}:{args.port}/webhook/twilio/sms")
    logger.info(f"Messages will be saved to: {MESSAGES_FILE}")
    logger.info("=" * 60)

    # Run Flask app
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
