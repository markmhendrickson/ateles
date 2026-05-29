# Data Publishing Privacy Guidelines

**Purpose:** Define what data from `$DATA_DIR` is safe to publish on public website vs. what should never be published.

**Last Updated:** 2026-01-13

---

## Core Principle

**Default to Private:** All data in `$DATA_DIR` is considered private by default unless explicitly marked as public.

---

## Publishing Categories

### ✅ Safe to Publish (with Curation)

Data types that CAN be published if properly curated and marked as public:

1. **Projects** (`projects.parquet`)
   - **Requirement:** Must have `public: true` flag
   - **Must include:** Non-empty name, description
   - **Must exclude:** Internal project IDs, sensitive business details, private client work
   - **Examples:** Open-source projects, public portfolio work, published products

2. **Public Speaking Events** (`events.parquet`)
   - **Requirement:** Must add `public: boolean` field and mark as `true`
   - **Must include:** Event name, date, description
   - **Must exclude:** Private meetings, personal events
   - **Examples:** Conference talks, podcast appearances, public workshops

3. **Professional Goals** (`goals.parquet`)
   - **Requirement:** Must add `public: boolean` field and mark as `true`
   - **Must include:** Goal name, description (non-sensitive)
   - **Must exclude:** Financial goals, personal goals, strategic business plans
   - **Examples:** "Launch open-source project", "Speak at 3 conferences"

4. **Domain Registrations** (`domains.parquet`)
   - **Note:** Domain registrations are already public information via WHOIS
   - **Safe to publish:** Domain name, registrar, status (not expiring)
   - **Exclude:** Expiring domains, internal-use-only domains
   - **Current status:** Only 1 domain (humans.name, expiring) - not suitable

5. **Public Notes/Articles** (`notes.parquet`)
   - **Requirement:** Must add `public: boolean` field and mark as `true`
   - **Must include:** Title, content appropriate for public consumption
   - **Must exclude:** Client work, personal notes, internal documentation
   - **Examples:** Published essays, public writing samples, blog post drafts marked for publication

6. **Transcriptions** (`transcriptions.parquet`)
   - **Requirement:** Must add `public: boolean` field and mark as `true`
   - **Must include:** Public presentations, podcasts, interviews
   - **Must exclude:** Private meetings, client calls, personal recordings

---

### ⚠️ Needs Manual Review

Data types that MIGHT be suitable but require careful review:

1. **Movies/Media** (`movies.parquet`)
   - Public media consumption is generally safe but not particularly useful for professional website
   - Review for professional relevance

2. **Locations** (`locations.parquet`)
   - Could include public venues for speaking events
   - Exclude: Home address, private locations, current location

3. **Outcomes** (`outcomes.parquet`)
   - Could include public professional outcomes
   - Exclude: Financial outcomes, private goals

---

### ❌ NEVER Publish

Data types that should NEVER be published under any circumstances:

#### Financial Data (22 types)
- `account_identifiers` - Bank/account numbers
- `accounts` - Financial accounts
- `asset_types`, `asset_values` - Asset holdings
- `balances` - Account balances
- `bank_certificates` - Banking documents
- `crypto_transactions` - Cryptocurrency transactions
- `equity_units` - Stock options, RSUs
- `financial_strategies` - Financial plans
- `fixed_costs` - Recurring expenses
- `flows` - Cash flows
- `holdings` - Portfolio holdings
- `income` - Income data
- `investments` - Investment details
- `liabilities` - Debts
- `orders` - Trading orders
- `properties` - Real estate holdings
- `property_equipment` - Property details
- `tax_events` - Tax transactions
- `tax_filings` - Tax returns
- `transactions` - Financial transactions
- `transfers` - Asset transfers
- `wallets` - Financial accounts

#### Personal Identifiers (7 types)
- `addresses` - Physical addresses
- `contacts` - Contact information
- `emails` - Email messages
- `messages` - SMS/messages
- `payroll_documents` - Payroll information
- `relationships` - Personal relationships
- `user_accounts` - Account credentials

#### Health Data (5 types)
- `exercises` - Exercise definitions
- `foods` - Food database
- `meals` - Meal tracking
- `sets` - Workout sets
- `workouts` - Workout routines

