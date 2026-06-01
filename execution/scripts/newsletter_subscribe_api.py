#!/usr/bin/env python3
"""
Newsletter Subscription API Handler

Handles newsletter subscription form submissions:
- Validates email address
- Stores subscriber in database (user-owned, sovereignty-aligned)
- Stores optional ICP mapping survey responses
- Sends confirmation email via email delivery API (Resend/SendGrid/Mailgun)
- Returns JSON response

Sovereignty-aligned: All data stored in user-owned database, not third-party platform.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# Add parent directory to path for imports
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(_repo_root / ".env")

# Configuration
EMAIL_DELIVERY_API = os.getenv(
    "EMAIL_DELIVERY_API", "resend"
)  # 'resend', 'sendgrid', or 'mailgun'
EMAIL_DELIVERY_API_KEY = os.getenv("EMAIL_DELIVERY_API_KEY", "")
FROM_EMAIL = os.getenv("NEWSLETTER_FROM_EMAIL", "newsletter@markmhendrickson.com")
NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Mark Hendrickson Newsletter")

# Database path (user-owned, sovereignty-aligned)
# TODO: Replace with actual database connection (PostgreSQL, SQLite, etc.)
# For now, using JSON file as placeholder
DB_PATH = Path(os.getenv("NEWSLETTER_DB_PATH", "data/newsletter_subscribers.json"))


def validate_email(email: str) -> bool:
    """Validate email address format."""
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


def load_subscribers() -> list[dict]:
    """Load subscribers from database."""
    if DB_PATH.exists():
        with open(DB_PATH) as f:
            return json.load(f)
    return []


def save_subscriber(email: str, survey: dict, subscribed_at: str) -> None:
    """Save subscriber to database (user-owned, sovereignty-aligned)."""
    subscribers = load_subscribers()

    # Check if email already exists
    existing = next((s for s in subscribers if s["email"] == email), None)
    if existing:
        # Update existing subscriber
        existing["survey"] = survey
        existing["updated_at"] = subscribed_at
    else:
        # Add new subscriber
        subscribers.append(
            {
                "email": email,
                "survey": survey,
                "subscribed_at": subscribed_at,
                "updated_at": subscribed_at,
                "status": "subscribed",
                "unsubscribed_at": None,
            }
        )

    # Ensure directory exists
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Save to database
    with open(DB_PATH, "w") as f:
        json.dump(subscribers, f, indent=2)


def send_confirmation_email(email: str, email_api: str, api_key: str) -> bool:
    """Send confirmation email via email delivery API."""
    if not api_key:
        print(
            "Warning: EMAIL_DELIVERY_API_KEY not set, skipping email send",
            file=sys.stderr,
        )
        return False

    confirmation_url = f"https://markmhendrickson.com/newsletter/confirm?email={email}&token=TOKEN"  # TODO: Generate token

    if email_api == "resend":
        return send_via_resend(email, api_key, confirmation_url)
    elif email_api == "sendgrid":
        return send_via_sendgrid(email, api_key, confirmation_url)
    elif email_api == "mailgun":
        return send_via_mailgun(email, api_key, confirmation_url)
    else:
        print(
            f"Warning: Unknown email API '{email_api}', skipping email send",
            file=sys.stderr,
        )
        return False


def send_via_resend(email: str, api_key: str, confirmation_url: str) -> bool:
    """Send email via Resend API."""
    url = "https://api.resend.com/emails"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    payload = {
        "from": FROM_EMAIL,
        "to": [email],
        "subject": f"Confirm your subscription to {NEWSLETTER_NAME}",
        "html": f"""
        <html>
        <body>
            <h2>Welcome to {NEWSLETTER_NAME}</h2>
            <p>Thank you for subscribing! Please confirm your email address by clicking the link below:</p>
            <p><a href="{confirmation_url}">Confirm Subscription</a></p>
            <p>If you didn't subscribe, you can safely ignore this email.</p>
            <hr>
            <p style="font-size: 0.9em; color: #666;">
                Privacy-first: Your data is stored in a user-owned database, not a third-party platform.
            </p>
        </body>
        </html>
        """,
        "text": f"""
        Welcome to {NEWSLETTER_NAME}

        Thank you for subscribing! Please confirm your email address by visiting:
        {confirmation_url}

        If you didn't subscribe, you can safely ignore this email.

        Privacy-first: Your data is stored in a user-owned database, not a third-party platform.
        """,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending email via Resend: {e}", file=sys.stderr)
        return False


def send_via_sendgrid(email: str, api_key: str, confirmation_url: str) -> bool:
    """Send email via SendGrid API."""
    url = "https://api.sendgrid.com/v3/mail/send"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    payload = {
        "personalizations": [{"to": [{"email": email}]}],
        "from": {"email": FROM_EMAIL},
        "subject": f"Confirm your subscription to {NEWSLETTER_NAME}",
        "content": [
            {
                "type": "text/html",
                "value": f"""
            <html>
            <body>
                <h2>Welcome to {NEWSLETTER_NAME}</h2>
                <p>Thank you for subscribing! Please confirm your email address by clicking the link below:</p>
                <p><a href="{confirmation_url}">Confirm Subscription</a></p>
                <p>If you didn't subscribe, you can safely ignore this email.</p>
                <hr>
                <p style="font-size: 0.9em; color: #666;">
                    Privacy-first: Your data is stored in a user-owned database, not a third-party platform.
                </p>
            </body>
            </html>
            """,
            }
        ],
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending email via SendGrid: {e}", file=sys.stderr)
        return False


def send_via_mailgun(email: str, api_key: str, confirmation_url: str) -> bool:
    """Send email via Mailgun API."""
    # Extract domain from FROM_EMAIL
    domain = FROM_EMAIL.split("@")[1]
    url = f"https://api.mailgun.net/v3/{domain}/messages"

    payload = {
        "from": FROM_EMAIL,
        "to": [email],
        "subject": f"Confirm your subscription to {NEWSLETTER_NAME}",
        "html": f"""
        <html>
        <body>
            <h2>Welcome to {NEWSLETTER_NAME}</h2>
            <p>Thank you for subscribing! Please confirm your email address by clicking the link below:</p>
            <p><a href="{confirmation_url}">Confirm Subscription</a></p>
            <p>If you didn't subscribe, you can safely ignore this email.</p>
            <hr>
            <p style="font-size: 0.9em; color: #666;">
                Privacy-first: Your data is stored in a user-owned database, not a third-party platform.
            </p>
        </body>
        </html>
        """,
        "text": f"""
        Welcome to {NEWSLETTER_NAME}

        Thank you for subscribing! Please confirm your email address by visiting:
        {confirmation_url}

        If you didn't subscribe, you can safely ignore this email.

        Privacy-first: Your data is stored in a user-owned database, not a third-party platform.
        """,
    }

    try:
        response = requests.post(url, auth=("api", api_key), data=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending email via Mailgun: {e}", file=sys.stderr)
        return False


def map_icp_tier(survey: dict) -> Optional[str]:
    """Map survey responses to ICP tier based on Neotoma ICP priority tiers."""
    role = survey.get("role")
    ai_usage = survey.get("ai_usage", [])
    crypto = survey.get("crypto")
    team_size = survey.get("team_size")

    # Tier 1: AI-Native Individual Operators, High-Context Knowledge Workers, AI-Native Founders & Small Teams
    if role in ["ai-native-operator", "knowledge-worker"]:
        return "tier_1"
    if role == "founder" and team_size in ["solo", "2-20"]:
        return "tier_1"
    if role == "developer" and "cursor-raycast" in ai_usage:
        return "tier_1"

    # Tier 2: Hybrid Product Teams, Cross-Functional Operational Teams, Developer Integrators, AI Tool Integrators
    if role in ["product-ops", "developer"]:
        return "tier_2"
    if team_size in ["2-20", "21-200"]:
        return "tier_2"

    # Tier 3: Cross-Border Solopreneurs, Multi-System Information Workers, High-Entropy Households
    if role == "solopreneur":
        return "tier_3"

    # Tier 4: Crypto-Native Power Users, High-Net-Worth Individuals, Multi-Jurisdiction Residents, Startup Founders with Equity Docs
    if role == "crypto-power-user" or crypto == "actively":
        return "tier_4"

    return None


def handle_update_survey(request_data: dict) -> tuple:
    """Handle newsletter survey update request (from confirmation page)."""
    email = request_data.get("email", "").strip().lower()
    survey = request_data.get("survey", {})

    # Validate email
    if not email:
        return {"error": "Email address is required"}, 400

    if not validate_email(email):
        return {"error": "Invalid email address format"}, 400

    subscribers = load_subscribers()
    existing = next((s for s in subscribers if s["email"] == email), None)

    if not existing:
        return {"error": "Subscriber not found"}, 404

    # Map ICP tier if survey has relevant fields
    icp_tier = map_icp_tier(survey)
    if icp_tier:
        survey["icp_tier"] = icp_tier

    # Merge survey into existing
    existing["survey"] = {**existing.get("survey", {}), **survey}
    existing["updated_at"] = datetime.utcnow().isoformat() + "Z"

    # Ensure directory exists and save
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DB_PATH, "w") as f:
        json.dump(subscribers, f, indent=2)

    return {"success": True, "message": "Survey updated"}, 200


def handle_subscribe(request_data: dict) -> tuple:
    """Handle newsletter subscription request."""
    email = request_data.get("email", "").strip().lower()
    survey = request_data.get("survey", {})

    # Validate email
    if not email:
        return {"error": "Email address is required"}, 400

    if not validate_email(email):
        return {"error": "Invalid email address format"}, 400

    # Map ICP tier
    icp_tier = map_icp_tier(survey)
    if icp_tier:
        survey["icp_tier"] = icp_tier

    # Save subscriber (user-owned database, sovereignty-aligned)
    subscribed_at = datetime.utcnow().isoformat() + "Z"
    save_subscriber(email, survey, subscribed_at)

    # Send confirmation email (optional, can fail without blocking subscription)
    email_sent = send_confirmation_email(
        email, EMAIL_DELIVERY_API, EMAIL_DELIVERY_API_KEY
    )

    return {
        "success": True,
        "message": "Successfully subscribed",
        "email": email,
        "icp_tier": icp_tier,
        "email_sent": email_sent,
    }, 200


# Main handler (for serverless functions or API endpoints)
if __name__ == "__main__":
    # Read JSON from stdin (for serverless function invocation)
    try:
        request_data = json.load(sys.stdin)
        result, status_code = handle_subscribe(request_data)
        print(json.dumps(result))
        sys.exit(0 if status_code == 200 else 1)
    except Exception as e:
        error_result = {"error": str(e)}
        print(json.dumps(error_result))
        sys.exit(1)
