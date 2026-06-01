import type { Entity, SheetRow } from '@/types/neotoma'
import { snapshotField } from './formatters'
import { entityDisplayName } from './humanize'

/** How the account is primarily denominated / held (for dashboard grouping). */
export type AccountDenominationKind = 'crypto' | 'fiat_cash' | 'investments' | 'mixed' | 'other'

const KIND_SORT_ORDER: Record<AccountDenominationKind, number> = {
  crypto: 0,
  fiat_cash: 1,
  investments: 2,
  mixed: 3,
  other: 4,
}

/** For table sort: non-accounts sort after all financial_account kinds. */
const NON_FINANCIAL_ACCOUNT_DENOM_SORT = 100

/** ISO-4217-style tickers that are crypto / stablecoins (not fiat cash). */
const CRYPTO_CURRENCIES = new Set(
  [
    'BTC',
    'ETH',
    'SOL',
    'STX',
    'USDT',
    'USDC',
    'DAI',
    'ADA',
    'DOT',
    'AVAX',
    'MATIC',
    'POL',
    'LINK',
    'UNI',
    'ATOM',
    'XRP',
    'LTC',
    'BCH',
    'DOGE',
    'TRX',
    'TON',
    'NEAR',
    'APT',
    'SUI',
    'ARB',
    'OP',
    'STRK',
    'WBTC',
    'WETH',
  ].map((c) => c.toUpperCase()),
)

const CRYPTO_INSTITUTION_RE =
  /\b(coinbase|kraken|binance|ledger|trezor|metamask|crypto\.com|gemini|okx|bybit|kucoin|nexo|electrum|exodus|phantom|rainbow|uniswap|aave|lido|stacks)\b/i

const CRYPTO_ASSET_RE = /\b(btc|bitcoin|eth|ethereum|sol|solana|stx|stacks|usdt|usdc|dai|ada|dot|avax|xrp|matic|pol|link)\b/i

const CASH_LIKE_RE =
  /\b(checking|savings|cash\s*management|money\s*market|high\s*yield|deposit\s*account|current\s*account|giro|iban|banking)\b/i

const INVESTMENT_RE =
  /\b(ira|sep|401k|401\(k\)|brokerage|broker|investment|mutual\s*fund|etf|retirement|pension\s*plan|vanguard|fidelity|schwab|blackrock|ishares|t\.?\s*rowe|portfolio\s*account|equity|equities)\b/i

function rowText(r: SheetRow): string {
  return [r['Asset'], r['Description'], r['Currency'], r['Ticker']]
    .map((x) => String(x ?? ''))
    .join(' ')
}

function rowsHaveAllocation(rows: SheetRow[] | undefined): boolean {
  if (!rows?.length) return false
  return rows.some((r) => /\d+\s*%/.test(rowText(r)))
}

function rowsSuggestCrypto(rows: SheetRow[] | undefined): boolean {
  if (!rows?.length) return false
  return rows.some((r) => CRYPTO_ASSET_RE.test(rowText(r)))
}

function rowsSuggestSecurities(rows: SheetRow[] | undefined): boolean {
  if (!rows?.length) return false
  return rows.some((r) => {
    const t = rowText(r)
    if (CRYPTO_ASSET_RE.test(t)) return false
    return /\d+\s*%/.test(t) || /\b[A-Z]{2,5}(?:\s+\d+|\s*$)/.test(String(r['Asset'] ?? r['Description'] ?? ''))
  })
}

function is721OrCustody(snapshot: Record<string, unknown>): boolean {
  const tags = snapshotField<string[]>(snapshot, 'filing_tags')
  if (tags?.includes('721')) return true
  const t = String(snapshotField<string>(snapshot, 'account_type') ?? '').toLowerCase()
  return t.includes('custod')
}

function primaryCurrencies(snapshot: Record<string, unknown>): string[] {
  const out: string[] = []
  for (const k of ['currency', 'account_value_currency'] as const) {
    const c = String(snapshotField<string>(snapshot, k) ?? '')
      .toUpperCase()
      .trim()
    if (c && /^[A-Z]{2,10}$/.test(c)) out.push(c)
  }
  return out
}

export interface AccountDenomination {
  kind: AccountDenominationKind
  /** Short badge label */
  label: string
  /** Tooltip / screen reader: why we chose this kind */
  detail: string
}

