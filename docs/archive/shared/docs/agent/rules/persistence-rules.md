# Agent Persistence Rules

**Purpose:** Rules for automatically persisting personal data, contact information, and account information.

**Last Updated:** 2025-01-23

**Related:** `/shared/docs/agent/rules/persistence-requirements.md` for contact and email persistence requirements.

---

## Personal Data Persistence

**MANDATORY:** When the user shares evergreen personal data during conversations, automatically save it to `/strategy/reference/personal-information.md` for future reference.

**Evergreen personal data includes:**
- Personal identifiers (name, NIF/NIE, passport numbers, etc.)
- Contact information (email, phone, addresses)
- Account numbers, credentials, or identifiers
- Property information (addresses, property details)
- Tax identification numbers
- Bank account details
- Any personal information that would be useful for future financial tasks

**Exclusions (do not save):**
- Transient conversation context
- One-time task-specific data
- Sensitive credentials (passwords, API keys) - these should be stored securely elsewhere

**Process:**
1. Identify when personal data is shared
2. Update `/strategy/reference/personal-information.md` immediately with the new information
3. Maintain existing structure and formatting
4. Update the "Last Updated" date
5. Do not prompt the user - save automatically

**Rationale:** Ensures personal information is available for future financial tasks, forms, and processes without requiring the user to re-share it.

---

## Contact Information Extraction

**MANDATORY:** When contact information is extracted from emails, documents, or other sources, automatically save it to `$DATA_DIR/contacts/contacts.parquet` following the contacts schema.

**Contact information includes:**
- Names and titles
- Phone numbers
- Email addresses
- Physical addresses
- Company/organization names
- Business relationships (banking, legal, vendors, etc.)
- Notes about the contact's role or purpose

**Process:**
1. Create snapshot of `$DATA_DIR/contacts/contacts.parquet` before modification
2. **Check for existing contacts** to avoid duplicates:
   - Search by name (case-insensitive, handle accents/variations)
   - Search by email (if available)
   - Search by phone (normalize formats, handle country codes)
   - Search by company/platform combination
   - If match found: **UPDATE existing contact** (merge new information, preserve existing data)
   - If no match: **CREATE new contact**
3. **For new contacts**, build entry following `$DATA_DIR/schemas/contacts_schema.json`:
   - Generate unique `contact_id` (16-character UUID)
   - Set `contact_type` (e.g., 'business', 'personal')
   - Set `category` (e.g., 'banking', 'legal', 'vendor')
   - Set `platform` (e.g., 'Ibercaja', 'Gmail')
   - Include all available contact details
   - Add notes describing the contact's role/purpose
   - Set date fields (`first_contact_date`, `last_contact_date`, `created_date`, `updated_date`)
4. **For existing contacts**, update fields:
   - Merge new information with existing (don't overwrite non-empty fields unless new data is more complete)
   - Update `last_contact_date` to current date
   - Update `updated_date` to current date
   - Append new notes to existing notes (if applicable)
   - Update any fields that have new/more complete information
5. Save updated contacts parquet file
6. Do not prompt the user - save automatically

**Duplicate Detection Logic:**
- **Name matching:** Normalize names (lowercase, remove extra spaces), handle accent variations
- **Email matching:** Exact match (case-insensitive)
- **Phone matching:** Normalize phone numbers (remove spaces, dashes, handle country codes)
- **Company/Platform matching:** If name matches and company/platform matches, likely same contact
- **Fuzzy matching:** If name is very similar and other fields match, treat as duplicate

**Examples:**
- ✅ Email from banking manager → Check for existing contact, update if found or create new
- ✅ Document with vendor contact → Check for duplicates, merge if existing contact found
- ✅ Business card information → Check by name/phone/email, update existing or create new
- ❌ Skipping duplicate check and creating duplicate contacts is WRONG
- ❌ Not creating snapshot before adding/updating contact is WRONG
- ❌ Overwriting existing contact data without merging is WRONG

**Rationale:** Centralizes contact information for easy reference in future financial tasks, vendor management, and relationship tracking.

**Reference:** `/shared/docs/agent/rules/persistence-requirements.md` for complete contact persistence requirements.

---

## Account Information Persistence

**MANDATORY:** When new accounts (online services, platforms, tools) are created or accessed, automatically save account information to `$DATA_DIR/user_accounts/user_accounts.parquet` following the user_accounts schema.

**Account information includes:**
- Service/platform name (title)
- Domain/URL
- Account email
- Category (hosting, email, payment, etc.)
- Status (active, inactive)
- Tags for categorization
- Primary URL for access
- Creation/update dates

**Process:**
1. Create snapshot of `$DATA_DIR/user_accounts/user_accounts.parquet` before modification
2. Check for existing account by domain or title to avoid duplicates
3. If account exists: Update with new information
4. If account doesn't exist: Create new entry following schema
5. Save updated user_accounts parquet file
6. Do not prompt the user - save automatically

**Examples:**
- ✅ "Set up Netlify account" → Save Netlify account details to user_accounts
- ✅ "Created GitHub account" → Save GitHub account details to user_accounts
- ✅ "Signed up for service X" → Save service account details to user_accounts
- ❌ Skipping account persistence is WRONG

**Rationale:** Centralizes account information for easy reference, subscription tracking, and service management.

