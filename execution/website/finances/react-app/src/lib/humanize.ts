import { coalesceSnapshot, normalizeFilingTags } from './formatters'

const RELATIONSHIP_LABELS: Record<string, string> = {
  REFERS_TO: 'References',
  PART_OF: 'Part of',
  EMBEDS: 'Contains',
  CORRECTS: 'Corrects',
  SETTLES: 'Settles',
  DUPLICATE_OF: 'Duplicate of',
  DEPENDS_ON: 'Depends on',
  SUPERSEDES: 'Supersedes',
  works_at: 'Works at',
  owns: 'Owns',
  manages: 'Manages',
  related_to: 'Related to',
  references: 'References',
  transacted_with: 'Transacted with',
  invested_in: 'Invested in',
}

const ENTITY_TYPE_LABELS: Record<string, string> = {
  financial_account: 'Account',
  crypto_wallet_address: 'Wallet address',
  tax_filing: 'Tax filing',
  account_statement: 'Statement',
  recurring_expense: 'Recurring expense',
  loan: 'Loan',
  transaction: 'Transaction',
  import_artifact: 'Import',
  income: 'Income',
  goods: 'Goods',
  note: 'Note',
  contact: 'Contact',
  person: 'Person',
  company: 'Company',
  event: 'Event',
  task: 'Task',
  file_asset: 'File',
  conversation: 'Conversation',
  agent_message: 'Message',
}

/** Normalized keys (snake_case) → display label for workflow-style status fields */
const WORKFLOW_STATUS_LABELS: Record<string, string> = {
  in_progress: 'In progress',
  not_started: 'Not started',
  not_started_yet: 'Not started',
  pending: 'Pending',
  draft: 'Draft',
  active: 'Active',
  open: 'Open',
  closed: 'Closed',
  complete: 'Complete',
  completed: 'Completed',
  filed: 'Filed',
  submitted: 'Submitted',
  approved: 'Approved',
  rejected: 'Rejected',
  cancelled: 'Cancelled',
  canceled: 'Canceled',
  failed: 'Failed',
  archived: 'Archived',
  unknown: 'Unknown',
}

function normalizeWorkflowStatusKey(raw: string): string {
  return raw
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, '_')
    .replace(/_+/g, '_')
}

function titleCaseFromSnake(key: string): string {
  return key
    .split('_')
    .filter(Boolean)
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
}

/** Common ISO 4217 codes we keep uppercase when splitting snake_case labels. */
const ISO_4217_COMMON = new Set([
  'EUR',
  'USD',
  'GBP',
  'CHF',
  'JPY',
  'CNY',
  'AUD',
  'CAD',
  'NZD',
  'SEK',
  'NOK',
  'DKK',
  'PLN',
  'MXN',
  'BRL',
  'INR',
  'KRW',
  'SGD',
  'HKD',
  'TWD',
  'ZAR',
  'TRY',
  'ILS',
  'AED',
  'SAR',
  'CZK',
  'HUF',
  'RON',
  'BGN',
  'HRK',
  'ISK',
  'THB',
  'MYR',
  'IDR',
  'PHP',
  'VND',
])

/** Splits "Institution — account_slug" and similar compound titles. */
const ACCOUNT_LABEL_COMPOUND_SEP = /\s*[—–]\s*|\s+-\s+/

function normalizeAccountLabelInput(raw: string): string {
  return raw
    .replace(/\s+/g, ' ')
    .replace(/^(?:[\s—–-]+)+/, '')
    .replace(/(?:[\s—–-]+)+$/, '')
    .trim()
}

function normalizeMissingAccountToken(raw: string | null | undefined): string | undefined {
  if (raw == null) return undefined
  const t = normalizeAccountLabelInput(String(raw))
  if (!t) return undefined
  if (/^[—–-]+$/.test(t)) return undefined
  return t
}

function splitCompoundAccountLabel(raw: string | null | undefined): string[] {
  const token = normalizeMissingAccountToken(raw)
  if (!token) return []
  return token
    .split(ACCOUNT_LABEL_COMPOUND_SEP)
    .map(part => normalizeMissingAccountToken(part))
    .filter((part): part is string => Boolean(part))
}

