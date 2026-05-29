# Data Publishing Transformation Documentation

**Purpose:** Document field mappings and transformation logic for converting `$DATA_DIR` data to website JSON format.

**Last Updated:** 2026-01-13

---

## Overview

This document defines how data from parquet files in `$DATA_DIR` should be transformed into JSON files for the website located at `execution/website/markmhendrickson/react-app/src/data/`.

**Current State:** No automated transformation is currently implemented, as no data has `public: true` flags. The website uses manually-curated JSON files.

---

## Website JSON Structure

All website data files follow this general structure:

```json
[
  {
    "field1": "value",
    "field2": "value",
    "field3": "value"
  }
]
```

Files are:
- **Location:** `execution/website/markmhendrickson/react-app/src/data/`
- **Format:** JSON arrays of objects
- **Naming:** lowercase with underscores (e.g., `timeline.json`, `links.json`)
- **Encoding:** UTF-8

---

## Existing Website Data Files

### timeline.json

**Source:** Manually curated (not from `$DATA_DIR`)

**Structure:**
```json
[
  {
    "role": "Founder",
    "company": "Startup (Neotoma & Ateles)",
    "date": "2025 – Present · Barcelona, Spain",
    "description": ["Array of description paragraphs"]
  }
]
```

**Fields:**
- `role` (string) - Job title or role
- `company` (string) - Company or organization name
- `date` (string) - Date range and location (formatted)
- `description` (array of strings) - Description paragraphs

### links.json

**Source:** Manually curated (not from `$DATA_DIR`)

**Structure:**
```json
[
  {
    "name": "GitHub",
    "url": "https://github.com/markmhendrickson",
    "icon": "Github",
    "description": "Code repositories"
  }
]
```

**Fields:**
- `name` (string) - Link name
- `url` (string) - Full URL
- `icon` (string) - Icon name (from lucide-react)
- `description` (string) - Optional description

### posts.json

**Source:** Manually curated markdown files

**Structure:**
```json
[
  {
    "slug": "post-slug",
    "title": "Post Title",
    "excerpt": "Brief description",
    "published": true,
    "publishedDate": "2026-01-01",
    "category": "essay",
    "readTime": 5,
    "tags": ["tag1", "tag2"]
  }
]
```

**Fields:**
- `slug` (string) - URL slug (matches markdown filename)
- `title` (string) - Post title
- `excerpt` (string) - Short description
- `published` (boolean) - Publication status
- `publishedDate` (string, YYYY-MM-DD or null) - Publication date
- `category` (string) - Category (essay, technical, article)
- `readTime` (number) - Estimated reading time in minutes
- `tags` (array of strings) - Tags

---

## Proposed Transformations

### Projects → projects.json

**Source:** `$DATA_DIR/projects/projects.parquet` (where `public: true`)

**Parquet Schema:**
- `project_id` (string)
- `name` (string)
- `description` (string)
- `status` (string)
- `start_date` (date)
- `end_date` (date)
- `public` (boolean)
- `icon` (string)
- `outcome_name` (string)
- `notes` (string)
- ... other fields

**Website JSON Structure:**
```json
[
  {
    "id": "project-id",
    "name": "Project Name",
    "description": "Project description",
    "status": "active",
    "startDate": "2025-01-01",
    "endDate": null,
    "icon": "icon-name",
    "outcome": "Outcome name"
  }
]
```

**Field Mappings:**
- `project_id` → `id` (strip internal prefixes if any)
- `name` → `name` (required, exclude if empty)
- `description` or `notes` → `description` (combine if both exist)
- `status` → `status` (normalize: active, completed, planned)
- `start_date` → `startDate` (ISO format YYYY-MM-DD)
- `end_date` → `endDate` (ISO format or null)
- `icon` → `icon` (optional)
- `outcome_name` → `outcome` (optional)

**Exclusions:**
- `asana_project_gid` - Internal ID
- `import_date`, `import_source_file` - Metadata
- `html_notes` - Internal format
- `owner_gid`, `members_gids` - Internal IDs
- Any field with "gid" suffix - Internal IDs

**Validation:**
- Require non-empty `name`
- Require non-empty `description`
- Require `public: true`
- Exclude records with sensitive keywords in description

### Goals → goals.json

**Source:** `$DATA_DIR/goals/goals.parquet` (where `public: true`)

**Parquet Schema:**
- `goal_id` (string)
- `name` (string)
- `status` (string)
- `priority` (string)
- `notes` (string)
- `public` (boolean) - **needs to be added**

