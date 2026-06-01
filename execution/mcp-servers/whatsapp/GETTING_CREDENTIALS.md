# Getting WhatsApp Business Platform API Credentials

This guide walks you through obtaining the three credentials needed for the WhatsApp MCP server:

1. **Access Token** (required)
2. **Phone Number ID** (required)
3. **Business Account ID** (optional, for some operations)

## Prerequisites

- ✅ Meta Business Manager account created
- ✅ WhatsApp Business account registered in Meta Business Manager
- ✅ Phone number registered with WhatsApp Business Platform

## Step-by-Step Guide

### Step 1: Access Meta App Dashboard

1. Go to **Meta for Developers**: https://developers.facebook.com/
2. Click **"My Apps"** in the top right
3. Select your app (or create a new app if you haven't already)
   - If creating new: Choose "Business" type
   - App name: e.g., "WhatsApp Business API"

### Step 2: Add WhatsApp Product

1. In your app dashboard, find **"Add Product"** or **"Products"** in the left sidebar
2. Find **"WhatsApp"** and click **"Set Up"**
3. This will add WhatsApp to your app

### Step 3: Get Phone Number ID

1. In your app dashboard, go to **WhatsApp** → **API Setup** (in left sidebar)
2. You'll see a section showing your phone number
3. The **Phone Number ID** is displayed in the format: `123456789012345`
   - It's a long numeric ID (not the phone number itself)
   - Copy this value - you'll need it for `WHATSAPP_PHONE_NUMBER_ID`

**Alternative location:**
- Go to **Meta Business Manager** → **WhatsApp Accounts** → Select your account
- The Phone Number ID is shown in the account details

### Step 4: Get Business Account ID

1. Go to **Meta Business Manager**: https://business.facebook.com/
2. Click **Business Settings** (gear icon in top right)
3. In the left sidebar, go to **Accounts** → **WhatsApp Accounts**
4. Click on your WhatsApp Business account
5. The **Business Account ID** is shown at the top of the account details
   - Format: `123456789012345`
   - Copy this value - you'll need it for `WHATSAPP_BUSINESS_ACCOUNT_ID` (optional)

### Step 5: Generate Access Token

1. In your app dashboard, go to **WhatsApp** → **API Setup**
2. Scroll down to the **"Temporary access token"** section
3. Click **"Generate token"** or **"Copy token"**
4. The token will look like: `EAABwzLixnjYBO...` (long string)
5. **Important**: This is a temporary token (expires in ~1 hour)

#### For Permanent Access Token (Recommended)

1. In your app dashboard, go to **WhatsApp** → **API Setup**
2. Scroll to **"Step 1: Get started"**
3. Under **"Access tokens"**, click **"Add or manage phone numbers"**
4. Or go to **Tools** → **Graph API Explorer**: https://developers.facebook.com/tools/explorer/
5. Select your app from the dropdown
6. Select **"WhatsApp Business Account"** as the user token type
7. Click **"Generate Access Token"**
8. Grant necessary permissions:
   - `whatsapp_business_messaging`
   - `whatsapp_business_management`
9. Copy the generated token

**Note**: For production use, you'll want to set up a System User with permanent tokens:
- Go to **Meta Business Manager** → **Business Settings** → **Users** → **System Users**
- Create a system user and assign WhatsApp permissions
- Generate a permanent token for the system user

## Quick Reference: Where to Find Each Credential

| Credential | Location | Format |
|------------|----------|--------|
| **Access Token** | Meta App Dashboard → WhatsApp → API Setup → Generate Token | `EAABwzLixnjYBO...` |
| **Phone Number ID** | Meta App Dashboard → WhatsApp → API Setup (or Business Manager → WhatsApp Accounts) | `123456789012345` |
| **Business Account ID** | Meta Business Manager → Business Settings → Accounts → WhatsApp Accounts | `123456789012345` |

## Setting Up Credentials

Once you have all three values, you can configure them in one of three ways:

### Option 1: Environment Variables (Recommended)

Add to your `mcp.json`:

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "python3.11",
      "args": [
        "${REPO_ROOT}/execution/mcp-servers/whatsapp/whatsapp_mcp_server.py"
      ],
      "env": {
        "WHATSAPP_ACCESS_TOKEN": "EAABwzLixnjYBO...",
        "WHATSAPP_PHONE_NUMBER_ID": "123456789012345",
        "WHATSAPP_BUSINESS_ACCOUNT_ID": "123456789012345"
      }
    }
  }
}
```

### Option 2: Config Directory

Create `~/.config/whatsapp-mcp/.env`:

```
WHATSAPP_ACCESS_TOKEN=EAABwzLixnjYBO...
WHATSAPP_PHONE_NUMBER_ID=123456789012345
WHATSAPP_BUSINESS_ACCOUNT_ID=123456789012345
```

### Option 3: 1Password

Create a 1Password item titled "WhatsApp Business Platform" with fields:
- `access token`: Your access token
- `phone number id`: Your phone number ID
- `business account id`: Your business account ID

## Testing Your Credentials

After configuring, test with:

```bash
cd execution/mcp-servers/whatsapp
python3.11 whatsapp_mcp_server.py
```

If credentials are correct, the server should start without authentication errors.

## Troubleshooting

### "Invalid OAuth access token"
- Token may have expired (temporary tokens expire in ~1 hour)
- Generate a new token
- For production, use System User with permanent token

### "Phone number ID not found"
- Verify you copied the Phone Number ID (not the phone number itself)
- Check that the phone number is registered in Meta Business Manager

### "Permission denied"
- Ensure your app has WhatsApp product added
- Check that you've granted necessary permissions
- Verify Business Account ID matches your WhatsApp Business account

## Additional Resources

- **Meta for Developers**: https://developers.facebook.com/
- **WhatsApp Business Platform API Docs**: https://developers.facebook.com/docs/whatsapp/cloud-api
- **Graph API Explorer**: https://developers.facebook.com/tools/explorer/
- **Meta Business Manager**: https://business.facebook.com/













