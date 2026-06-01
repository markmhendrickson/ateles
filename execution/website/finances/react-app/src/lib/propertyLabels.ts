/**
 * Human-readable labels for Neotoma snapshot / API field names.
 * Use {@link humanizePropertyKey} for display; keep the literal key in a `title` tooltip.
 */

/** Segments shown in ALL CAPS when they appear as a snake_case part */
const ACRONYM_SEGMENTS = new Set([
  'eur',
  'usd',
  'gbp',
  'chf',
  'id',
  'iban',
  'bic',
  'swift',
  'apr',
  'fx',
  'url',
  'uri',
  'api',
  'tin',
  'vat',
  'nft',
  'btc',
  'eth',
])

/**
 * Full-key overrides (literal API / snapshot key → display string).
 */
const OVERRIDES: Record<string, string> = {
  canonical_name: 'Display name',
  entity_id: 'Entity ID',
  entity_type: 'Entity type',
  filing_tags: 'Filing tags',
  strategy_bucket: 'Strategy bucket',
  tax_year_context: 'Tax year',
  balance_eur: 'Balance (EUR)',
  balance_usd: 'Balance (USD)',
  account_value: 'Account value',
  account_value_currency: 'Account value currency',
  outstanding_principal_eur: 'Outstanding principal (EUR)',
  assets_sheet_as_of_date: 'Assets sheet as of',
  registry_id: 'Registry ID',
  account_registry_id: 'Account registry ID',
  idempotency_key: 'Idempotency key',
  observation_kind: 'Observation kind',
  account_type: 'Account type',
  account_name: 'Account name',
  last_statement_date: 'Last statement date',
  filing_type: 'Filing type',
  form_code: 'Form code',
  form_name: 'Form name',
  tax_year: 'Tax year',
  due_date: 'Due date',
  filed_at: 'Filed at',
  confirmation_number: 'Confirmation number',
  filing_authority: 'Filing authority',
  modelo_bien: 'Modelo asset (bien)',
  modelo_bien_hint: 'Modelo asset hint',
  billing_frequency: 'Billing frequency',
  yearly_total_eur: 'Yearly total (EUR)',
  expense_type: 'Expense type',
  secured_property: 'Secured property',
  loan_type: 'Loan type',
  outstanding_principal: 'Outstanding principal',
  monthly_payment: 'Monthly payment',
  statement_as_of_date: 'Statement as of date',
  statement_period_start: 'Statement period start',
  statement_period_end: 'Statement period end',
  statement_source_kind: 'Statement source kind',
  statement_pdf_path: 'Statement PDF path',
  ending_account_value: 'Ending account value',
  ending_account_value_eur: 'Ending account value (EUR)',
  ending_account_value_usd: 'Ending account value (USD)',
  counterparty: 'Counterparty',
  amount_eur: 'Amount (EUR)',
}

function titleCaseSegment(segment: string): string {
  const lower = segment.toLowerCase()
  if (/^\d+$/.test(lower)) return segment
  if (ACRONYM_SEGMENTS.has(lower)) return lower.toUpperCase()
  if (lower.length <= 1) return lower.toUpperCase()
  return lower.charAt(0).toUpperCase() + lower.slice(1)
}

/**
 * Split API / schema tokens that may be snake_case, kebab-case, or camelCase / PascalCase
 * (e.g. `canonical_name`, `accountMaskLast4`, `HTTPResponse`).
 */
export function splitKeySegments(key: string): string[] {
  const s = key.trim()
  if (!s) return []
  const spaced = s
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
  return spaced.split(/[_\s-]+/).filter(Boolean)
}

/**
 * Maps a literal field key (e.g. `canonical_name`, `balance_eur`) to a short human-readable label.
 */
export function humanizePropertyKey(literalKey: string): string {
  if (!literalKey) return ''
  if (OVERRIDES[literalKey]) return OVERRIDES[literalKey]
  return splitKeySegments(literalKey).map(titleCaseSegment).join(' ')
}
