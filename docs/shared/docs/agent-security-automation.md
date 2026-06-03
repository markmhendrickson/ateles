# Agent Security & Automation

**Purpose:** Security requirements (1Password CLI) and browser automation requirements (Playwright).

**Last Updated:** 2025-01-23

---

## Security Requirements

### 1Password CLI Usage

**CRITICAL:** Never call the 1Password `op` CLI in a way that could surface secrets into agent chat output.

- **Prohibited:** Using `run_terminal_cmd` with `op read`, `op item get`, or any command that could return sensitive values (including structure + values)
- **Required:** All 1Password access must be done via local scripts the user runs themselves
- **Pattern:** Generate scripts that read from `op` internally, never print secrets, and only write to local files (`.env`, config)

**Acceptable Usage Guidelines:**

- **Script generation only:** Agents may generate or modify local Python/shell scripts that:
  - Call `op read` or related commands **only inside the script**, and
  - Never print or log secret values, and
  - Only write secrets into local files such as `.env` or in‑memory variables.
- **User-driven CLI calls:** Agents may suggest `op` commands for the user to run manually in their own terminal, but:
  - Must not run those commands via tools, and
  - Must not request that the user paste secret values back into chat.
- **Structure-only reasoning:** When reasoning about 1Password usage, agents should:
  - Work from known vault/item/field names supplied by the user or docs,
  - Avoid any automated discovery that could reveal secrets into chat,
  - Prefer descriptive placeholders (e.g., `op://<Vault>/<Item>/<Field>`) over concrete secret values.

---

## Browser Automation Requirements

### Standard Tool: Playwright

**MANDATORY:** For all browser-based automation tasks, agents must use **Playwright** instead of Cursor's native browser tools.

**Rationale:**
- More reliable for authenticated sessions and complex interactions
- Better support for session state persistence and credential management
- Cross-browser compatibility (Chromium, Firefox, WebKit)
- Established pattern already used in this repository (see `scripts/extract_twilio_creds_playwright.py`)

### Standard Pattern

**1. Create Playwright Script:**
```python
#!/usr/bin/env python3
"""
Browser automation script using Playwright.
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).parent.parent
AUTH_STATE_DIR = PROJECT_ROOT / "playwright" / ".auth"
AUTH_STATE_DIR.mkdir(parents=True, exist_ok=True)

async def browser_task():
    """Standard browser automation pattern."""
    async with async_playwright() as p:
        # Launch browser (headless=False for initial login, True for automation)
        browser = await p.chromium.launch(headless=False)
        
        # Load saved auth state if available, otherwise create new context
        auth_state_path = AUTH_STATE_DIR / "auth_state.json"
        if auth_state_path.exists():
            context = await browser.new_context(storage_state=str(auth_state_path))
        else:
            context = await browser.new_context()
        
        page = await context.new_page()
        
        # Perform browser interactions
        await page.goto("https://example.com")
        # ... automation logic ...
        
        # Save auth state after login (if applicable)
        await context.storage_state(path=str(auth_state_path))
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(browser_task())
```

**2. Security Best Practices:**

- **Credentials:** Never hardcode credentials. Use environment variables or 1Password integration (via scripts, per Security Requirements above)
- **1Password Browser Extension (MOST SECURE):** Prefer using existing Chrome profile with 1Password extension installed - credentials never leave 1Password's secure storage
- **Auth State Files:** Store in `playwright/.auth/` directory (already in `.gitignore`)
- **Session Reuse:** Save authenticated state after login to avoid repeated authentication
- **Headless Mode:** Use `headless=False` for initial login/2FA, switch to `headless=True` for automated runs

**2a. 1Password Browser Extension Integration (RECOMMENDED):**

**Most Secure Approach (No Chrome Restart Required):** Load 1Password extension into Playwright:

```python
# Find and load 1Password extension
extension_path = find_1password_extension()
browser = await p.chromium.launch(
    headless=False,
    args=[
        f"--disable-extensions-except={extension_path}",
        f"--load-extension={extension_path}",
    ],
)
```

**Benefits:**
- Credentials never leave 1Password's secure storage
- 1Password extension handles auto-fill automatically
- No credential exposure to scripts or LLM
- **Works even if Chrome is already running** (uses separate browser instance)

**Alternative (Requires Chrome Closed):** Use existing Chrome profile:
```python
# Requires Chrome to be closed - uses your actual browser profile
browser = await p.chromium.launch_persistent_context(
    user_data_dir=str(Path.home() / "Library/Application Support/Google/Chrome/Default"),
    headless=False,
)
```

