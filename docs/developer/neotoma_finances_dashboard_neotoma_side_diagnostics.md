# Neotoma-side diagnostics: finances dashboard

**Audience:** Agents debugging why `execution/website/finances/react-app` shows empty data, errors, or mismatches when talking to Neotoma.

**Consumer app:** Vite dev server proxies `GET/POST /neotoma-api/*` → Neotoma API base (`VITE_NEOTOMA_API_URL`, default `http://localhost:3180`), with optional `Authorization: Bearer <VITE_NEOTOMA_TOKEN>`.

**EUR↔USD (Frankfurter):** The app loads **ECB reference** USD-per-EUR from Frankfurter (`FxRateProvider`, `src/lib/frankfurterClient.ts`). **Per row:** conversions use the account’s valuation date (`entityFxDate` → `GET /{YYYY-MM-DD}?from=EUR&to=USD` via the same client) when that date resolves; until loaded or if missing, the **latest** rate applies. **Caching:** `src/lib/frankfurterRateCache.ts` — **historical** rates keyed by date in `localStorage` (`finances-frankfurter:h1:<YYYY-MM-DD>`) with in-memory + in-flight dedupe in the client; **latest** uses `finances-frankfurter:latest:v1` with a **1h** TTL so reloads rarely hit the network. React Query historical queries use `staleTime: Infinity` for the same dates. **Dev:** Vite proxies `/frankfurter-api/*` → `api.frankfurter.app`. **Production:** the browser calls `https://api.frankfurter.app` directly (CORS allowed); if blocked, set `VITE_FRANKFURTER_BASE` to your own origin (no trailing slash). On fetch failure the UI falls back to **1.08**.

**Table columns:** Data tables (Accounts, EntityTable pages) use a shadcn **DropdownMenu** (“Columns” button, `TableColumnToggle` + `components/ui/dropdown-menu.tsx`); visibility is stored as `localStorage` keys prefixed with `finances-table-cols:`. **Modelo 720** tables pass `columnEnsureVisibleKeysCsv="account_name,account_value"` so **Account** and **Value** (monetary column, key `account_value`) stay on and are not hideable; Accounts passes `account`. The menu lists **Account** / **account_name** first when present. Accounts defaults still hide **Registry ID**, **Assets**, and **Rows** until enabled.

**FX tooltip:** `MonetaryPair` / `MonetaryStack` wrap the **primary** amount (when not mask mode) with a Radix tooltip: valuation **date** (`getEntityFxAsOfDate` or Frankfurter latest `rateDate`), **1 EUR = X USD**, and a **multi-line equation** (`€… × rate` → USD, or `$… ÷ rate` → EUR when the display basis is USD-sourced). `TooltipProvider` wraps the app in `App.tsx`.

