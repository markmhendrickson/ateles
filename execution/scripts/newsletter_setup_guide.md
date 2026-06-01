# Newsletter Form Setup Guide

**Purpose:** Set up newsletter subscription form with ICP mapping survey for markmhendrickson.com

**Sovereignty-aligned:** All subscriber data stored in user-owned database, not third-party platform.

---

## Files Created

1. **`execution/newsletter-form.html`** - Newsletter subscription form with ICP survey (ready to embed)
2. **`execution/scripts/newsletter_subscribe_api.py`** - Backend API handler for form submissions

---

## Setup Steps

### 1. Choose Email Delivery API

Select one of:
- **Resend** (recommended): Developer-friendly, good for newsletters
- **SendGrid**: Established, robust deliverability
- **Mailgun**: Good deliverability, flexible API

### 2. Configure Email Delivery API

**For Resend:**
1. Sign up at https://resend.com
2. Get API key from dashboard
3. Verify domain (markmhendrickson.com)
4. Set environment variable: `EMAIL_DELIVERY_API_KEY=re_...`

**For SendGrid:**
1. Sign up at https://sendgrid.com
2. Get API key from dashboard
3. Verify domain (markmhendrickson.com)
4. Set environment variable: `EMAIL_DELIVERY_API_KEY=SG....`

**For Mailgun:**
1. Sign up at https://mailgun.com
2. Get API key from dashboard
3. Verify domain (markmhendrickson.com)
4. Set environment variable: `EMAIL_DELIVERY_API_KEY=key-...`

### 3. Configure Domain Email (SPF, DKIM, DMARC)

**Required DNS records for email deliverability:**

**SPF Record:**
```
TXT @ "v=spf1 include:_spf.resend.com ~all"  (for Resend)
TXT @ "v=spf1 include:sendgrid.net ~all"     (for SendGrid)
TXT @ "v=spf1 include:mailgun.org ~all"      (for Mailgun)
```

**DKIM Record:**
- Provided by email delivery API after domain verification
- Add as TXT record (format provided by API)

**DMARC Record:**
```
TXT _dmarc "v=DMARC1; p=quarantine; rua=mailto:dmarc@markmhendrickson.com"
```

**Setup:**
1. Follow email delivery API's domain verification guide
2. Add DNS records via DNSimple (or your DNS provider)
3. Wait for verification (usually 24-48 hours)

### 4. Set Up Database

**Option A: SQLite (Simple, Local)**
```python
# Update newsletter_subscribe_api.py to use SQLite
import sqlite3

DB_PATH = Path('data/newsletter_subscribers.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS subscribers (
            email TEXT PRIMARY KEY,
            survey TEXT,
            subscribed_at TEXT,
            updated_at TEXT,
            status TEXT,
            icp_tier TEXT
        )
    ''')
    conn.commit()
    conn.close()
```

**Option B: PostgreSQL (Production, Recommended)**
```python
# Update newsletter_subscribe_api.py to use PostgreSQL
import psycopg2

DB_CONNECTION = os.getenv('DATABASE_URL', 'postgresql://user:pass@localhost/newsletter')

def save_subscriber(email, survey, subscribed_at):
    conn = psycopg2.connect(DB_CONNECTION)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO subscribers (email, survey, subscribed_at, updated_at, status, icp_tier)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (email) DO UPDATE
        SET survey = %s, updated_at = %s
    ''', (email, json.dumps(survey), subscribed_at, subscribed_at, 'subscribed', survey.get('icp_tier'), json.dumps(survey), subscribed_at))
    conn.commit()
    conn.close()
```

**Option C: JSON File (Development/Testing)**
- Current implementation uses JSON file
- Good for testing, not recommended for production

### 5. Deploy Backend API

**Option A: Serverless Function (Vercel, Netlify, Cloudflare Workers)**
1. Create API endpoint: `/api/newsletter/subscribe`
2. Deploy `newsletter_subscribe_api.py` as serverless function
3. Set environment variables:
   - `EMAIL_DELIVERY_API` (resend/sendgrid/mailgun)
   - `EMAIL_DELIVERY_API_KEY`
   - `NEWSLETTER_FROM_EMAIL` (newsletter@markmhendrickson.com)
   - `DATABASE_URL` (if using PostgreSQL)

