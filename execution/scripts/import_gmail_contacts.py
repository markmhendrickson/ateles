#!/usr/bin/env python3
"""
Gmail Contacts Import Script

Imports contacts from Gmail using:
1. Google People API (Contacts API) to fetch existing contacts
2. Gmail API to scan last N emails and extract contact information

Filters out company promotions and irrelevant emails during scanning.

Usage:
    python import_gmail_contacts.py [--max-emails N] [--credentials-file PATH] [--token-file PATH]

Examples:
    python import_gmail_contacts.py --max-emails 100
    python import_gmail_contacts.py --max-emails 500 --credentials-file ~/credentials.json
"""

import argparse
import hashlib
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from dateutil import parser as date_parser
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import BaseDataImporter and CurrencyConverter
# Configuration
from scripts.config import DATA_DIR
from scripts.import_data import BaseDataImporter, CurrencyConverter

LOGS_DIR = DATA_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

SCOPES = [
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Configure logging
LOG_FILE = (
    LOGS_DIR / f"gmail_contacts_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Common promotional email patterns
PROMOTIONAL_PATTERNS = [
    r"noreply@",
    r"no-reply@",
    r"newsletter@",
    r"notifications@",
    r"@.*\.(com|net|org).*marketing",
    r"unsubscribe",
    r"promo",
    r"deal",
    r"sale",
    r"discount",
    r"offer",
    r"coupon",
    r"newsletter",
    r"bulk",
    r"marketing",
]

# Common promotional domains
PROMOTIONAL_DOMAINS = {
    "mailchimp.com",
    "constantcontact.com",
    "campaignmonitor.com",
    "sendgrid.com",
    "amazon.com",
    "ebay.com",
    "etsy.com",
    "shopify.com",
    "stripe.com",
    "paypal.com",
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "instagram.com",
    "pinterest.com",
    "youtube.com",
    "github.com",
    "stackoverflow.com",
    "medium.com",
    "substack.com",
    "patreon.com",
    "kickstarter.com",
    "indiegogo.com",
    "eventbrite.com",
    "meetup.com",
    "airbnb.com",
    "booking.com",
    "expedia.com",
    "tripadvisor.com",
    "uber.com",
    "lyft.com",
    "doordash.com",
    "grubhub.com",
    "ubereats.com",
    "netflix.com",
    "spotify.com",
    "hulu.com",
    "disney.com",
    "hbo.com",
    "paramount.com",
    "peacock.com",
    "apple.com",
    "google.com",
    "microsoft.com",
    "adobe.com",
    "salesforce.com",
    "hubspot.com",
    "zendesk.com",
    "slack.com",
    "zoom.com",
    "dropbox.com",
    "box.com",
    "onedrive.com",
    "icloud.com",
    "gmail.com",
    "outlook.com",
    "yahoo.com",
    "aol.com",
    "protonmail.com",
    "tutanota.com",
    "mail.com",
    "gmx.com",
    "zoho.com",
    "yandex.com",
    "mail.ru",
    "qq.com",
    "163.com",
    "sina.com",
    "sohu.com",
    "reddit.com",
    "quora.com",
    "digg.com",
    "stumbleupon.com",
    "delicious.com",
    "pocket.com",
    "flipboard.com",
    "feedly.com",
    "newsletter",
    "notifications",
    "alerts",
    "updates",
    "digest",
    "summary",
    "weekly",
    "daily",
    "monthly",
}


def get_credentials(credentials_file: Path, token_file: Path) -> Credentials:
    """Get valid user credentials from storage or OAuth flow."""
    logger.info(f"Getting credentials from {credentials_file}")
    logger.debug(f"Token file path: {token_file}")
    creds = None

    # Load existing token
    if token_file.exists():
        logger.info(f"Found existing token file: {token_file}")
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            logger.debug(
                f"Loaded credentials from token file. Valid: {creds.valid}, Expired: {creds.expired if creds else 'N/A'}"
            )
        except Exception as e:
            logger.warning(f"Could not load token file: {e}", exc_info=True)
    else:
        logger.info("No existing token file found")

    # If there are no (valid) credentials available, let the user log in
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Token expired, refreshing...")
            try:
                creds.refresh(Request())
                logger.info("Token refreshed successfully")
            except Exception as e:
                logger.error(f"Failed to refresh token: {e}", exc_info=True)
                creds = None
        else:
            if not credentials_file.exists():
                error_msg = (
                    f"Credentials file not found: {credentials_file}\n"
                    "Please download OAuth 2.0 credentials from Google Cloud Console:\n"
                    "1. Go to https://console.cloud.google.com/\n"
                    "2. Create a project or select existing one\n"
                    "3. Enable People API and Gmail API\n"
                    "4. Create OAuth 2.0 credentials (Desktop app)\n"
                    "5. Download credentials JSON file\n"
                    "6. Specify path with --credentials-file"
                )
                logger.error(error_msg)
                raise FileNotFoundError(error_msg)

            logger.info(
                f"Starting OAuth flow with credentials file: {credentials_file}"
            )
            logger.debug(f"Requested scopes: {SCOPES}")
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_file), SCOPES
                )
                logger.info("OAuth flow initialized, starting local server...")
                creds = flow.run_local_server(port=0)
                logger.info("OAuth flow completed successfully")
            except Exception as e:
                logger.error(f"OAuth flow failed: {e}", exc_info=True)
                raise

        # Save credentials for next run
        logger.info(f"Saving credentials to token file: {token_file}")
        token_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(token_file, "w") as token:
                token.write(creds.to_json())
            logger.info("Token file saved successfully")
        except Exception as e:
            logger.error(f"Failed to save token file: {e}", exc_info=True)
            raise
    else:
        logger.info("Using existing valid credentials")

    return creds