**Website JSON Structure:**
```json
[
  {
    "id": "goal-id",
    "name": "Goal name",
    "status": "active",
    "priority": "high",
    "description": "Goal description"
  }
]
```

**Field Mappings:**
- `goal_id` → `id`
- `name` → `name` (required)
- `status` → `status` (normalize: active, completed, archived)
- `priority` → `priority` (optional, normalize: high, medium, low)
- `notes` → `description`

**Exclusions:**
- `parent_goals` - Internal relationships
- `import_date`, `import_source_file` - Metadata

**Validation:**
- Require non-empty `name`
- Require `public: true`
- Exclude financial goals
- Exclude overly personal goals

### Events → events.json

**Source:** `$DATA_DIR/events/events.parquet` (where `public: true`)

**Parquet Schema:**
- `event_id` (string)
- `name` (string)
- `date` (date)
- `location` (string)
- `description` (string)
- `event_type` (string)
- `public` (boolean) - **needs to be added**

**Website JSON Structure:**
```json
[
  {
    "id": "event-id",
    "name": "Event name",
    "date": "2026-01-15",
    "location": "Barcelona, Spain",
    "type": "conference",
    "description": "Event description"
  }
]
```

**Field Mappings:**
- `event_id` → `id`
- `name` → `name` (required)
- `date` → `date` (ISO format)
- `location` → `location`
- `event_type` → `type` (normalize: conference, podcast, workshop, talk)
- `description` or `notes` → `description`

**Validation:**
- Require non-empty `name`
- Require `public: true`
- Require valid date
- Exclude private meetings/events

### Domains → domains.json

**Source:** `$DATA_DIR/domains/domains.parquet`

**Note:** Domain registrations are already public information.

**Parquet Schema:**
- `domain_id` (string)
- `name` (string)
- `registrar` (string)
- `expiry_date` (date)
- `status` (string)

**Website JSON Structure:**
```json
[
  {
    "name": "example.com",
    "registrar": "DNSimple",
    "status": "active"
  }
]
```

**Field Mappings:**
- `name` → `name`
- `registrar` → `registrar`
- `status` → `status` (exclude if "expiring" or "expired")

**Exclusions:**
- `domain_id` - Internal ID
- `expiry_date` - Private
- `notes` - Internal
- Domains with status "expiring" or "expired"

**Validation:**
- Exclude expiring/expired domains
- Only include professional/relevant domains

---

## Transformation Pipeline

### Step 1: Extract

```python
# Extract public records from parquet
df = pd.read_parquet(DATA_DIR / data_type / f"{data_type}.parquet")
public_df = df[df["public"] == True]
```

### Step 2: Transform

```python
# Map fields to website format
website_records = []
for _, row in public_df.iterrows():
    record = {
        "id": row["project_id"],
        "name": row["name"],
        "description": row.get("description") or row.get("notes"),
        "status": normalize_status(row["status"]),
        "startDate": row["start_date"].isoformat() if row["start_date"] else None,
        # ... other fields
    }
    
    # Validate
    if validate_record(record):
        website_records.append(record)
```

### Step 3: Sanitize

```python
def sanitize_record(record):
    """Remove sensitive content."""
    # Remove internal IDs
    record = {k: v for k, v in record.items() if not k.endswith('_gid')}
    
    # Remove metadata fields
    metadata_fields = ['import_date', 'import_source_file', 'created_at', 'modified_at']
    record = {k: v for k, v in record.items() if k not in metadata_fields}
    
    # Sanitize text fields
    if 'description' in record and record['description']:
        record['description'] = remove_internal_references(record['description'])
    
    return record
```

### Step 4: Validate

```python
def validate_record(record):
    """Validate record before publishing."""
    # Required fields
    if not record.get('name') or not record.get('name').strip():
        return False
    
    # Check for sensitive keywords
    sensitive_keywords = ['password', 'api_key', 'secret', 'token']
    text_fields = [record.get('name', ''), record.get('description', '')]
    if any(kw in ' '.join(text_fields).lower() for kw in sensitive_keywords):
        return False
    
    return True
```

### Step 5: Generate JSON

```python
# Write to website data directory
output_file = WEBSITE_DATA_DIR / f"{data_type}.json"
with open(output_file, 'w', encoding='utf-8') as f:
    json.dump(website_records, f, indent=2, ensure_ascii=False)
```

---

## Normalization Rules

### Status Values

**Projects:**
- `planned`, `planning` → `planned`
- `active`, `in_progress` → `active`
- `completed`, `done`, `finished` → `completed`
- `archived`, `cancelled` → `archived`

