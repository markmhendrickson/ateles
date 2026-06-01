# Google Cloud OAuth Setup Automation

Automated script to set up Google Cloud OAuth credentials for Gmail/Calendar MCP servers and Vision API.

## Quick Start

```bash
# Run the automation script
./execution/scripts/setup_google_cloud_oauth.sh
```

The script will:
- ✅ Automate what can be automated (API enablement, service account creation)
- 📋 Guide you through steps that require web UI (OAuth client creation)

## What Gets Automated

### With gcloud CLI installed:
- ✅ Enable required APIs (Gmail, Calendar, Vision)
- ✅ Create service account for Vision API
- ✅ Grant Cloud Vision API User role

### Without gcloud CLI:
- Provides direct links to web UI for each step
- Guides you through manual configuration

## What Requires Web UI

These steps require the Google Cloud Console web UI:

1. **OAuth Consent Screen Configuration**
   - User type, app name, support email
   - Adding required scopes

2. **OAuth Client ID Creation**
   - Create desktop app OAuth client
   - Download credentials JSON

3. **Service Account Key Download** (if gcloud not installed)
   - Create and download JSON key

## Prerequisites

### Optional (for automation):
- `gcloud` CLI installed: `brew install google-cloud-sdk`
- Authenticated: `gcloud auth login`
- Project set: `gcloud config set project personal-412209`

### Required:
- Google Cloud project: `personal-412209`
- Access to Google Cloud Console
- 1Password account (for credential storage)

## Manual Steps Guide

If you prefer to do everything manually or the script can't automate certain parts:

### 1. Enable APIs
- Gmail API: https://console.cloud.google.com/apis/library/gmail.googleapis.com
- Calendar API: https://console.cloud.google.com/apis/library/calendar-json.googleapis.com
- Vision API: https://console.cloud.google.com/apis/library/vision.googleapis.com

### 2. OAuth Consent Screen
- URL: https://console.cloud.google.com/apis/credentials/consent
- Configure: User type, app name, scopes

### 3. Create OAuth Client
- URL: https://console.cloud.google.com/apis/credentials
- Create → OAuth client ID → Desktop app
- Download JSON

### 4. Service Account (for Vision API)
- URL: https://console.cloud.google.com/iam-admin/serviceaccounts
- Create service account
- Grant "Cloud Vision API User" role
- Create and download JSON key

## After Setup

1. **Store credentials in 1Password:**
   - OAuth credentials JSON → "OAuth credentials" field
   - Service account key JSON → "service key" field

2. **Sync to .env:**
   ```bash
   /sync_env_from_1password
   ```

3. **Restart Cursor** to load new credentials

## Troubleshooting

### gcloud not found
- Install: `brew install google-cloud-sdk`
- Or follow manual steps provided by script

### Permission errors
- Ensure you have Owner/Editor role on the project
- Check IAM permissions

### OAuth client creation fails
- Verify OAuth consent screen is configured first
- Check that required scopes are added

## Related Scripts

- `op_sync_env_from_1password.py` - Syncs credentials from 1Password to .env
- `fix_gmail_mcp_config.py` - Updates MCP configuration
