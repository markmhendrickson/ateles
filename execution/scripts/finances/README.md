# Finance import scripts

## Source material — **always copy here (local disk, not git)**

Downloads (Desktop, Downloads, email attachments) must land in a **stable imports tree** so checklists, Neotoma `file_asset` paths, and gestor handoffs stay consistent.

| Item | Value |
|------|--------|
| **Root** | `$HOME/Documents/data/imports` (or **`FINANCE_SOURCE_IMPORT_ROOT`**) |
| **Layout** | `source_material/<tax_year>/<kind>/` — see script help for **`kind`** |

```bash
./execution/scripts/finances/copy_finance_source_material.sh --tax-year 2025 --kind modelo_720 "/path/to/statement.pdf"
./execution/scripts/finances/copy_finance_source_material.sh --tax-year 2025 --kind us_tax "/path/to/1099-composite.pdf"
```

**Rule:** Cursor agents load [`.cursor/rules/finance_source_material.mdc`](../../../.cursor/rules/finance_source_material.mdc) when handling finance PDFs/exports — **run this copy step in the same turn** after the user provides a path, then reference the **destination** path in docs (never commit the files).

## `export_finance_sheet_gws.sh`

Pulls the **Finances** spreadsheet (Google Sheets workbook title in Drive) to dated CSVs using **[`gws`](https://github.com/googleworkspace/cli)** (Step **1b** in [`neotoma_data_collection_pipeline.md`](../../../docs/private/finances/neotoma_data_collection_pipeline.md)).

### Setup

1. Install `gws` and run `gws auth login` (scopes including Sheets). See [`main_financial_accounts_registry.md`](../../../docs/private/finances/main_financial_accounts_registry.md) §Recommended data sources.
2. Open the **Finances** workbook in the browser. From the URL `https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`, copy `<SPREADSHEET_ID>` (the ID is **not** the word "Finances").
3. Add to repo **`.env`** at the monorepo root (not committed):

```bash
FINANCE_GOOGLE_SHEET_ID="<your_spreadsheet_id>"
# Optional: comma-separated **tab** names inside Finances. If omitted, default is Assets + Savings accounts only.
# FINANCE_SHEET_TABS="Assets,Savings accounts,Loans"
# Optional: export every tab in the workbook (can be slow).
# FINANCE_EXPORT_ALL_TABS=1
# Optional: override output directory
# FINANCE_GWS_OUTPUT_DIR="${HOME}/Documents/data/imports/google sheets finances"
```

### Run

From the **ateles** repo root:

```bash
./execution/scripts/finances/export_finance_sheet_gws.sh
```

`jq` is required only when using **`FINANCE_EXPORT_ALL_TABS=1`**.

Outputs files like `2026-03-11_Assets-Table 1.csv` under the imports folder. Use **`RUN_DATE` in the filename** as **`as_of_date`** for Neotoma **`import_artifact`** / observations (**§7.4**).

**Note:** `gws` writes **Sheets API JSON** (`values` array) into files named `*.csv`. Ingest expects that JSON shape (see Step 1c below), not RFC-4180 CSV.

### Tab names

Defaults target tabs named **`Assets`** and **`Savings accounts`** inside the **Finances** workbook. If your tab titles differ (e.g. the positions tab is not called `Assets`), set **`FINANCE_SHEET_TABS`** to the exact names shown on the tab bar.

## `build_assets_observation_entities.py`

Builds a **`neotoma store`** JSON array for Step **1c**: **`Type == Crypto`** rows on **Assets**, grouped by **Description 2** → **`registry_id`** (see **`main_financial_accounts_registry.md`** §2), resolved to existing **`financial_account`** entities via **`registry_id` → `canonical_name`** from a Neotoma entity dump.

- **Unmapped** crypto rows → a **`note`** (capped at 500 rows in payload).
- Optional third argument: **Savings accounts** export → one **`note`** with embedded CSV text.

## `link_crypto_wallet_addresses.py`

Creates **`crypto_wallet_address`** entities from **`Assets-Table 1.csv`** (on-chain `Address`, or **`koinly_wallet:`** from **Koinly URL**) and links each to **`financial_account`** with **`PART_OF`** (source = address, target = account). Resolves parents via the same **Description 2 → `custody_*`** map as the assets builder, then **`modelo_workbook_{year}_*`** slugs from **Description** for each **`--try-year`**. Any remaining crypto account (721 / custody / crypto denomination) gets a **`reporting_placeholder`** address so the graph always has an address child.

**Canonical names** are ASCII-normalized before store (unicode punctuation in sheet labels can otherwise trigger observation id collisions on some Neotoma builds). Stores **one entity per** `neotoma store` call.

```bash
python3 execution/scripts/finances/link_crypto_wallet_addresses.py \
  --csv "${HOME}/Documents/data/imports/google sheets finances/Assets-Table 1.csv" \
  --base-url http://localhost:3180 \
  --dry-run

python3 execution/scripts/finances/link_crypto_wallet_addresses.py \
  --csv "${HOME}/Documents/data/imports/google sheets finances/Assets-Table 1.csv" \
  --base-url http://localhost:3180 \
  --execute
```

## `ingest_finance_sheet_observations.sh`

End-to-end Step **1c**: dumps `financial_account` entities via CLI, runs the Python builder, then **`neotoma store`** with idempotency key `finances-assets-gws-<as_of>-<sha256_prefix>`.

```bash
./execution/scripts/finances/ingest_finance_sheet_observations.sh \
  "${HOME}/Documents/data/imports/google sheets finances/2026-03-23_Assets-Table 1.csv" \
  "${HOME}/Documents/data/imports/google sheets finances/2026-03-23_Savings accounts-Table 1.csv"
```

**`import_artifact`:** not created by this path yet; observations carry **`assets_sheet_import_sha256`** (16-char prefix in builder; full hash in shell key material). Add artifact linking in a follow-up if you want file-entity provenance.

## `build_account_statement_store_payload.py`

Builds a structured payload for one dated statement snapshot using the new split:

- **`financial_account`** keeps durable identity plus denormalized current fields:
  - `account_value`
  - `account_value_currency`
  - `last_statement_date`
- **`account_statement`** keeps the dated statement evidence:
  - `statement_as_of_date`
  - `statement_period_start` / `statement_period_end`
  - `statement_source_kind`
  - `statement_pdf_path`
  - raw parser-specific fields such as `ending_account_value_usd`

Usage:

```bash
python3 execution/scripts/finances/build_account_statement_store_payload.py /tmp/statement.json
```

Input JSON:

```json
{
  "account": {
    "registry_id": "fidelity_lyft_shares",
    "canonical_name": "Fidelity Lyft shares",
    "institution": "Fidelity",
    "jurisdiction": "USA",
    "currency": "USD"
  },
  "statement": {
    "title": "Fidelity LYFT investment report 2025-10-01 to 2025-12-31",
    "statement_as_of_date": "2025-12-31",
    "statement_period_start": "2025-10-01",
    "statement_period_end": "2025-12-31",
    "statement_source_kind": "fidelity_investment_report_pdf",
    "statement_pdf_path": "/abs/path/to/file.pdf",
    "ending_account_value_usd": 1011.09
  }
}
```

Output JSON is shaped for MCP `store_structured`:

```json
{
  "entities": [
    { "entity_type": "financial_account", "...": "..." },
    { "entity_type": "account_statement", "...": "..." }
  ],
  "relationships": [
    { "relationship_type": "REFERS_TO", "source_index": 1, "target_index": 0 }
  ]
}
```

This same builder is also the recommended template for legacy backfill: extract statement-only fields off older `financial_account` snapshots into new `account_statement` entities, then keep only the canonical convenience fields on the account.

## `reload_account_statements_with_metadata_and_pdfs.sh`

Safe provenance reload for `account_statement` rows that also preserves statement metadata needed by the UI (`statement_as_of_date`, period dates, source kind, and optional `account_registry_id`).

- Uses PDF attachments from local source material paths.
- Tries to recover metadata from existing observation history first.
- Applies per-statement overrides from:
  - `execution/scripts/finances/account_statement_reload_metadata.json`
  - or `STATEMENT_METADATA_JSON=/abs/path/file.json`
- Skips rows with no date fields unless `ALLOW_MINIMAL=1`.

Usage:

```bash
./execution/scripts/finances/reload_account_statements_with_metadata_and_pdfs.sh
```

## Modelo export parity (UI + script)

For filing prep, keep Neotoma as the source of truth and generate workbook copies from that data:

- UI: filing detail page "Download Excel" button.
- Script parity: from `execution/website/finances/react-app` run:

```bash
npm run export:modelo -- --tax-year 2025 --out "${HOME}/Documents/data/imports/source_material/2025/taxes/Modelos_720_721_2025.xlsx"
```

The script uses the same workbook builder as the UI export and enforces a Neotoma coverage gate first:

- missing `registry_id`
- missing `modelo_bien` / `modelo_bien_hint`
- active account with no non-zero balance legs

If you intentionally want a draft copy while filling gaps, pass `--allow-incomplete`.

### Coverage report before export

Use this to see what is still missing in Neotoma (including `custody_*` rows from the checklist):

```bash
cd execution/website/finances/react-app
npm run report:modelo-coverage -- --tax-year 2025
```

JSON output is available with `--json`.
