# Credential Management

This repository uses 1Password CLI for secure credential management, eliminating the need for manual `.env` file management. Credentials are retrieved on-demand from 1Password vaults without storing secrets in the repository.

## Setup

### 1. Install 1Password CLI

Quick Setup (Recommended):
```bash
./scripts/setup-1password-cli.sh
```

Manual Installation (macOS):
```bash
brew install --cask 1password-cli
```

Other platforms: See https://developer.1password.com/docs/cli/get-started

### 2. Configure Account

Choose one of these authentication methods:

Option A: Desktop App Integration (Recommended)
1. Open 1Password desktop app
2. Enable CLI integration in settings
3. Run: `eval $(op signin)`

Option B: Manual Account Setup
```bash
op account add
# Follow prompts to add your account
eval $(op signin)
```

Option C: Service Account (For Automation)
```bash
export OP_SERVICE_ACCOUNT_TOKEN="your-service-account-token"
```

Option D: Connect Server
```bash
export OP_CONNECT_HOST="your-connect-host"
export OP_CONNECT_TOKEN="your-connect-token"
```

See https://developer.1password.com/docs/cli/get-started for detailed setup instructions.

### 3. Verify Installation

```bash
op account list
```

## Usage

### Python Scripts

Import and use the credential utility:

```python
from scripts.credentials import get_credential, get_credential_by_domain

# Get username and password
email, password = get_credential("Minted.com")

# Get specific field
api_key = get_credential("Service Name", field="api_key")

# Use specific vault
token = get_credential("API Service", vault="Work", field="token")

# Search by domain
email, password = get_credential_by_domain("minted.com")
```

### Command Line

Test credential retrieval:

```bash
python scripts/credentials.py "Minted.com"
python scripts/credentials.py "Minted.com" password
python scripts/credentials.py "API Service" api_key Work
```

---

## 1Password Item Organization

### Best Practices

1. Use descriptive titles - Match service names exactly (e.g., "Minted.com", "GitHub")
2. Set URL fields - Enables domain-based lookup
3. Use consistent field labels - Standard labels work best:
   - `username` or `email` for usernames
   - `password` for passwords
   - `api_key`, `token`, `access_token` for API credentials
4. Tag items - Use tags for organization (optional, not required for lookup)

### Field Mapping

The credential utility automatically maps common field variations:

- Username: `username`, `user`, `login`, `email`, `e-mail`
- Password: `password`, `pass`
- API Key: `api_key`, `api key`, `apikey`
- Token: `token`, `access token`, `access_token`

## Migration from .env Files

### Before (using .env)

```python
import os
from dotenv import load_dotenv

load_dotenv()
email = os.environ["minted_email"]
password = os.environ["minted_password"]
```

### After (using 1Password)

```python
from scripts.credentials import get_credential

email, password = get_credential("Minted.com")
```

### Migration Steps

1. Identify all .env variables used in scripts
2. Create/verify 1Password items for each service
3. Update scripts to use `get_credential()` instead of `os.environ`
4. Remove .env file (or keep for legacy scripts, but don't add new entries)
5. Test scripts to ensure credentials are retrieved correctly

## Security Benefits

1. No secrets in repository - Credentials never stored in code or config files
2. Centralized management - All credentials managed in 1Password
3. Access control - 1Password vault permissions control access
4. Audit trail - 1Password tracks credential access
5. Automatic updates - Changes in 1Password are immediately available
6. No manual sync - No need to update `.env` files when credentials change

## Error Handling

### Common Errors

1Password CLI not found:
```
Error: 1Password CLI not found. Install from: https://developer.1password.com/docs/cli
```
Solution: Install 1Password CLI and ensure it's in PATH

Not authenticated:
```
Error: 1Password CLI error: You are not currently signed in
```
Solution: Run `eval $(op signin)`

Item not found:
```
Error: Could not find 1Password item matching 'Service Name'
```
Solution: Verify item title matches exactly, or use `item_id` parameter

Field not found:
```
Error: Field 'api_key' not found in 1Password item
```
Solution: Check field label in 1Password item, or use correct field name

## Integration Examples

### Example: Minted.com Export Script

Before:
```python
import os
from dotenv import load_dotenv

load_dotenv()
minted_email = os.environ.get("minted_email")
minted_password = os.environ.get("minted_password")
```

After:
```python
from scripts.credentials import get_credential

minted_email, minted_password = get_credential("Minted.com")
```

### Example: API Key Retrieval

```python
from scripts.credentials import get_credential

# Get API key for service
api_key = get_credential("Service Name", field="api_key")

# Use in API request
headers = {"Authorization": f"Bearer {api_key}"}
```

### Example: Multiple Credentials

```python
from scripts.credentials import get_credential

# Get credentials for different services
github_token = get_credential("GitHub", field="token")
aws_access_key = get_credential("AWS", field="access_key_id")
aws_secret_key = get_credential("AWS", field="secret_access_key")
```

## Troubleshooting

### Session Expired

1Password CLI sessions expire after a period of inactivity. If you get authentication errors:

```bash
eval $(op signin)
```

Note: The credential utility requires an active 1Password CLI session. If running scripts in automated contexts, consider using a service account token (see Setup section).

### Item Title Mismatch

If item lookup fails, check exact title in 1Password:

```bash
op item list --format=json | grep -i "service name"
```

Then use exact title or provide `item_id` directly.

### Field Label Variations

If field lookup fails, check available fields:

```bash
op item get "<item-id>" --format=json | jq '.fields[] | {label: .label, id: .id}'
```

Update field label in 1Password or use correct label name.

## Future Enhancements

Potential improvements:

1. Caching - Cache credentials in memory for script duration (with TTL)
2. Vault detection - Auto-detect vault based on service type
3. Field mapping config - Configurable field name mappings
4. Credential validation - Verify credentials before returning
5. Multi-account support - Support multiple 1Password accounts