**Option B: Python Web Server (Flask/FastAPI)**
```python
# Example Flask endpoint
from flask import Flask, request, jsonify
import newsletter_subscribe_api

app = Flask(__name__)

@app.route('/api/newsletter/subscribe', methods=['POST'])
def subscribe():
    data = request.json
    result, status_code = newsletter_subscribe_api.handle_subscribe(data)
    return jsonify(result), status_code
```

**Option C: Direct Script (Cron/Webhook)**
- Use `newsletter_subscribe_api.py` as standalone script
- Call via webhook or cron job

### 6. Embed Form on Website

**Option A: Direct Embed (HTML)**
```html
<!-- Add to your website HTML -->
<iframe src="/newsletter-form.html" width="100%" height="800" frameborder="0"></iframe>
```

**Option B: Copy Form Code**
- Copy form HTML/CSS/JS from `newsletter-form.html`
- Paste into your website template
- Update API endpoint URL in JavaScript

**Option C: React/Next.js Component**
- Convert HTML form to React component
- Use same API endpoint

### 7. Test Form Submission

1. Fill out form with test email
2. Check database for subscriber entry
3. Verify confirmation email received
4. Check email deliverability (SPF, DKIM, DMARC)

### 8. Set Up Unsubscribe Handling

**Create unsubscribe endpoint:**
```python
@app.route('/api/newsletter/unsubscribe', methods=['POST'])
def unsubscribe():
    email = request.json.get('email')
    token = request.json.get('token')
    # Verify token, update subscriber status
    # Send confirmation email
```

**Add unsubscribe link to emails:**
```
https://markmhendrickson.com/newsletter/unsubscribe?email={email}&token={token}
```

---

## Environment Variables

```bash
# Email Delivery API
EMAIL_DELIVERY_API=resend  # or 'sendgrid' or 'mailgun'
EMAIL_DELIVERY_API_KEY=re_xxxxx  # or SG.xxxxx or key-xxxxx
NEWSLETTER_FROM_EMAIL=newsletter@markmhendrickson.com
NEWSLETTER_NAME="Mark Hendrickson Newsletter"

# Database
DATABASE_URL=postgresql://user:pass@localhost/newsletter  # or path to SQLite file
NEWSLETTER_DB_PATH=data/newsletter_subscribers.json  # if using JSON file
```

---

## ICP Tier Mapping

The survey automatically maps responses to Neotoma ICP priority tiers:

- **Tier 1:** AI-Native Individual Operators, High-Context Knowledge Workers, AI-Native Founders & Small Teams
- **Tier 2:** Hybrid Product Teams, Cross-Functional Operational Teams, Developer Integrators, AI Tool Integrators
- **Tier 3:** Cross-Border Solopreneurs, Multi-System Information Workers, High-Entropy Households
- **Tier 4:** Crypto-Native Power Users, High-Net-Worth Individuals, Multi-Jurisdiction Residents, Startup Founders with Equity Docs

---

## Privacy & Sovereignty

- **User-owned database:** All subscriber data stored in your database, not third-party platform
- **Optional survey:** Survey participation is optional (reduces friction)
- **Clear explanation:** Privacy note explains how data is used
- **GDPR compliant:** Unsubscribe handling, data export capability

---

## Next Steps

1. Choose email delivery API (Resend recommended)
2. Set up domain email (SPF, DKIM, DMARC)
3. Configure database (PostgreSQL recommended for production)
4. Deploy backend API endpoint
5. Embed form on website
6. Test form submission
7. Set up unsubscribe handling

---

## Related Documents

- [`strategy/strategy/self-publishing-strategy.md`](../../strategy/strategy/self-publishing-strategy.md) - Newsletter strategy and ICP survey questions
- [`strategy/operations/newsletter-launch-criteria.md`](../../strategy/operations/newsletter-launch-criteria.md) - Launch criteria and readiness evaluation
