# Gmail Contacts Import - OAuth Setup Guide

## Step 1: Enable Required APIs

1. Go to https://console.cloud.google.com/apis/library?project=personal-412209
2. Search for "People API" and click on it
3. Click the "ENABLE" button
4. Search for "Gmail API" and click on it  
5. Click the "ENABLE" button

## Step 2: Create OAuth 2.0 Credentials

1. Go to https://console.cloud.google.com/apis/credentials?project=personal-412209
2. Click "+ CREATE CREDENTIALS" at the top
3. Select "OAuth client ID"
4. If prompted, configure the OAuth consent screen:
   - User Type: External (unless you have a Google Workspace account)
   - App name: "Gmail Contacts Importer" (or any name)
   - User support email: your email
   - Developer contact: your email
   - Click "SAVE AND CONTINUE" through the scopes (you can skip adding scopes)
   - Click "SAVE AND CONTINUE" for test users (skip if not needed)
   - Click "BACK TO DASHBOARD"
5. Back at credentials page, click "+ CREATE CREDENTIALS" → "OAuth client ID"
6. Application type: Select "Desktop app"
7. Name: "Gmail Contacts Desktop Client" (or any name)
8. Click "CREATE"
9. A dialog will appear with your credentials
10. Click "DOWNLOAD JSON"
11. Save the file as `credentials.json` in your project root

## Step 3: Run the Import Script

Once `credentials.json` is in place, run:

```bash
# From the repo root directory
source execution/venv/bin/activate
python execution/scripts/import_gmail_contacts.py --max-emails 100
```

The script will:
- Open a browser for OAuth authentication
- Store the token in `token.json` for future runs
- Import contacts from Google Contacts
- Scan your last 100 emails for contact information
- Filter out promotional emails
- Save contacts to `$DATA_DIR/contacts/contacts.parquet`

## Notes

- The first run will require browser authentication
- Subsequent runs will use the stored token (unless it expires)
- The script filters out promotional emails automatically
- Contacts are deduplicated by email address