**Display unit (sidebar):** `DisplayUnitProvider` + `finances-display-unit` in `localStorage` — default **USD**. **Primary** comparable basis uses `comparableEurFromStorageForDisplay` in `useMonetaryDisplay`: **EUR-only** raw legs → stored EUR; **USD-only** → EUR implied at valuation-date `usdPerEur`; **both** legs → if `snapshot.currency` is **USD**, use USD→EUR; if **EUR**/**EURO**, use stored EUR; otherwise fall back to `getEntityCanonicalEur` (max-of-legs). Then format in the selected display unit. **Secondary** (parenthesized when `layout="inline"`) = Neotoma **stored** legs only via `getEntityRawStorageLegs` (sheet columns **merged** with top-level `balance_eur` / `balance_usd` / `balance`+`currency` when a row omits that currency, then name-heuristic EUR/USD fields if still empty); no synthetic “other unit of canonical.” **Net worth / strategy buckets** sum `getEntityMonetaryDisplayBasisEur` so totals match the displayed primary. **Year vs balance:** If `balance_eur` (or row EUR) equals an integer snapshot year field (`tax_year_context`, `filing_year`, etc.) on a **USD** (or USD-balance) account, the dashboard treats that EUR figure as mis-imported metadata and ignores it so rows do not all show the same €2,024-style amount. **`balance` + `currency: USD`:** `getEntityValueEur` does **not** treat `balance` as EUR; `getEntityValueUsd` uses `balance` as USD when `balance_usd` is absent so conversion uses one nominal leg. **Canonical account value (preferred):** On `financial_account` snapshots, set **`account_value`** (number) with **`account_value_currency`** (`EUR` \| `USD`; if omitted, **`currency`** on the same snapshot is used, default EUR). The dashboard reads this **before** `balance_eur` / `balance_usd` for that leg so imports have one stable field for Modelo 720 / Accounts / totals; legacy `balance_*` still fill a leg when the canonical field did not set it. **Migration compatibility:** Until all statement parsers emit the canonical pair, the dashboard also accepts structured legacy statement-ending fields such as **`ending_account_value`** + `ending_account_value_currency` and **`ending_account_value_usd`** / **`ending_account_value_eur`**.

**No invented balances:** `hasExplicitBalanceRelevantData` gates `getEntityValueEur` / `getEntityValueUsd` (and `rawLegsFromTopLevelFallback`) so name-pattern sums (`topLevelCurrencyNumbers`, `fallbackTopLevelMoneyNumber`) run only after row money columns or structured snapshot fields (`account_value`, legacy `ending_account_value*`, `balance_*`, `balance`+`currency`, principal/amount/yearly, `market_value_*`, `value_*`) indicate a real amount — equity rows with only metadata show **—**. For **portfolio totals**, secondary is the aggregate in the non-selected currency (`useAggregateMonetaryLabels`). **Data hygiene:** USD-denominated accounts should set `currency: "USD"` when both EUR and USD numbers exist in the snapshot so the primary does not take `max()` of inconsistent legs.

**Account detail header:** Uses `MonetaryPair` with `getEntityCanonicalEur` + `getEntityRawStorageLegs` — do **not** pair `getEntityValueEur` and `getEntityValueUsd` as if they were FX equivalents; those legs are often independent snapshot fields and can look like a bad “conversion.”

---

## 0. Dashboard-facing shape contracts

These are the canonical fields the current app expects by route / entity type. Raw parser fields may still exist, but new ingestion flows should satisfy these first-class fields.

| Route / page | Primary entity type(s) | Canonical fields the UI should be able to rely on |
|--------------|------------------------|---------------------------------------------------|
| Overview / Accounts / Modelo 720 / Modelo 721 | `financial_account` | `canonical_name`, `registry_id`, `institution`, `jurisdiction`, `currency`, `strategy_bucket`, `filing_tags`, `account_value`, `account_value_currency`, `last_statement_date` |
| Filings shell | `tax_filing` + filtered `financial_account` views | `canonical_name` or `title`, `filing_type`, `form_code`, `jurisdiction`, `tax_year`, `status`, `due_date`, `filed_at`, plus relationships to included accounts and supporting statement/doc entities |
| Account detail | `financial_account` (+ optional related `account_statement`) | Account identity fields above; statement evidence should come from related `account_statement` entities when present, with legacy statement fields treated as migration-only fallback |
| Loans | `loan` | `canonical_name`, `lender`, `currency`, `outstanding_principal_eur` / `outstanding_principal_usd`, `payment_amount` or `monthly_payment`, `payment_cadence`, `maturity_date`, `apr_pct` / `apr` |
| Recurring expenses | `recurring_expense` | `canonical_name`, `merchant` or `provider`, `expense_label`, `billing_frequency`, `amount_eur` / `amount_usd`, `yearly_total_eur`, `payment_method`, `payer_registry_id` |
| Transactions | `transaction` | `canonical_name`, `date` or `posting_date`, `amount_eur` / `amount`, `currency`, `counterparty` or `description`, `provider`, `transaction_source`, `registry_id` |
| Timeline | mixed timeline rows | `entity_id`, `entity_type`, `event_type`, `timestamp`, `data` or `source_field` |
| Entity explorer | any schema / stats type | `canonical_name`, stable `snapshot`, `updated_at` or `last_observation_at` |

**Target modeling rule:** `financial_account` is the durable anchor. Dated statement / filing evidence should live on related **`account_statement`** entities, with only denormalized current-value fields copied onto the account.

---

## 1. Map symptoms to Neotoma surfaces

| Symptom (dashboard) | Neotoma area to verify |
|---------------------|-------------------------|
| **Timeline page empty** (no error) | `GET /timeline` — timeline **index** may be empty even when entities/observations exist |
| **Account detail crashes or chart empty** | `GET /entities/:id/observations` — observation `data` may be missing or shaped differently |
| **Observations list incomplete** | Pagination: `limit` / `offset` on observations route; `total` in body |
| **401 / 403 / network errors** | Token, CORS (prod), proxy target URL |
| **Silent empty lists** | Response JSON shape not matching what the app parses (see §3–4) |

**Important:** Timeline events ≠ all observations. The dashboard Timeline view only uses **`GET /timeline`**. Per-entity history uses **`GET /entities/:id/observations`**. **Account detail** and **Entity explorer** (selected entity) also list **`GET /timeline`** rows filtered to that `entity_id` (client-side after fetch; optional `entity_id` query param is sent when supported), as the **last** section on those pages.

---

## 2. Baseline HTTP checks (run against Neotoma host)

Replace `BASE` with the API origin (no `/neotoma-api` prefix). Add `-H "Authorization: Bearer $TOKEN"` if the deployment requires it.

```bash
# Health / reachability (adjust path if your deployment uses a different health route)
curl -sS "$BASE/health" || curl -sS "$BASE/" | head -c 200

# Timeline (finances app expects events array or legacy shapes — see §3)
curl -sS "$BASE/timeline?limit=10"
# Optional: entity-scoped (sent by dashboard; server may ignore unknown params — app filters client-side)
curl -sS "$BASE/timeline?limit=50&entity_id=ent_REPLACE_ME"

# Entity exists
curl -sS "$BASE/entities/ent_REPLACE_ME"

# Observations (pagination)
curl -sS "$BASE/entities/ent_REPLACE_ME/observations?limit=50&offset=0"

# Entity query (POST — same as Accounts overview)
curl -sS -X POST "$BASE/entities/query" \
  -H "Content-Type: application/json" \
  -d '{"entity_type":"financial_account","include_snapshots":true,"limit":5}'
```

**Interpretation:**

- **`/timeline` returns `{ "events": [], "total": 0, ... }`** → Neotoma timeline index is empty; expected dashboard behavior is an empty Timeline page (not a bug in the app).
- **`/entities/:id/observations` returns items without `data`** → Balance charts must skip those rows; confirm whether API should always include `data` for financial observations.

---

## 3. `GET /timeline` contract (consumer expectations)

**Dashboard code:** `execution/website/finances/react-app/src/api/timeline.ts`

The client accepts, in order:

1. JSON **array** of timeline event objects  
2. `{ "events": [ ... ] }` ← **observed** Neotoma shape  
3. `{ "data": [ ... ] }`  

Anything else → client returns **`[]`** (looks like “no events” with no error).

**Query params sent by the app:** `start_date`, `end_date`, `limit`, `offset` (ISO date strings for dates when user sets filters).

**Agent checklist:**

- [ ] Confirm `GET /timeline` returns HTTP 200 and a body the client recognizes (§3).  
- [ ] If events exist in DB but API returns empty, trace **timeline materialization** (what writes timeline rows / `timeline_events` / equivalent).  
- [ ] Align MCP **`list_timeline_events`** with HTTP `/timeline` semantics so CLI/MCP and dashboard see the same universe of events.

---

## 4. `GET /entities/:id/observations` contract

**Dashboard code:** `execution/website/finances/react-app/src/api/entities.ts` → `normalizeObservationsPayload()`

Accepted shapes:

- JSON **array** of observations  
- `{ "data": [ ... ] }` or `{ "observations": [ ... ] }`  
- Optional **`total`** (number) for pagination stop conditions  

**Pagination:** App pages with `limit=2000` until empty or `merged.length >= total`.

**Agent checklist:**

- [ ] Confirm **`limit` / `offset`** are honored; if ignored, dashboard may show only the first page or over-fetch duplicates depending on server behavior.  
- [ ] Confirm each observation includes **`data`** when the entity type expects payload (e.g. balances); missing `data` is valid for some kinds but breaks **balance charts** that read `data.balance_eur`.
- [ ] If list responses omit `data` but expose the body as **`payload`**, **`snapshot`**, **`fields`**, or **`body`**, the dashboard maps those into `data` client-side (`normalizeObservationRow` in `src/api/entities.ts`). Prefer a single canonical `data` key in API responses.  
- [ ] Confirm **`source`**, **`idempotency_key`**, **`observed_at` / `created_at`** exist when the UI should show provenance.
- [ ] **`source` is optional** in many write paths. If it is `null`, omitted, or blank, the finances app groups those observations under **Unspecified** in the observation-sources table (not a rendering bug).

---

## 5. Related dashboard endpoints (quick reference)

| Feature | Method + path |
|--------|----------------|
| Accounts list / filters | `POST /entities/query` |
| Account detail header | `GET /entities/:id` |
| Accounts **As of** column | First set snapshot field among `last_statement_date`, `statement_as_of_date`, `statement_period_end`, `assets_sheet_as_of_date`, `as_of_date` (`getEntityFxAsOfDate` in `src/lib/entityFxDate.ts`). **—** if none are set. |
| Observation history / chart | `GET /entities/:id/observations` |
| Relationships panel | `GET /entities/:id/relationships` |
| Timeline page | `GET /timeline` |
| Timeline **type labels** | `entity_type` / `event_type` may be **camelCase** (e.g. `accountMaskLast4`). The app splits camelCase for display; a single blob like `Accountmasklast4` was only capitalizing the first letter before that fix. |
| Timeline **thin rows (no `data`)** | The UI **hydrates client-side**: `GET /entities/{entity_id}` and reads **`source_field`** (or `extracted_from_field` / `sourceField`, normalized in `timeline.ts`) from the **current** snapshot via dotted paths (`snapshotPathValue`). If `source_field` is absent, it tries **PascalCase `event_type` → snake_case** as a snapshot key hint. Prefer API including `source_field` when `data` is null. Historical values are not recoverable from the pointer alone. |
| Stats tiles | `GET` stats route as implemented in `src/api/stats.ts` |

Agents should open `execution/website/finances/react-app/src/api/*.ts` for exact paths and query params.

---

## 6. Environment alignment

| Variable | Role |
|----------|------|
| `VITE_NEOTOMA_API_URL` | Neotoma API origin for Vite proxy target |
| `VITE_NEOTOMA_TOKEN` | Optional bearer token injected on proxied requests |

**Mismatch symptom:** Browser shows empty or 401 while `curl` to localhost works — usually wrong token, wrong port, or prod vs dev API.

---

## 7. Suggested Neotoma repo follow-ups (when root cause is server-side)

1. **Document** the canonical JSON schema for `/timeline` and `/entities/:id/observations` (including pagination and `total`).  
2. **Guarantee** `data` presence (or explicit null) for observation kinds that downstream UIs depend on.  
3. **Backfill or generate** timeline events if product expectation is “Timeline shows all financial activity” (may require a new indexer or widening timeline sources).  
4. **Tests:** Contract tests or OpenAPI examples that match the shapes in §3–4.

---

## 8. Related internal docs

- `docs/private/finances/neotoma_data_collection_pipeline.md` — how financial data enters Neotoma  
- `docs/private/finances/neotoma_financial_entity_types_master_sheet.md` — entity types  
- `docs/workflows.md` — MCP `list_timeline_events` example (conversation-scoped)
