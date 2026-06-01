# Scripts Directory

This directory contains automation scripts for workflows.

## Setup

### Install Dependencies

```bash
# From the repo root directory
pip install -r scripts/requirements.txt
```

Or install individually:
```bash
pip install pandas pyarrow requests python-dotenv
```

### Configure Environment Variables

#### Option 1: Sync from 1Password (Recommended)

```bash
# Ensure 1Password CLI is signed in
op signin

# Sync credentials to .env
python scripts/op_sync_env_from_1password.py
```

This will create/update `.env` in the repo root with:
- `COINBASE_API_KEY`
- `COINBASE_API_SECRET`
- `COINBASE_API_PASSPHRASE`
- `HIRO_PLATFORM_API_KEY`

#### Option 2: Manual .env Configuration

Create `.env` in repo root:

```env
COINBASE_API_KEY="your_key_here"
COINBASE_API_SECRET="your_secret_here"
COINBASE_API_PASSPHRASE="your_passphrase_here"
HIRO_PLATFORM_API_KEY="your_hiro_key_here"
```

**Note:** Never commit `.env` to git (it's already in `.gitignore`)

---

## Development Tools

### Symlink Docs to Cursor Rules

Makes agent rule files (files with "-rules" suffix) available as Cursor rules that are automatically loaded into context:

```bash
./execution/scripts/symlink-docs-to-cursor-rules.sh
```

**What it does:**
- Creates symlinks from `docs/` to `.cursor/rules/` for all `.md` files with "-rules" suffix
- Prefixes symlink names with "ateles-" to avoid conflicts with other repos
- Ensures `docs/` remains the single source of truth
- Changes to files in `docs/` are immediately reflected in `.cursor/rules/`

**Behavior:**
- Removes all existing symlinks with "ateles-" prefix before creating new ones
- Only symlinks files with "-rules" suffix (e.g., `communication-rules.md`, `data-rules.md`)
- Symlink names are prefixed (e.g., `communication-rules.md` -> `ateles-communication-rules.md`)
- Skips files that already exist as regular files (preserves customizations)
- Uses relative paths for portability

**Notes:**
- Cursor automatically loads all `.md` files from `.cursor/rules/` into context
- Only agent rule files (with "-rules" suffix) are symlinked, not all documentation
- Run from repository root
- Safe to run multiple times (idempotent)

**Related:**
- `foundation/scripts/setup-cursor-rules.sh` - Symlinks foundation rules
- `docs/decision_framework_rules.mdc` - Decision-making framework and document hierarchy

---

## STX Balance Scripts

### Fetch On-Chain STX Balances

Queries Stacks blockchain API for Ledger wallet balances:

```bash
python scripts/stx_fetch_onchain_balances.py
```

**Requirements:**
- `requests` library
- No credentials needed (public API)

**Configuration:**
- Edit `STACKS_ADDRESSES` dict in the script to add/remove wallet addresses

---

### Fetch Coinbase STX Balances

Queries Coinbase API for STX account balances:

```bash
python scripts/stx_fetch_coinbase_stx_balances.py
```

**Requirements:**
- `requests` and `python-dotenv` libraries
- Coinbase API credentials in `.env` or 1Password

**Credential Resolution Order:**
1. Environment variables (from `.env` or system)
2. 1Password via `op` CLI (fallback)

**Setup:**
1. Sync credentials: `python scripts/op_sync_env_from_1password.py`
2. Or manually set env vars: `COINBASE_API_KEY`, `COINBASE_API_SECRET`

---

## 1Password Integration

### Sync Secrets to .env

```bash
python scripts/op_sync_env_from_1password.py
```

**What it does:**
- Reads secrets from 1Password using `op` CLI
- Writes them to `.env` (or specified file)
- Never prints secret values

**Configuration:**
- Edit `ENV_TO_OP_REF` dict in the script to add/remove mappings

**Prerequisites:**
- 1Password CLI installed (`brew install --cask 1password-cli`)
- Signed in: `op signin`

---

## Security

### Environment Variables
- `.env` is in `.gitignore` - never commit it
- Scripts load `.env` automatically via `python-dotenv`
- System environment variables take precedence over `.env`

### 1Password CLI
- Scripts never print secrets
- All `op` calls happen inside scripts, not via shell commands
- Safe structure queries only in agent context (vault/item/field names, no values)

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'requests'"

Install dependencies:
```bash
pip install -r scripts/requirements.txt
```

### "Could not retrieve credentials from 1Password"

Option 1 - Sync to .env:
```bash
python scripts/op_sync_env_from_1password.py
```

Option 2 - Set environment variables manually:
```bash
export COINBASE_API_KEY="your_key"
export COINBASE_API_SECRET="your_secret"
```

### "1Password CLI error"

Ensure you're signed in:
```bash
op signin
```

Check vault/item names match:
```bash
op vault list
op item list --vault Wallets
```

---

**Last Updated:** 2025-12-19