**Scripts (in order of preference):**
- `scripts/twilio_login_1password_extension.py` - **RECOMMENDED** - Loads 1Password extension (works with Chrome open)
- `scripts/twilio_login_chrome_profile.py` - Uses existing Chrome profile (requires Chrome closed)
- `scripts/twilio_login_playwright.py` - Uses 1Password CLI (fallback, still secure)

**3. File Organization:**

- **Scripts:** Place browser automation scripts in `/execution/scripts/` directory
- **Auth State:** Store in `/playwright/.auth/` (gitignored)
- **Naming:** Use descriptive names like `check_cursor_usage.py`, `extract_credentials.py`

**4. Error Handling:**

- Handle network timeouts and page load failures gracefully
- Use `domcontentloaded` instead of `networkidle` for more reliable page loads
- Wrap timeout operations in try/except to continue on slow pages
- Provide clear error messages for authentication failures
- Log actions but never log credentials or sensitive tokens

**5. Best Practices:**

**5a. Browser Launch Options:**
```python
# Minimize browser window (may not work on macOS)
browser = await p.chromium.launch(
    headless=False,
    args=[
        '--disable-blink-features=AutomationControlled',  # Avoid detection
        '--disable-dev-shm-usage',
        '--no-sandbox',
        '--start-minimized',  # Start minimized (Linux/Windows)
    ]
)

# Realistic browser fingerprint
context_options = {
    'viewport': {'width': 1920, 'height': 1080},
    'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36...',
    'locale': 'en-US',
    'timezone_id': 'America/Los_Angeles',
}
```

**5b. Polling Pattern for User Interaction:**
```python
# Poll every N seconds instead of single long wait
max_attempts = 30  # 30 attempts * 10 seconds = 5 minutes
for attempt in range(max_attempts):
    await asyncio.sleep(10)
    current_url = page.url
    print(f"Checking status... ({attempt + 1}/{max_attempts})")
    
    if condition_met(current_url):
        print("✓ Condition met, continuing...")
        break
```

**5c. API Response Interception:**
```python
# Intercept and parse API responses for data extraction
api_responses = []
parsed_data = {}

async def handle_response(response):
    url = response.url
    if 'api' in url and 'usage' in url:
        try:
            json_data = await response.json()
            # Extract and parse data
            if 'data' in json_data:
                parsed_data.update(extract_usage_data(json_data['data']))
        except:
            pass

page.on('response', handle_response)
# Navigate to trigger API calls
await page.goto("https://example.com/dashboard")
await asyncio.sleep(3)  # Wait for API calls
```

**5d. Timeout Handling:**
```python
# Use domcontentloaded instead of networkidle (more reliable)
try:
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
except:
    pass  # Continue anyway if page is slow

# For page load states, use shorter timeouts with fallback
try:
    await page.wait_for_load_state("domcontentloaded", timeout=10000)
except:
    pass  # Continue even if page is slow
```

**5e. Historical Data Extraction:**
```python
# Extract and aggregate historical data from API responses
from datetime import datetime, timedelta

# Calculate date range
now = datetime.now()
first_day_this_month = datetime(now.year, now.month, 1)
last_day_last_month = first_day_this_month - timedelta(days=1)
first_day_last_month = datetime(last_day_last_month.year, last_day_last_month.month, 1)

# Filter and aggregate
last_month_start_ms = int(first_day_last_month.timestamp() * 1000)
last_month_end_ms = int((first_day_this_month.timestamp() - 1) * 1000)

last_month_data = [
    item for item in daily_metrics
    if last_month_start_ms <= int(item['date']) <= last_month_end_ms
]

# Sum metrics
total_usage = sum(item.get('usage', 0) for item in last_month_data)
```

**6. Reference Examples:**

- **Credential Extraction:** `scripts/extract_twilio_creds_playwright.py` - Basic pattern with manual sign-in
- **Usage Monitoring:** `scripts/check_cursor_usage.py` - **COMPREHENSIVE EXAMPLE** - Includes:
  - API response interception and parsing
  - Historical data extraction (last month's usage)
  - Polling pattern for sign-in detection
  - Minimized browser launch
  - Auth state persistence
  - Error handling with graceful fallbacks
- **Form Automation:** `scripts/export_minted_contacts.py` (Selenium - migrate to Playwright pattern)

**Prohibited:**
- ❌ Using Cursor's native browser tools (`mcp_cursor-browser-extension_*`) for agent automation tasks
- ❌ Hardcoding credentials in scripts
- ❌ Committing auth state files to git
- ❌ Logging sensitive tokens or credentials

**Required:**
- ✅ Use Playwright for all browser automation
- ✅ Store auth state in `playwright/.auth/` directory
- ✅ Use environment variables or 1Password for credentials
- ✅ Follow the standard pattern above