**Goals:**
- `active`, `current` → `active`
- `completed`, `done`, `achieved` → `completed`
- `archived`, `abandoned` → `archived`

### Priority Values

- `high`, `p0`, `p1`, `critical` → `high`
- `medium`, `p2`, `normal` → `medium`
- `low`, `p3`, `p4` → `low`

### Event Types

- `conference`, `talk`, `presentation` → `conference`
- `podcast`, `interview` → `podcast`
- `workshop`, `training` → `workshop`
- `meetup`, `community` → `meetup`

### Date Formats

All dates must be ISO 8601 format:
- Full dates: `YYYY-MM-DD` (e.g., `2026-01-15`)
- Year-month: `YYYY-MM` (e.g., `2026-01`)
- Year only: `YYYY` (e.g., `2026`)

### Text Sanitization

Remove:
- Notion URLs: `https://www.notion.so/...`
- Asana URLs: `https://app.asana.com/...`
- Internal IDs in text: `asana-1234567890`
- Email addresses (unless explicitly public)
- Phone numbers
- Internal notes markers: `[INTERNAL]`, `PRIVATE:`, etc.

---

## Validation Checklist

Before generating website JSON files:

- [ ] **Privacy Check:** All records have `public: true` flag
- [ ] **Content Quality:** All records have non-empty required fields
- [ ] **No PII:** No personal identifiable information
- [ ] **No Sensitive Data:** No financial, health, or confidential information
- [ ] **No Internal References:** No Asana/Notion links, internal IDs
- [ ] **Professional Relevance:** Content is appropriate for professional website
- [ ] **Data Completeness:** Required fields are filled
- [ ] **Format Consistency:** Dates, statuses normalized
- [ ] **Manual Review:** Human review of all extracted content

---

## Example Transformation Script

```python
#!/usr/bin/env python3
"""
Transform public data to website JSON format.
"""

import json
from datetime import date
from pathlib import Path
import pandas as pd

DATA_DIR = Path("/path/to/data")
WEBSITE_DATA_DIR = Path("/path/to/website/src/data")

def transform_projects():
    """Transform projects to website format."""
    # Read parquet
    df = pd.read_parquet(DATA_DIR / "projects" / "projects.parquet")
    
    # Filter public projects
    public_df = df[df["public"] == True]
    
    # Transform records
    projects = []
    for _, row in public_df.iterrows():
        project = {
            "id": row["project_id"],
            "name": row["name"],
            "description": row.get("description") or row.get("notes", ""),
            "status": normalize_status(row.get("status")),
            "startDate": row["start_date"].isoformat() if pd.notna(row["start_date"]) else None,
            "endDate": row["end_date"].isoformat() if pd.notna(row["end_date"]) else None,
        }
        
        # Validate
        if validate_project(project):
            projects.append(sanitize_record(project))
    
    # Write JSON
    output_file = WEBSITE_DATA_DIR / "projects.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(projects, f, indent=2, ensure_ascii=False)
    
    print(f"Generated {len(projects)} projects")

def normalize_status(status):
    """Normalize status values."""
    if not status:
        return "unknown"
    status = status.lower()
    if status in ['planned', 'planning']:
        return 'planned'
    elif status in ['active', 'in_progress']:
        return 'active'
    elif status in ['completed', 'done']:
        return 'completed'
    else:
        return 'archived'

def validate_project(project):
    """Validate project record."""
    # Required fields
    if not project.get('name') or not project.get('name').strip():
        return False
    if not project.get('description'):
        return False
    # Check length
    if len(project['description']) < 10:
        return False
    return True

def sanitize_record(record):
    """Remove sensitive content."""
    # Remove null values
    return {k: v for k, v in record.items() if v is not None}

if __name__ == "__main__":
    transform_projects()
```

---

## Future Enhancements

### Automated Transformation

Once public data is available:
1. Create transformation script per data type
2. Add to website build process
3. Validate before deployment

### Content Enhancement

- Add metadata: last updated, source data type
- Add sorting/filtering fields
- Add thumbnail images (if applicable)
- Add tags/categories for organization

### Validation Improvements

- Automated sensitive keyword detection
- Link validation
- Format validation
- Schema validation against website expectations

---

## Related Documents

- Privacy Guidelines: `docs/data_publishing_privacy_guidelines.md`
- Analysis Report: `tmp/website_data_analysis/ANALYSIS_REPORT.md`
- Extraction Script: `execution/scripts/extract_public_data.py`
- Website Data Directory: `execution/website/markmhendrickson/react-app/src/data/`