const normalizeFilingTagList = normalizeFilingTags

function inferInstitutionFromSnapshot(snapshot: Record<string, unknown> | null | undefined): string | undefined {
  if (!snapshot) return undefined
  const accountType = String(coalesceSnapshot<string>(snapshot, ['account_type']) ?? '').toLowerCase()
  const modeloTipo = String(coalesceSnapshot<string>(snapshot, ['modelo_tipo']) ?? '').toLowerCase()
  const modeloBien = String(coalesceSnapshot<string>(snapshot, ['modelo_bien', 'modelo_bien_hint']) ?? '').toLowerCase()
  const ccy = String(coalesceSnapshot<string>(snapshot, ['currency', 'account_value_currency']) ?? '').toUpperCase()
  const filingModel = String(coalesceSnapshot<string>(snapshot, ['filing_model']) ?? '').trim()
  const filingTags = normalizeFilingTagList(snapshot)

  if (filingTags.includes('721') || accountType.includes('custod') || modeloBien.includes('criptomoneda')) {
    return 'Crypto wallet'
  }
  if (filingTags.includes('equity') || modeloBien.includes('acciones') || modeloBien.includes('private equity')) {
    return 'Private equity'
  }
  const is720Scope =
    filingTags.includes('720') || filingModel === '720'
  if (
    is720Scope &&
    ccy === 'USD' &&
    (modeloBien.includes('corretaje') || modeloBien.includes('securities') || modeloTipo.includes('corretaje'))
  ) {
    return 'Brokerage account'
  }
  return undefined
}