def is_promotional_email(email_address: str, subject: str = "", body: str = "") -> bool:
    """Check if an email appears to be promotional or automated."""
    email_lower = email_address.lower()
    subject_lower = subject.lower()
    body.lower()

    logger.debug(f"Checking if email is promotional: {email_address[:50]}...")

    # Check domain
    domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    if domain in PROMOTIONAL_DOMAINS:
        logger.debug(f"Email flagged as promotional due to domain: {domain}")
        return True

    # Check email address patterns
    for pattern in PROMOTIONAL_PATTERNS:
        if re.search(pattern, email_lower, re.IGNORECASE):
            logger.debug(
                f"Email flagged as promotional due to pattern match: {pattern}"
            )
            return True

    # Check subject for promotional keywords
    promotional_keywords = [
        "unsubscribe",
        "newsletter",
        "promo",
        "deal",
        "sale",
        "discount",
        "offer",
        "coupon",
        "marketing",
        "bulk",
        "digest",
        "summary",
        "weekly",
        "daily",
        "monthly",
        "alert",
        "notification",
        "update",
    ]
    for keyword in promotional_keywords:
        if keyword in subject_lower:
            logger.debug(
                f"Email flagged as promotional due to subject keyword: {keyword}"
            )
            return True

    logger.debug(f"Email not flagged as promotional: {email_address[:50]}...")
    return False


def extract_email_address(text: str) -> str | None:
    """Extract email address from text."""
    if not text:
        return None

    # Pattern to match email addresses
    pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
    matches = re.findall(pattern, text)

    if matches:
        # Return first valid email (prefer non-promotional)
        for email in matches:
            email_lower = email.lower()
            if not is_promotional_email(email_lower):
                return email_lower
        # If all are promotional, return first anyway
        return matches[0].lower()

    return None


