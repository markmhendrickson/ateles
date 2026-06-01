# Troubleshooting Gmail OAuth "deleted_client" Error

## Problem
Getting "Error 401: deleted_client" even after creating a fresh secret for the OAuth client.

## Possible Causes

1. **Client was deleted and recreated** - The client ID might be the same, but Google considers it a new client
2. **Credentials mismatch** - The credentials JSON might be for a different client
3. **Consent screen not configured** - Missing scopes or incomplete setup
4. **Timing issue** - Client just created, needs time to propagate

## Solution: Create a Brand New OAuth Client

### Step 1: Delete the Old Client (Optional but Recommended)

1. Go to: https://console.cloud.google.com/apis/credentials?project=personal-412209
2. Find "Ateles" OAuth client
3. Click the Delete (trash) icon
4. Confirm deletion

### Step 2: Create a New OAuth Client

1. On the same Credentials page
2. Click "+ CREATE CREDENTIALS" → "OAuth client ID"
3. Application type: "Desktop app"
4. Name: "Gmail MCP Desktop Client" (use a different name to avoid confusion)
5. Click "CREATE"
6. **IMPORTANT**: Click "DOWNLOAD JSON" immediately
7. Save the JSON file

### Step 3: Verify Consent Screen

1. Go to: https://console.cloud.google.com/apis/credentials/consent?project=personal-412209
   - Or click "Audience" in Google Auth Platform
2. Ensure these scopes are added in "Data Access":
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.modify`
   - `https://www.googleapis.com/auth/calendar`

### Step 4: Update 1Password

1. Open 1Password item: "Google (markmhendrickson@gmail.com)"
2. Update "OAuth credentials" field with the NEW JSON content
3. Make sure it's the complete JSON (not just client_id/secret)

### Step 5: Sync and Test

1. Run: `/sync_env_from_1password`
2. Verify: Check that `.creds/gcp-oauth.keys.json` has the new client_id
3. Restart Cursor completely
4. Try using Gmail MCP tools

## Verify Client ID Match

After syncing, verify the client ID matches:

```bash
cat .creds/gcp-oauth.keys.json | python3 -c "import sys, json; print(json.load(sys.stdin)['installed']['client_id'])"
```

Compare this to the Client ID shown in Google Cloud Console for your new OAuth client.

## If Still Not Working

1. **Check client status** - Ensure it shows "Enabled" in the console
2. **Wait a few minutes** - New clients sometimes need time to propagate
3. **Clear browser cache** - Try in incognito/private window
4. **Check scopes** - Verify all required scopes are in "Data Access"