function haystackForInstitutionGuess(entity: {
  canonical_name?: string | null
  snapshot?: Record<string, unknown> | null
}): string {
  const snap = entity.snapshot
  const parts = [
    entity.canonical_name,
    coalesceSnapshot<string>(snap, ['canonical_name', 'account_name', 'registry_id']),
  ].filter((p): p is string => Boolean(p && String(p).trim()))
  return String(parts.join(' '))
    .toLowerCase()
    .replace(/_/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

/** When `institution` is unset, infer a display institution from names the user already encodes elsewhere. */
function inferInstitutionFromEmbeddedNames(entity: {
  canonical_name?: string | null
  snapshot?: Record<string, unknown> | null
}): string | undefined {
  const hay = haystackForInstitutionGuess(entity)
  if (!hay) return undefined
  if (hay.includes('schwab')) return 'Charles Schwab'
  if (hay.includes('fidelity')) return 'Fidelity'
  if (hay.includes('american express') || /\bamex\b/.test(hay)) return 'American Express'
  if (hay.includes('capital one')) return 'Capital One'
  if (hay.includes('coinbase')) return 'Coinbase'
  if (hay.includes('kraken')) return 'Kraken'
  if (hay.includes('wise')) return 'Wise'
  return undefined
}

function humanizeAccountWordToken(w: string): string {
  if (!w) return w
  if (w.length === 3 && /^[A-Za-z]{3}$/.test(w)) {
    const u = w.toUpperCase()
    if (ISO_4217_COMMON.has(u)) return u
  }
  if (w.length >= 2 && w.length <= 5 && /^[A-Z0-9]+$/.test(w) && w === w.toUpperCase()) {
    return w
  }
  return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()
}

function humanizeAccountSlugSegment(segment: string): string {
  const t = segment.trim()
  if (!t) return t
  if (!t.includes('_') && t.includes(' ')) {
    return t.split(/\s+/).map(humanizeAccountWordToken).join(' ')
  }
  if (!t.includes('_') && /^[A-Z][a-z]/.test(t)) {
    return t
  }
  if (!t.includes('_') && /^[A-Z0-9][A-Z0-9 ,.&'’-]*$/i.test(t) && !/^[a-z]/.test(t)) {
    return t
  }
  const parts = t.replace(/-/g, '_').split('_').filter(Boolean)
  if (parts.length === 0) return t
  return parts.map(humanizeAccountWordToken).join(' ')
}

/**
 * Pretty-print stored account labels (snake_case slugs, compound titles) for UI and exports.
 * Leaves already-readable phrases (title case, institution names with spaces) unchanged.
 */
export function humanizeAccountLabel(raw: string | null | undefined): string {
  const s = normalizeMissingAccountToken(raw)
  if (!s) return '—'
  if (ACCOUNT_LABEL_COMPOUND_SEP.test(s)) {
    return s
      .split(ACCOUNT_LABEL_COMPOUND_SEP)
      .map(part => humanizeAccountSlugSegment(part.trim()))
      .filter(Boolean)
      .join(' — ')
  }
  return humanizeAccountSlugSegment(s)
}

/** Tailwind classes for Badge (outline variant + border-0 applied in component) */
const WORKFLOW_STATUS_BADGE_CLASSES: Record<string, string> = {
  in_progress: 'bg-amber-100 text-amber-900 dark:bg-amber-900/35 dark:text-amber-200',
  pending: 'bg-amber-100 text-amber-900 dark:bg-amber-900/35 dark:text-amber-200',
  submitted: 'bg-sky-100 text-sky-900 dark:bg-sky-900/35 dark:text-sky-200',
  draft: 'bg-muted text-muted-foreground',
  not_started: 'bg-muted text-muted-foreground',
  not_started_yet: 'bg-muted text-muted-foreground',
  active: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-900/35 dark:text-emerald-200',
  open: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-900/35 dark:text-emerald-200',
  complete: 'bg-green-100 text-green-900 dark:bg-green-900/35 dark:text-green-200',
  completed: 'bg-green-100 text-green-900 dark:bg-green-900/35 dark:text-green-200',
  filed: 'bg-green-100 text-green-900 dark:bg-green-900/35 dark:text-green-200',
  approved: 'bg-green-100 text-green-900 dark:bg-green-900/35 dark:text-green-200',
  closed: 'bg-zinc-200 text-zinc-800 dark:bg-zinc-800/50 dark:text-zinc-200',
  rejected: 'bg-red-100 text-red-900 dark:bg-red-900/35 dark:text-red-200',
  cancelled: 'bg-red-100 text-red-900 dark:bg-red-900/35 dark:text-red-200',
  canceled: 'bg-red-100 text-red-900 dark:bg-red-900/35 dark:text-red-200',
  failed: 'bg-red-100 text-red-900 dark:bg-red-900/35 dark:text-red-200',
  archived: 'bg-muted text-muted-foreground',
  unknown: 'bg-muted text-muted-foreground',
}

const SOURCE_LABELS: Record<string, string> = {
  neotoma_store: 'Imported',
  imported_payload: 'Imported payload',
  store: 'Imported',
  mcp: 'Imported via integration',
  mcp_store: 'Imported via integration',
  store_unstructured: 'Imported file',
  cli: 'Imported via CLI',
  correction: 'Manual correction',
  user: 'User entry',
  sheet_import: 'Google Sheets import',
  assets_sheet_rows: 'Assets sheet import',
  savings_accounts_csv: 'Savings accounts import',
  assets_sheet_unmapped_crypto: 'Unmapped crypto rows',
}

export function humanizeRelationshipType(type: string): string {
  return RELATIONSHIP_LABELS[type] ?? type.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

export function entityTypeLabel(type: string): string {
  return ENTITY_TYPE_LABELS[type] ?? type.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

export function isWorkflowStatusSnapshotKey(key: string): boolean {
  return key === 'status' || key.endsWith('_status')
}

export function humanizeWorkflowStatus(raw: string | null | undefined): string {
  if (raw == null || String(raw).trim() === '') return '—'
  const key = normalizeWorkflowStatusKey(String(raw))
  return WORKFLOW_STATUS_LABELS[key] ?? titleCaseFromSnake(key)
}

export function workflowStatusBadgeClassName(raw: string | null | undefined): string {
  if (raw == null || String(raw).trim() === '') return 'bg-muted text-muted-foreground'
  const key = normalizeWorkflowStatusKey(String(raw))
  return WORKFLOW_STATUS_BADGE_CLASSES[key] ?? 'bg-muted text-muted-foreground'
}

export function humanizeSource(source: string | undefined | null): string {
  if (!source) return 'Unspecified'
  const exact = SOURCE_LABELS[source]
  if (exact) return exact

  if (source.endsWith('.csv') || source.endsWith('.json') || source.endsWith('.pdf') || source.endsWith('.xlsx')) {
    return source
  }

  return source.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

/** Snapshot + entity fields used when picking a primary account label (same order as filing tables). */
export function rawFinancialAccountDisplayLabel(entity: {
  canonical_name?: string | null
  snapshot?: Record<string, unknown> | null
}): string | undefined {
  const fromSnap = coalesceSnapshot<string>(entity.snapshot, [
    'account_name',
    'display_name_en',
    'display_name_es',
    'canonical_name',
  ])
  if (fromSnap?.trim()) return fromSnap.trim()
  if (entity.canonical_name?.trim()) return entity.canonical_name.trim()
  return undefined
}

export function deriveFinancialAccountInstitution(entity: {
  canonical_name?: string | null
  snapshot?: Record<string, unknown> | null
}): string | undefined {
  const instName = coalesceSnapshot<string>(entity.snapshot, ['institution_name'])
  if (instName?.trim()) return instName.trim()

  const explicit = normalizeMissingAccountToken(coalesceSnapshot<string>(entity.snapshot, ['institution']))
  if (explicit) {
    const parts = splitCompoundAccountLabel(explicit)
    return humanizeAccountLabel(parts[0] ?? explicit)
  }

  const fallbackSources: Array<string | null | undefined> = [
    coalesceSnapshot<string>(entity.snapshot, ['canonical_name']),
    entity.canonical_name,
    coalesceSnapshot<string>(entity.snapshot, ['account_name']),
  ]
  for (const src of fallbackSources) {
    const parts = splitCompoundAccountLabel(src)
    if (parts.length >= 2) return humanizeAccountLabel(parts[0])
  }

  const inferred = inferInstitutionFromSnapshot(entity.snapshot)
  if (inferred) return inferred
  const fromNames = inferInstitutionFromEmbeddedNames(entity)
  if (fromNames) return fromNames
  return undefined
}

export function deriveFinancialAccountName(entity: {
  canonical_name?: string | null
  snapshot?: Record<string, unknown> | null
}): string | undefined {
  const primary = normalizeMissingAccountToken(
    coalesceSnapshot<string>(entity.snapshot, ['account_name', 'display_name_en', 'display_name_es']),
  )
  if (primary) {
    const parts = splitCompoundAccountLabel(primary)
    if (parts.length >= 2) return humanizeAccountLabel(parts.slice(1).join(' — '))
    return humanizeAccountLabel(primary)
  }

  const fallbackSources: Array<string | null | undefined> = [
    coalesceSnapshot<string>(entity.snapshot, ['canonical_name']),
    entity.canonical_name,
  ]
  for (const src of fallbackSources) {
    const parts = splitCompoundAccountLabel(src)
    if (parts.length >= 2) return humanizeAccountLabel(parts.slice(1).join(' — '))
    if (parts.length === 1) return humanizeAccountLabel(parts[0])
  }
  return undefined
}

export function entityDisplayName(entity: {
  canonical_name?: string | null
  entity_id: string
  entity_type: string
  snapshot?: Record<string, unknown> | null
}): string {
  if (entity.entity_type === 'financial_account') {
    const primary = rawFinancialAccountDisplayLabel(entity)
    if (primary) return humanizeAccountLabel(primary)
  } else if (entity.canonical_name?.trim()) {
    return entity.canonical_name.trim()
  }

  const snap = entity.snapshot
  if (snap) {
    const title = snap.title ?? snap.name ?? snap.canonical_name
    if (typeof title === 'string' && title.trim()) {
      const t = title.trim()
      if (entity.entity_type === 'financial_account') return humanizeAccountLabel(t)
      return t
    }
  }
  return `${entityTypeLabel(entity.entity_type)} ${entity.entity_id.slice(-8)}`
}