def extract_phone_number(text: str) -> str | None:
    """Extract phone number from text."""
    if not text:
        return None

    # Pattern to match phone numbers (various formats)
    patterns = [
        r"\+\d{1,3}[\s-]?\(?\d{1,4}\)?[\s-]?\d{1,4}[\s-]?\d{1,9}",  # International
        r"\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{4}",  # US format
        r"\d{3}[\s-]?\d{3}[\s-]?\d{4}",  # Simple format
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text)
        if matches:
            # Clean up phone number
            phone = re.sub(r"[\s\-\(\)]", "", matches[0])
            if len(phone) >= 10:  # Minimum valid length
                return phone

    return None


class GmailContactsImporter(BaseDataImporter):
    """Importer for Gmail contacts using People API and Gmail API."""

    def __init__(self, converter: CurrencyConverter):
        super().__init__("contacts", converter)
        self.people_service = None
        self.gmail_service = None

    def initialize_services(self, credentials: Credentials):
        """Initialize Google API services."""
        logger.info("Initializing Google API services...")
        try:
            logger.debug("Building People API service...")
            self.people_service = build("people", "v1", credentials=credentials)
            logger.info("People API service initialized")

            logger.debug("Building Gmail API service...")
            self.gmail_service = build("gmail", "v1", credentials=credentials)
            logger.info("Gmail API service initialized")
        except Exception as e:
            logger.error(f"Failed to initialize API services: {e}", exc_info=True)
            raise

    def fetch_contacts_from_people_api(self) -> list[dict]:
        """Fetch contacts from Google People API."""
        logger.info("Starting to fetch contacts from People API...")
        contacts = []

        if not self.people_service:
            logger.error("People service not initialized")
            raise ValueError(
                "People service not initialized. Call initialize_services() first."
            )

        try:
            logger.debug("Making API call to People API connections.list...")
            # Fetch all contacts
            results = (
                self.people_service.people()
                .connections()
                .list(
                    resourceName="people/me",
                    pageSize=1000,
                    personFields="names,emailAddresses,phoneNumbers,addresses,organizations,urls",
                )
                .execute()
            )

            connections = results.get("connections", [])
            logger.info(f"Retrieved {len(connections)} connections from People API")

            for idx, person in enumerate(connections):
                logger.debug(f"Processing person {idx + 1}/{len(connections)}")
                # Extract name
                names = person.get("names", [])
                name = names[0].get("displayName", "") if names else ""

                # Extract email addresses
                emails = person.get("emailAddresses", [])
                primary_email = None
                all_emails = []
                for email_obj in emails:
                    email = email_obj.get("value", "").lower()
                    if email:
                        all_emails.append(email)
                        if email_obj.get("metadata", {}).get("primary", False):
                            primary_email = email

                if not primary_email and all_emails:
                    primary_email = all_emails[0]

                # Extract phone numbers
                phones = person.get("phoneNumbers", [])
                primary_phone = None
                for phone_obj in phones:
                    phone = phone_obj.get("value", "")
                    if phone:
                        primary_phone = phone
                        break

                # Extract addresses
                addresses = person.get("addresses", [])
                primary_address = None
                for addr_obj in addresses:
                    addr = addr_obj.get("formattedValue", "")
                    if addr:
                        primary_address = addr
                        break

                # Extract organizations
                orgs = person.get("organizations", [])
                organization = None
                for org_obj in orgs:
                    org = org_obj.get("name", "")
                    if org:
                        organization = org
                        break

                # Extract URLs/websites
                urls = person.get("urls", [])
                website = None
                for url_obj in urls:
                    url = url_obj.get("value", "")
                    if url:
                        website = url
                        break

                if name or primary_email:
                    contact_info = {
                        "name": name,
                        "email": primary_email,
                        "all_emails": all_emails,
                        "phone": primary_phone,
                        "address": primary_address,
                        "organization": organization,
                        "website": website,
                        "source": "people_api",
                        "resource_name": person.get("resourceName", ""),
                    }
                    contacts.append(contact_info)
                    logger.debug(
                        f"Added contact: {name or 'No name'} ({primary_email or 'No email'})"
                    )
                else:
                    logger.debug(f"Skipped person {idx + 1} - no name or email")

        except HttpError as error:
            logger.error(
                f"HTTP error fetching contacts from People API: {error}", exc_info=True
            )
        except Exception as e:
            logger.error(
                f"Unexpected error fetching contacts from People API: {e}",
                exc_info=True,
            )

        logger.info(f"Successfully fetched {len(contacts)} contacts from People API")
        return contacts

    def fetch_contacts_from_emails(self, max_emails: int = 100) -> list[dict]:
        """Scan Gmail messages to extract contact information."""
        logger.info(
            f"Starting to scan up to {max_emails} emails for contact information..."
        )
        contacts = []

        if not self.gmail_service:
            logger.error("Gmail service not initialized")
            raise ValueError(
                "Gmail service not initialized. Call initialize_services() first."
            )

        try:
            logger.debug(f"Fetching message list (maxResults={max_emails})...")
            # Fetch message list
            results = (
                self.gmail_service.users()
                .messages()
                .list(userId="me", maxResults=max_emails)
                .execute()
            )

            messages = results.get("messages", [])
            logger.info(f"Retrieved {len(messages)} messages from Gmail")

            seen_emails: set[str] = set()
            promotional_count = 0
            duplicate_count = 0

            for idx, msg_ref in enumerate(messages):
                logger.debug(
                    f"Processing message {idx + 1}/{len(messages)} (ID: {msg_ref.get('id', 'unknown')})"
                )
                try:
                    # Get full message
                    msg = (
                        self.gmail_service.users()
                        .messages()
                        .get(userId="me", id=msg_ref["id"], format="full")
                        .execute()
                    )

                    headers = msg["payload"].get("headers", [])

                    # Extract headers
                    subject = ""
                    from_email = ""
                    from_name = ""

                    for header in headers:
                        name = header.get("name", "").lower()
                        value = header.get("value", "")

                        if name == "subject":
                            subject = value
                        elif name == "from":
                            # Parse "Name <email@domain.com>" format
                            match = re.match(r"^(.+?)\s*<(.+?)>$", value)
                            if match:
                                from_name = match.group(1).strip()
                                from_email = match.group(2).strip().lower()
                            else:
                                from_email = value.strip().lower()
                        elif name == "to":
                            value.strip().lower()

                    # Skip promotional emails
                    if is_promotional_email(from_email, subject):
                        promotional_count += 1
                        logger.debug(
                            f"Skipping promotional email from: {from_email[:50]}..."
                        )
                        continue

                    # Skip if already seen
                    if from_email in seen_emails:
                        duplicate_count += 1
                        logger.debug(
                            f"Skipping duplicate email from: {from_email[:50]}..."
                        )
                        continue

                    seen_emails.add(from_email)
                    logger.debug(f"Processing new contact from: {from_email}")

                    # Extract body for additional contact info
                    body_text = ""
                    if "parts" in msg["payload"]:
                        for part in msg["payload"]["parts"]:
                            if part.get("mimeType") == "text/plain":
                                data = part.get("body", {}).get("data", "")
                                if data:
                                    import base64

                                    body_text = base64.urlsafe_b64decode(data).decode(
                                        "utf-8", errors="ignore"
                                    )
                                    break
                    elif msg["payload"].get("mimeType") == "text/plain":
                        data = msg["payload"].get("body", {}).get("data", "")
                        if data:
                            import base64

                            body_text = base64.urlsafe_b64decode(data).decode(
                                "utf-8", errors="ignore"
                            )

                    # Extract additional contact info from body
                    logger.debug(
                        f"Extracting contact info from email body (length: {len(body_text)} chars)"
                    )
                    phone = extract_phone_number(body_text)
                    if phone:
                        logger.debug(f"Extracted phone number: {phone}")

                    # Get date
                    internal_date = msg.get("internalDate")
                    if internal_date:
                        try:
                            contact_date = datetime.fromtimestamp(
                                int(internal_date) / 1000
                            ).date()
                            logger.debug(f"Parsed contact date: {contact_date}")
                        except (ValueError, TypeError) as e:
                            logger.warning(
                                f"Failed to parse date {internal_date}: {e}, using today"
                            )
                            contact_date = date.today()
                    else:
                        logger.debug("No internal date found, using today")
                        contact_date = date.today()

                    if from_email:
                        contact_info = {
                            "name": from_name or "",
                            "email": from_email,
                            "phone": phone,
                            "address": None,
                            "organization": None,
                            "website": None,
                            "source": "gmail_scan",
                            "first_contact_date": contact_date,
                            "last_contact_date": contact_date,
                        }
                        contacts.append(contact_info)
                        logger.debug(
                            f"Added contact from email: {from_name or 'No name'} ({from_email})"
                        )

                except HttpError as error:
                    logger.error(
                        f"HTTP error fetching message {msg_ref.get('id', 'unknown')}: {error}",
                        exc_info=True,
                    )
                    continue
                except Exception as e:
                    logger.error(
                        f"Error processing message {msg_ref.get('id', 'unknown')}: {e}",
                        exc_info=True,
                    )
                    continue

            logger.info(
                f"Email scanning complete. Found {len(contacts)} contacts, skipped {promotional_count} promotional, {duplicate_count} duplicates"
            )

        except HttpError as error:
            logger.error(
                f"HTTP error fetching messages from Gmail: {error}", exc_info=True
            )
        except Exception as e:
            logger.error(
                f"Unexpected error fetching messages from Gmail: {e}", exc_info=True
            )

        logger.info(
            f"Successfully extracted {len(contacts)} contacts from email scanning"
        )
        return contacts

    def generate_id(self, record: dict, source_file: str) -> str:
        """Generate unique ID from contact record."""
        # Use email as primary identifier if available
        key_fields = {
            "email": record.get("email", ""),
            "name": record.get("name", ""),
            "phone": record.get("phone", ""),
        }
        hash_string = json.dumps(key_fields, sort_keys=True, default=str) + source_file
        return hashlib.sha256(hash_string.encode()).hexdigest()[:16]

    def normalize_record(
        self, record: dict, source: str, source_file: str, **kwargs
    ) -> dict:
        """Normalize contact record to schema."""
        logger.debug(
            f"Normalizing record: {record.get('email', 'No email')} from {source}"
        )
        contact_id = self.generate_id(record, source_file)
        logger.debug(f"Generated contact_id: {contact_id}")

        # Determine contact type and category
        contact_type = "personal"
        category = "unknown"

        if record.get("organization"):
            contact_type = "business"
            category = "vendor"
            logger.debug(
                f"Classified as business/vendor due to organization: {record.get('organization')}"
            )
        elif record.get("source") == "people_api":
            contact_type = "personal"
            category = "contact"
            logger.debug("Classified as personal/contact from People API")
        elif record.get("source") == "gmail_scan":
            contact_type = "personal"
            category = "email_contact"
            logger.debug("Classified as personal/email_contact from Gmail scan")

        # Extract dates
        first_contact_date = record.get("first_contact_date")
        if isinstance(first_contact_date, str):
            try:
                first_contact_date = date_parser.parse(first_contact_date).date()
            except (ValueError, TypeError):
                first_contact_date = date.today()
        elif not isinstance(first_contact_date, date):
            first_contact_date = date.today()

        last_contact_date = record.get("last_contact_date")
        if isinstance(last_contact_date, str):
            try:
                last_contact_date = date_parser.parse(last_contact_date).date()
            except (ValueError, TypeError):
                last_contact_date = date.today()
        elif not isinstance(last_contact_date, date):
            last_contact_date = date.today()

        # Extract country from address if available
        country = None
        address = record.get("address", "")
        if address:
            # Simple country extraction (can be improved)
            country_match = re.search(r"\b([A-Z]{2})\b", address)
            if country_match:
                country = country_match.group(1)

        return {
            "contact_id": contact_id,
            "name": record.get("name", "") or "",
            "contact_type": contact_type,
            "category": category,
            "platform": "gmail",
            "email": record.get("email", "") or "",
            "phone": record.get("phone", "") or "",
            "address": address or "",
            "country": country or "",
            "website": record.get("website", "") or "",
            "notes": f"Imported from {source}",
            "first_contact_date": first_contact_date,
            "last_contact_date": last_contact_date,
            "created_date": date.today(),
            "updated_date": date.today(),
        }

    def import_from_gmail(
        self, credentials: Credentials, max_emails: int = 100
    ) -> dict:
        """Import contacts from Gmail APIs."""
        logger.info("=" * 80)
        logger.info("Starting Gmail contacts import")
        logger.info(f"Max emails to scan: {max_emails}")
        logger.info(f"Data file: {self.data_file}")
        logger.info("=" * 80)

        # Initialize services
        self.initialize_services(credentials)

        # Fetch contacts from People API
        logger.info("Fetching contacts from Google People API...")
        people_contacts = self.fetch_contacts_from_people_api()
        logger.info(f"Found {len(people_contacts)} contacts from People API")

        # Fetch contacts from email scanning
        logger.info(f"Scanning last {max_emails} emails for contact information...")
        email_contacts = self.fetch_contacts_from_emails(max_emails=max_emails)
        logger.info(f"Found {len(email_contacts)} contacts from email scanning")

        # Combine contacts
        all_contacts = people_contacts + email_contacts
        logger.info(
            f"Total contacts collected: {len(all_contacts)} ({len(people_contacts)} from People API, {len(email_contacts)} from email scan)"
        )

        if not all_contacts:
            logger.warning("No contacts found to import")
            return {"imported": 0, "duplicates": 0, "total": 0}

        # Normalize records
        logger.info("Normalizing contact records...")
        normalized_records = []
        for idx, contact in enumerate(all_contacts):
            try:
                normalized = self.normalize_record(contact, "gmail", "gmail_api", **{})
                normalized_records.append(normalized)
                if (idx + 1) % 50 == 0:
                    logger.debug(f"Normalized {idx + 1}/{len(all_contacts)} contacts")
            except Exception as e:
                logger.error(f"Error normalizing contact {idx + 1}: {e}", exc_info=True)
                continue

        logger.info(f"Normalized {len(normalized_records)} contact records")

        # Load existing data
        logger.info(f"Loading existing data from {self.data_file}")
        existing_df = self.load_existing_data()
        existing_count = len(existing_df) if not existing_df.empty else 0
        logger.info(f"Found {existing_count} existing contacts in database")

        # Create DataFrame from new records
        logger.info("Creating DataFrame from normalized records...")
        new_df = pd.DataFrame(normalized_records)

        if new_df.empty:
            logger.warning("No records to import after normalization")
            return {"imported": 0, "duplicates": 0, "total": existing_count}

        logger.info(f"Created DataFrame with {len(new_df)} records")

        # Deduplicate: remove records that already exist
        logger.info("Deduplicating contacts...")
        id_column = "contact_id"
        initial_new_count = len(new_df)

        if not existing_df.empty and id_column in existing_df.columns:
            existing_ids = set(existing_df[id_column].values)
            logger.debug(f"Found {len(existing_ids)} existing contact IDs")
            new_df = new_df[~new_df[id_column].isin(existing_ids)]
            duplicates_by_id = initial_new_count - len(new_df)
            logger.info(f"Removed {duplicates_by_id} duplicates by contact_id")
        else:
            duplicates_by_id = 0
            logger.debug("No existing data to deduplicate against")

        # Also deduplicate by email within new records
        if "email" in new_df.columns:
            before_email_dedup = len(new_df)
            new_df = new_df.drop_duplicates(subset=["email"], keep="first")
            duplicates_by_email = before_email_dedup - len(new_df)
            logger.info(f"Removed {duplicates_by_email} duplicates by email address")
        else:
            duplicates_by_email = 0

        duplicates = duplicates_by_id + duplicates_by_email
        logger.info(f"Total duplicates removed: {duplicates}")

        # Merge with existing
        logger.info("Merging new contacts with existing data...")
        if existing_df.empty:
            combined_df = new_df
            logger.debug("No existing data, using only new contacts")
        else:
            combined_df = pd.concat([existing_df, new_df], ignore_index=True)
            logger.debug(
                f"Merged {len(existing_df)} existing + {len(new_df)} new = {len(combined_df)} total"
            )

        # Save to Parquet
        logger.info(f"Saving {len(combined_df)} total contacts to {self.data_file}")
        try:
            combined_df.to_parquet(self.data_file, index=False, engine="pyarrow")
            logger.info(f"Successfully saved contacts to {self.data_file}")
        except Exception as e:
            logger.error(f"Failed to save contacts to parquet file: {e}", exc_info=True)
            raise

        # Log import
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "data_type": self.data_type,
            "source_file": "gmail_api",
            "source": "gmail",
            "records_imported": len(new_df),
            "records_duplicate": duplicates,
            "total_records": len(combined_df),
            "options": {"max_emails": max_emails},
        }

        log_file = DATA_DIR / "logs" / "import_log.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(log_entry) + "\n")
            logger.info(f"Logged import summary to {log_file}")
        except Exception as e:
            logger.warning(f"Failed to write to import log: {e}", exc_info=True)

        logger.info("=" * 80)
        logger.info("Import Summary:")
        logger.info(f"  New contacts imported: {len(new_df)}")
        logger.info(f"  Duplicate contacts skipped: {duplicates}")
        logger.info(f"  Total contacts in database: {len(combined_df)}")
        logger.info("=" * 80)

        return {
            "imported": len(new_df),
            "duplicates": duplicates,
            "total": len(combined_df),
        }