#### Internal Operations (10+ types)
- `daily_triages` - Daily task management
- `env_var_mappings` - Environment configuration
- `execution_plans` - Project execution plans
- `logs` - System logs
- `mcp_server_integrations` - Server configuration
- `processes` - Internal processes
- `task_attachments` - Task files
- `task_comments` - Task comments
- `task_dependencies` - Task relationships
- `task_stories` - Task history
- `tasks` - Task management

#### Private/Sensitive Notes (3 types)
- `arguments` - Personal arguments
- `disputes` - Disputes
- `emotions` - Emotion tracking

#### Other Private (5+ types)
- `beliefs` - Personal beliefs (mostly empty, not professionally relevant)
- `companies` - Company data (context-dependent, likely private)
- `contracts` - Legal contracts
- `people` - Personal relationships
- `purchases` - Personal purchases
- `snapshots` - Backup data

---

## Implementation Guidelines

### Adding Public Flags

For data types that could have public records, add a `public: boolean` field:

```python
# Example: Add public flag to goals
df = pd.read_parquet(DATA_DIR / "goals" / "goals.parquet")

# Add public column if it doesn't exist
if "public" not in df.columns:
    df["public"] = False

# Mark specific goals as public (manual selection)
df.loc[df["goal_id"] == "some-id", "public"] = True

# Save updated parquet
df.to_parquet(DATA_DIR / "goals" / "goals.parquet", index=False)
```

### Validation Checklist

Before marking any record as `public: true`, verify:

- [ ] **No PII** - No personal identifiable information
- [ ] **No financial data** - No account numbers, balances, transactions
- [ ] **No sensitive business info** - No confidential client work, internal strategies
- [ ] **No private relationships** - No personal contacts, private communications
- [ ] **Complete and professional** - All required fields filled, appropriate for public consumption
- [ ] **Contextually appropriate** - Makes sense to publish on professional website

### Content Transformation

When preparing public data for website:

1. **Field Selection**
   - Include: name, description, date, status (if public-appropriate)
   - Exclude: internal IDs, import metadata, private notes fields

2. **Content Sanitization**
   - Remove internal references (e.g., Asana IDs, Notion URLs)
   - Remove sensitive notes or context
   - Ensure all URLs/links are appropriate

3. **Format Consistency**
   - Use ISO date formats
   - Standardize status values
   - Ensure consistent naming conventions

---

## Current State (2026-01-13)

### Data Suitable for Publishing

**None.** Analysis found:
- 0 projects with `public: true`
- 0 goals with public flag
- 1 domain (expiring, not suitable)

### Recommendation

**Continue using manually-curated JSON files** in `execution/website/markmhendrickson/react-app/src/data/`:
- `timeline.json` - Career timeline (manually curated)
- `links.json` - Social media links (manually curated)
- `posts.json` - Blog posts (manually curated)

**If additional website content is needed:**
1. Manually curate appropriate records from `$DATA_DIR`
2. Add `public: true` flag to those records
3. Run extraction and transformation scripts
4. Review generated JSON before publishing

---

## Privacy Risk Matrix

| Data Type | Sensitivity Level | Public Flag | Recommendation |
|-----------|------------------|-------------|----------------|
| Financial | CRITICAL | N/A | Never publish |
| Health | HIGH | N/A | Never publish |
| Personal IDs | HIGH | N/A | Never publish |
| Internal Ops | MEDIUM-HIGH | N/A | Never publish |
| Projects | LOW-MEDIUM | Required | Curate carefully |
| Goals | LOW-MEDIUM | Required | Curate carefully |
| Events | LOW | Required | Curate carefully |
| Notes | MEDIUM | Required | Review individually |
| Domains | PUBLIC | N/A | Safe (already public) |

---

## Maintenance

### Regular Review

- **Monthly:** Review any new data types added to `$DATA_DIR`
- **Before publishing:** Re-run privacy analysis
- **After marking public:** Verify no sensitive data leaked

### Audit Trail

When marking data as public:
- Document why it's safe to publish
- Record who approved (if applicable)
- Keep audit log of what was published

---

## Related Documents

- Analysis Report: `tmp/website_data_analysis/ANALYSIS_REPORT.md`
- Extraction Script: `execution/scripts/extract_public_data.py`
- Analysis Script: `execution/scripts/analyze_data_for_website.py`
- Website Data Directory: `execution/website/markmhendrickson/react-app/src/data/`