export function compareAccountDenomination(a: AccountDenominationKind, b: AccountDenominationKind): number {
  return KIND_SORT_ORDER[a] - KIND_SORT_ORDER[b]
}

const KIND_LABEL: Record<AccountDenominationKind, string> = {
  crypto: 'Crypto',
  fiat_cash: 'Fiat',
  investments: 'Securities',
  mixed: 'Mixed',
  other: 'Other',
}

/**
 * Classify a financial_account for UI.
 * Prefers the canonical `denomination_category` field when stored; falls back to heuristics.
 */
export function getAccountDenomination(entity: Entity): AccountDenomination {
  const snap = entity.snapshot
  if (!snap || typeof snap !== 'object') {
    return { kind: 'other', label: 'Other', detail: 'No snapshot' }
  }

  const stored = snapshotField<string>(snap, 'denomination_category')
  if (stored && stored in KIND_SORT_ORDER) {
    const kind = stored as AccountDenominationKind
    return { kind, label: KIND_LABEL[kind], detail: 'Stored denomination category' }
  }

  const accountType = String(snapshotField<string>(snap, 'account_type') ?? '')
  const institution = String(snapshotField<string>(snap, 'institution') ?? '')
  const registry = String(snapshotField<string>(snap, 'registry_id') ?? '')
  const name = entityDisplayName(entity)
  const haystack = `${institution} ${name} ${registry} ${accountType}`.toLowerCase()

  const rows = snapshotField<SheetRow[]>(snap, 'rows')
  const rowCrypto = rowsSuggestCrypto(rows)
  const rowSec = rowsSuggestSecurities(rows)
  const custody = is721OrCustody(snap)
  const instCrypto = CRYPTO_INSTITUTION_RE.test(haystack)

  const ccys = primaryCurrencies(snap)
  const ccyCrypto = ccys.some((c) => CRYPTO_CURRENCIES.has(c))

  if (rowCrypto && rowSec) {
    return { kind: 'mixed', label: 'Mixed', detail: 'Holdings suggest both crypto and traditional securities' }
  }

  if (custody || ccyCrypto || instCrypto || (rowCrypto && !rowSec)) {
    return {
      kind: 'crypto',
      label: 'Crypto',
      detail: custody
        ? '721 / custody tagging or account type'
        : ccyCrypto
          ? `Primary currency ${ccys.join(', ')}`
          : instCrypto
            ? 'Institution or name'
            : 'Holdings reference crypto assets',
    }
  }

  if (CASH_LIKE_RE.test(haystack)) {
    return { kind: 'fiat_cash', label: 'Fiat', detail: 'Cash, checking, or savings style account' }
  }

  if (INVESTMENT_RE.test(haystack) || rowsHaveAllocation(rows) || (rowSec && !rowCrypto)) {
    return {
      kind: 'investments',
      label: 'Securities',
      detail: INVESTMENT_RE.test(haystack)
        ? 'Account type, institution, or name'
        : 'Holdings rows look like funds / allocations',
    }
  }

  const fiatCcy = ccys.length > 0 && ccys.every((c) => !CRYPTO_CURRENCIES.has(c))
  if (fiatCcy) {
    return { kind: 'fiat_cash', label: 'Fiat', detail: ccys.length ? `Primary currency ${ccys.join(', ')}` : 'Fiat currency' }
  }

  return { kind: 'other', label: 'Other', detail: 'Could not infer denomination' }
}

export function getAccountDenominationSortOrder(entity: Entity): number {
  if (entity.entity_type !== 'financial_account') return NON_FINANCIAL_ACCOUNT_DENOM_SORT
  return KIND_SORT_ORDER[getAccountDenomination(entity).kind]
}

export function denominationBadgeClass(kind: AccountDenominationKind): string {
  switch (kind) {
    case 'crypto':
      return 'border-amber-500/50 bg-amber-500/10 text-amber-950 dark:text-amber-100'
    case 'fiat_cash':
      return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-950 dark:text-emerald-100'
    case 'investments':
      return 'border-sky-500/40 bg-sky-500/10 text-sky-950 dark:text-sky-100'
    case 'mixed':
      return 'border-violet-500/40 bg-violet-500/10 text-violet-950 dark:text-violet-100'
    default:
      return 'border-border bg-muted/50 text-muted-foreground'
  }
}