def main():
    parser = argparse.ArgumentParser(description="Import contacts from Gmail")
    parser.add_argument(
        "--max-emails",
        type=int,
        default=100,
        help="Maximum number of emails to scan (default: 100)",
    )
    parser.add_argument(
        "--credentials-file",
        type=str,
        default=str(PROJECT_ROOT / "credentials.json"),
        help="Path to OAuth 2.0 credentials JSON file",
    )
    parser.add_argument(
        "--token-file",
        type=str,
        default=str(PROJECT_ROOT / "token.json"),
        help="Path to store OAuth token",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set logging level (default: INFO)",
    )

    args = parser.parse_args()

    # Set logging level
    logging.getLogger().setLevel(getattr(logging, args.log_level))
    logger.info(f"Logging level set to {args.log_level}")
    logger.info(f"Log file: {LOG_FILE}")

    credentials_file = Path(args.credentials_file)
    token_file = Path(args.token_file)

    logger.info("Starting Gmail contacts import script")
    logger.info(
        f"Arguments: max_emails={args.max_emails}, credentials_file={credentials_file}, token_file={token_file}"
    )

    try:
        # Get credentials
        logger.info("Authenticating with Google APIs...")
        credentials = get_credentials(credentials_file, token_file)
        logger.info("Authentication successful")

        # Create importer
        logger.info("Creating GmailContactsImporter instance...")
        converter = CurrencyConverter()
        importer = GmailContactsImporter(converter)

        # Import contacts
        stats = importer.import_from_gmail(credentials, max_emails=args.max_emails)

        logger.info("Script completed successfully")
        print("\nImport Summary:")
        print(f"  New contacts imported: {stats['imported']}")
        print(f"  Duplicate contacts skipped: {stats['duplicates']}")
        print(f"  Total contacts in database: {stats['total']}")
        print(f"\nDetailed log saved to: {LOG_FILE}")

    except KeyboardInterrupt:
        logger.warning("Script interrupted by user")
        print("\nScript interrupted by user", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        print(f"Error: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        print(f"\nDetailed error log saved to: {LOG_FILE}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
