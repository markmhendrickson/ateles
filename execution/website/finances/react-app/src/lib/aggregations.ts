import type { Entity, SheetRow } from '@/types/neotoma'
import { snapshotField } from './formatters'

/** Per-entity USD-per-EUR (valuation date from snapshot) or a constant. */
export type UsdPerEurResolver = (entity: Entity) => number
export type UsdPerEurInput = number | UsdPerEurResolver

function resolvePair(fx: UsdPerEurInput): UsdPerEurResolver {
  if (typeof fx === 'function') return fx
  const n = fx
  return () => n
}

export interface BucketAggregate {
  bucket: string
  totalEur: number
  count: number
  entities: Entity[]
}

const EURO_KEY_RE = /€|\u20ac|eur|euro/i
const USD_KEY_RE = /\$|usd|dollar|u\.s\./i

/** Parse a cell that may be number or formatted string like "€26" or " $ 30 ". */
function parseMoneyCell(raw: unknown): number {
  if (raw == null || raw === '') return 0
  if (typeof raw === 'number') return isNaN(raw) ? 0 : raw
  const s = String(raw).replace(/[€$£,\s]/g, '').replace(/[^\d.-]/g, '')
  const val = parseFloat(s)
  return isNaN(val) ? 0 : val
}

function parseEurValue(rows: SheetRow[] | undefined): number {
  if (!rows || rows.length === 0) return 0
  return rows.reduce((sum, row) => {
    let cell: unknown = row['€'] ?? row['EUR'] ?? row['eur']
    if (cell === undefined || cell === '' || cell === null) {
      for (const [k, v] of Object.entries(row)) {
        if (EURO_KEY_RE.test(k) && v != null && String(v).trim() !== '') {
          cell = v
          break
        }
      }
    }
    return sum + parseMoneyCell(cell)
  }, 0)
}

function parseUsdValue(rows: SheetRow[] | undefined): number {
  if (!rows || rows.length === 0) return 0
  return rows.reduce((sum, row) => {
    let cell: unknown = row['$'] ?? row['USD'] ?? row['usd']
    if (cell === undefined || cell === '' || cell === null) {
      for (const [k, v] of Object.entries(row)) {
        if (USD_KEY_RE.test(k) && !EURO_KEY_RE.test(k) && v != null && String(v).trim() !== '') {
          cell = v
          break
        }
      }
    }
    return sum + parseMoneyCell(cell)
  }, 0)
}

const TOP_EUR_KEY_HINTS =
  /eur|€|\u20ac|euro|value_eur|total_eur|balance_eur|market_value|portfolio|eur_value|eur_equivalent/i
const TOP_USD_KEY_HINTS = /usd|\$|dollar|value_usd|total_usd|balance_usd|market_value_usd/i

const TOP_NUMERIC_EXCLUDE_KEY =
  /^(rows|provenance|observation_kind|schema_version|entity_id|entity_type|canonical_name|registry_id|assets_sheet|import|sha256|source_file|as_of|observation_count|last_observation)/i

/** Near-zero threshold for raw currency legs (sheet vs top-level merge). */
const RAW_LEG_EPS = 1e-9

/** Snapshot keys that hold a calendar/tax year (not a cash balance). */
const SNAPSHOT_YEAR_FIELD_KEYS = [
  'tax_year_context',
  'filing_year',
  'reporting_year',
  'calendar_year',
  'statement_year',
  'modelo_year',
  'tax_year',
] as const

function snapshotYearIntegerSet(snap: Record<string, unknown>): Set<number> {
  const s = new Set<number>()
  for (const key of SNAPSHOT_YEAR_FIELD_KEYS) {
    const y = snap[key]
    if (typeof y === 'number' && Number.isFinite(y)) s.add(Math.round(y))
    if (typeof y === 'string' && /^\s*\d{4}\s*$/.test(y)) s.add(Number(y.trim()))
  }
  return s
}

/** True when `eur` is an integer that matches a year field on the snapshot (common sheet mis-import). */
function eurMatchesSnapshotYearField(eur: number, snap: Record<string, unknown>): boolean {
  const r = Math.round(eur)
  if (!Number.isFinite(eur) || Math.abs(eur - r) > 1e-6) return false
  return snapshotYearIntegerSet(snap).has(r)
}

function isUsdishAccountSnapshot(snap: Record<string, unknown>, usdFromRows = 0): boolean {
  const ccy = String(snap.currency ?? '').toUpperCase()
  if (ccy === 'USD' || ccy === 'US') return true
  if (Math.abs(usdFromRows) > RAW_LEG_EPS) return true
  if (typeof snap.balance_usd === 'number' && !isNaN(snap.balance_usd) && Math.abs(snap.balance_usd) > RAW_LEG_EPS) {
    return true
  }
  return false
}

/**
 * Drop EUR amounts that equal a stored tax/calendar year on USD (or USD-balance) accounts.
 * Prevents every workbook row showing the same €2,024 when `balance_eur` was filled with 2024.
 */
function shouldStripEurAsYearMisimport(
  eur: number,
  snap: Record<string, unknown>,
  usdFromRows = 0,
): boolean {
  if (!eurMatchesSnapshotYearField(eur, snap)) return false
  return isUsdishAccountSnapshot(snap, usdFromRows)
}

function finalizeRawLegsAfterYearNoise(
  eur: number,
  usd: number,
  snap: Record<string, unknown>,
  usdFromRows: number,
): { eur: number; usd: number } {
  if (!shouldStripEurAsYearMisimport(eur, snap, usdFromRows)) return { eur, usd }
  let u = usd
  if (Math.abs(u) <= RAW_LEG_EPS && typeof snap.balance_usd === 'number' && !isNaN(snap.balance_usd)) {
    u = snap.balance_usd
  }
  return { eur: 0, usd: u }
}

/** Default USD per EUR when Frankfurter has not loaded or fails (matches prior hardcoded UI behavior). */
export const FALLBACK_USD_PER_EUR = 1.08

/** @deprecated Use {@link FALLBACK_USD_PER_EUR} or live rate from `FxRateProvider` / `useFxRate`. */
export const IMPLIED_USD_PER_EUR = FALLBACK_USD_PER_EUR

function roundMoneyBands(abs: number): number {
  let out = abs
  if (out >= 1_000_000) out = Math.round(out / 1000) * 1000
  else if (out >= 100_000) out = Math.round(out / 100) * 100
  else if (out >= 10_000) out = Math.round(out / 50) * 50
  else if (out >= 1_000) out = Math.round(out / 10) * 10
  else if (out >= 100) out = Math.round(out * 10) / 10
  else out = Math.round(out * 100) / 100
  return out
}

/** USD display from EUR at `usdPerEur` (human-style rounding). */
export function roundUsdFromEur(eurAmount: number, usdPerEur: number = FALLBACK_USD_PER_EUR): number {
  if (!Number.isFinite(eurAmount)) return eurAmount
  const n = eurAmount * usdPerEur
  const sign = n < 0 ? -1 : 1
  const abs = Math.abs(n)
  if (abs === 0) return 0
  return roundMoneyBands(abs) * sign
}

/** EUR display from USD at `usdPerEur` (human-style rounding). */
export function roundEurFromUsd(usdAmount: number, usdPerEur: number = FALLBACK_USD_PER_EUR): number {
  if (!Number.isFinite(usdAmount)) return usdAmount
  const n = usdAmount / usdPerEur
  const sign = n < 0 ? -1 : 1
  const abs = Math.abs(n)
  if (abs === 0) return 0
  return roundMoneyBands(abs) * sign
}

/** Sum top-level snapshot numbers whose names look like EUR or USD (avoids counting market_value_usd as EUR). */
function topLevelCurrencyNumbers(snap: Record<string, unknown>, mode: 'eur' | 'usd'): number {
  let sum = 0
  for (const [k, v] of Object.entries(snap)) {
    if (TOP_NUMERIC_EXCLUDE_KEY.test(k)) continue
    if (mode === 'eur') {
      if (!TOP_EUR_KEY_HINTS.test(k)) continue
      if ((/\busd\b|_usd$/i.test(k) || /\$/i.test(k)) && !EURO_KEY_RE.test(k)) continue
    } else {
      if (!TOP_USD_KEY_HINTS.test(k)) continue
    }
    if (typeof v === 'number' && !isNaN(v)) {
      if (mode === 'eur' && shouldStripEurAsYearMisimport(v, snap, 0)) continue
      sum += v
      continue
    }
    if (typeof v === 'string' && v.trim() !== '') {
      const parsed = parseMoneyCell(v)
      if (mode === 'eur' && shouldStripEurAsYearMisimport(parsed, snap, 0)) continue
      sum += parsed
    }
  }
  return sum
}

/** When structured fields/rows yield 0, use the largest plausible money-like top-level number. */
function fallbackTopLevelMoneyNumber(snap: Record<string, unknown>, preferEur: boolean): number {
  const EXCLUDE = /unit|percent|rate|apr|wallet|account|count|index|version|sha|id$/i
  let best = 0
  for (const [k, v] of Object.entries(snap)) {
    if (TOP_NUMERIC_EXCLUDE_KEY.test(k)) continue
    if (EXCLUDE.test(k)) continue
    if (typeof v !== 'number' || isNaN(v)) continue
    if (v < 10 || v > 1e15) continue
    if (preferEur && shouldStripEurAsYearMisimport(v, snap, 0)) continue
    if (preferEur && USD_KEY_RE.test(k) && !EURO_KEY_RE.test(k)) continue
    if (!preferEur && EURO_KEY_RE.test(k) && !USD_KEY_RE.test(k)) continue
    if (v > best) best = v
  }
  return best
}

/**
 * True when there is a real stored balance signal (row money columns or known snapshot balance / NAV fields).
 * If false, we return 0 instead of guessing from name-pattern heuristics — equity placeholders with no balances show "—".
 */
function hasExplicitBalanceRelevantData(
  snap: Record<string, unknown>,
  rows: SheetRow[] | undefined,
  rowEur: number,
  rowUsd: number,
): boolean {
  if (Math.abs(rowUsd) > RAW_LEG_EPS || Math.abs(rowEur) > RAW_LEG_EPS) return true
  const top = snapshotTopLevelStorageLegs(snap)
  return Math.abs(top.eur) > RAW_LEG_EPS || Math.abs(top.usd) > RAW_LEG_EPS
}

/**
 * True when the account is modeled as revolving credit / charge card for display sign.
 * Prefers the canonical `display_sign` field when available; falls back to heuristics.
 */
export function isCreditCardStyleFinancialAccount(entity: Entity | null | undefined): boolean {
  if (!entity?.snapshot || entity.entity_type !== 'financial_account') return false
  const snap = entity.snapshot

  const ds = snapshotField<number>(snap, 'display_sign')
  if (typeof ds === 'number') return ds < 0

  const isLiabilityRaw = snapshotField<boolean | string>(snap, 'is_liability')
  const isLiability =
    isLiabilityRaw === true || (typeof isLiabilityRaw === 'string' && /^(true|1|yes)$/i.test(isLiabilityRaw.trim()))
  if (isLiability) {
    const lt = String(snapshotField<string>(snap, 'liability_type') ?? '').toLowerCase()
    if (!lt || /(credit\s*card|creditcard|charge\s*card|revolving|line\s*of\s*credit|\bcc\b)/i.test(lt)) return true
  }

  const liabilityType = String(snapshotField<string>(snap, 'liability_type') ?? '').toLowerCase().trim()
  if (/(credit\s*card|creditcard|charge\s*card|revolving|line\s*of\s*credit|\bcc\b)/i.test(liabilityType)) return true

  const t = String(snapshotField<string>(snap, 'account_type') ?? '').toLowerCase().trim()
  if (/(credit\s*card|creditcard|charge\s*card|revolving|line\s*of\s*credit|\bcc\b)/i.test(t)) return true

  return false
}

export function applyFinancialAccountLiabilityDisplaySign(amount: number, entity: Entity | null | undefined): number {
  if (!isCreditCardStyleFinancialAccount(entity)) return amount
  if (!Number.isFinite(amount) || Math.abs(amount) < 1e-12) return amount
  return -amount
}

export function getEntityValueEur(entity: Entity, fx: UsdPerEurInput = FALLBACK_USD_PER_EUR): number {
  const usdPerEur = resolvePair(fx)(entity)
  const snap = entity.snapshot
  if (!snap) return 0

  const bv = snap.balance_value
  const bc = String(snap.balance_currency ?? '').toUpperCase()
  if (typeof bv === 'number' && Number.isFinite(bv) && Math.abs(bv) > RAW_LEG_EPS) {
    if (bc === 'USD') return bv / usdPerEur
    return bv
  }

  const rows = snap.rows as SheetRow[] | undefined
  let rowUsd = 0
  let rowEur = 0

  if (rows && rows.length > 0) {
    rowUsd = parseUsdValue(rows)
    const eurParsed = parseEurValue(rows)
    rowEur = shouldStripEurAsYearMisimport(eurParsed, snap, rowUsd) ? 0 : eurParsed

    if (Math.abs(rowUsd) > RAW_LEG_EPS) {
      const impliedEurFromUsd = rowUsd / usdPerEur
      if (Math.abs(rowEur) <= RAW_LEG_EPS) return impliedEurFromUsd
      if (Math.abs(impliedEurFromUsd) > Math.abs(rowEur) * 1.5) return impliedEurFromUsd
      return rowEur
    }

    if (rowEur !== 0) return rowEur
  }

  if (!hasExplicitBalanceRelevantData(snap, rows, rowEur, rowUsd)) {
    return 0
  }

  const topLegs = snapshotTopLevelStorageLegs(snap)
  if (Math.abs(topLegs.eur) > RAW_LEG_EPS) return topLegs.eur
  if (Math.abs(topLegs.usd) > RAW_LEG_EPS) return topLegs.usd / usdPerEur

  const ccyTop = String(snap.currency ?? '').toUpperCase()
  const skipHeuristicEurOnUsdLabel = ccyTop === 'USD' || ccyTop === 'US'

  if (!skipHeuristicEurOnUsdLabel) {
    const fromTop = topLevelCurrencyNumbers(snap, 'eur')
    if (fromTop !== 0) return fromTop
  }

  if (typeof snap.balance_eur === 'number' && !shouldStripEurAsYearMisimport(snap.balance_eur, snap, 0)) {
    return snap.balance_eur
  }
  if (typeof snap.balance === 'number') {
    if (!skipHeuristicEurOnUsdLabel) return snap.balance
  }

  if (skipHeuristicEurOnUsdLabel) return 0

  return fallbackTopLevelMoneyNumber(snap, true)
}

export function getEntityValueUsd(entity: Entity, fx: UsdPerEurInput = FALLBACK_USD_PER_EUR): number {
  const usdPerEur = resolvePair(fx)(entity)
  const snap = entity.snapshot
  if (!snap) return 0

  const bv = snap.balance_value
  const bc = String(snap.balance_currency ?? '').toUpperCase()
  if (typeof bv === 'number' && Number.isFinite(bv) && Math.abs(bv) > RAW_LEG_EPS) {
    if (bc === 'USD') return bv
    return bv * usdPerEur
  }

  const rows = snap.rows as SheetRow[] | undefined
  let rowUsd = 0
  let rowEur = 0

  if (rows && rows.length > 0) {
    rowUsd = parseUsdValue(rows)
    if (rowUsd !== 0) return rowUsd

    const eurParsed = parseEurValue(rows)
    rowEur = shouldStripEurAsYearMisimport(eurParsed, snap, rowUsd) ? 0 : eurParsed
    if (rowEur !== 0) return rowEur * usdPerEur
  }

  if (!hasExplicitBalanceRelevantData(snap, rows, rowEur, rowUsd)) {
    return 0
  }

  const topLegs = snapshotTopLevelStorageLegs(snap)
  if (Math.abs(topLegs.usd) > RAW_LEG_EPS) return topLegs.usd
  if (Math.abs(topLegs.eur) > RAW_LEG_EPS) return topLegs.eur * usdPerEur

  const fromTop = topLevelCurrencyNumbers(snap, 'usd')
  if (fromTop !== 0) return fromTop

  return fallbackTopLevelMoneyNumber(snap, false)
}

/**
 * One comparable EUR total per account for portfolio sums: uses the stronger of the EUR leg and
 * USD converted to EUR so USD-heavy / EUR-stale accounts still count once (no double-count).
 */
export function getEntityCanonicalEur(entity: Entity, fx: UsdPerEurInput = FALLBACK_USD_PER_EUR): number {
  const usdPerEur = resolvePair(fx)(entity)
  const eur = getEntityValueEur(entity, fx)
  const usd = getEntityValueUsd(entity, fx)
  const fromUsd = Math.abs(usd) > RAW_LEG_EPS ? usd / usdPerEur : 0
  if (Math.abs(eur) <= RAW_LEG_EPS && Math.abs(fromUsd) <= RAW_LEG_EPS) return 0
  if (Math.abs(eur) <= RAW_LEG_EPS) return fromUsd
  if (Math.abs(fromUsd) <= RAW_LEG_EPS) return eur
  return Math.max(eur, fromUsd)
}

/**
 * Top-level snapshot fields only (no sheet rows). Same cascade as legacy {@link getEntityRawStorageLegs}
 * before row merge was added.
 */
function snapshotTopLevelStorageLegs(snap: Record<string, unknown>): { eur: number; usd: number } {
  let eur = 0
  let usd = 0

  /** Canonical filing / dashboard value: one number + ISO currency (falls back to `currency`). */
  let fromAccountValueEur = false
  let fromAccountValueUsd = false
  if (typeof snap.account_value === 'number' && !isNaN(snap.account_value)) {
    const ac = String(snap.account_value_currency ?? snap.currency ?? 'EUR').toUpperCase()
    if (ac === 'USD' || ac === 'US') {
      usd = snap.account_value
      fromAccountValueUsd = true
    } else {
      eur = snap.account_value
      fromAccountValueEur = true
    }
  }

  // Some imports write normalized EUR/USD legs directly and omit `account_value`.
  if (!fromAccountValueEur && typeof snap.account_value_eur === 'number' && !isNaN(snap.account_value_eur)) {
    eur = snap.account_value_eur
    fromAccountValueEur = true
  }
  if (!fromAccountValueUsd && typeof snap.account_value_usd === 'number' && !isNaN(snap.account_value_usd)) {
    usd = snap.account_value_usd
    fromAccountValueUsd = true
  }

  /**
   * Legacy statement imports may still write ending values under source-specific keys.
   * Accept them during migration, but prefer canonical `account_value` upstream.
   */
  if (Math.abs(eur) <= RAW_LEG_EPS && Math.abs(usd) <= RAW_LEG_EPS) {
    if (typeof snap.ending_account_value === 'number' && !isNaN(snap.ending_account_value)) {
      const endingCurrency = String(snap.ending_account_value_currency ?? snap.currency ?? 'EUR').toUpperCase()
      if (endingCurrency === 'USD' || endingCurrency === 'US') usd = snap.ending_account_value
      else eur = snap.ending_account_value
    }
  }

  if (!fromAccountValueEur && typeof snap.balance_eur === 'number' && !isNaN(snap.balance_eur)) {
    eur = snap.balance_eur
  }
  if (!fromAccountValueUsd && typeof snap.balance_usd === 'number' && !isNaN(snap.balance_usd)) {
    usd = snap.balance_usd
  }

  if (Math.abs(eur) <= RAW_LEG_EPS && Math.abs(usd) <= RAW_LEG_EPS) {
    if (typeof snap.outstanding_principal_eur === 'number' && !isNaN(snap.outstanding_principal_eur)) {
      eur = snap.outstanding_principal_eur
    } else if (typeof snap.outstanding_principal === 'number' && !isNaN(snap.outstanding_principal)) {
      eur = snap.outstanding_principal
    }
  }

  if (Math.abs(eur) <= RAW_LEG_EPS && Math.abs(usd) <= RAW_LEG_EPS) {
    if (typeof snap.amount_eur === 'number' && !isNaN(snap.amount_eur)) {
      eur = snap.amount_eur
    } else if (typeof snap.amount === 'number' && !isNaN(snap.amount)) {
      const ccy = String(snap.currency ?? '').toUpperCase()
      if (ccy === 'USD') usd = snap.amount
      else eur = snap.amount
    }
  }

  if (
    Math.abs(eur) <= RAW_LEG_EPS &&
    Math.abs(usd) <= RAW_LEG_EPS &&
    typeof snap.yearly_total_eur === 'number' &&
    !isNaN(snap.yearly_total_eur)
  ) {
    eur = snap.yearly_total_eur
  }

  if (Math.abs(eur) <= RAW_LEG_EPS && Math.abs(usd) <= RAW_LEG_EPS && typeof snap.balance === 'number' && !isNaN(snap.balance)) {
    const ccy = String(snap.currency ?? 'EUR').toUpperCase()
    if (ccy === 'USD') usd = snap.balance
    else eur = snap.balance
  }

  if (Math.abs(eur) <= RAW_LEG_EPS && Math.abs(usd) <= RAW_LEG_EPS) {
    if (typeof snap.market_value_eur === 'number' && !isNaN(snap.market_value_eur)) eur = snap.market_value_eur
    if (typeof snap.market_value_usd === 'number' && !isNaN(snap.market_value_usd)) usd = snap.market_value_usd
  }

  if (Math.abs(eur) <= RAW_LEG_EPS && Math.abs(usd) <= RAW_LEG_EPS) {
    if (typeof snap.ending_account_value_eur === 'number' && !isNaN(snap.ending_account_value_eur)) {
      eur = snap.ending_account_value_eur
    }
    if (typeof snap.ending_account_value_usd === 'number' && !isNaN(snap.ending_account_value_usd)) {
      usd = snap.ending_account_value_usd
    }
  }

  if (Math.abs(eur) <= RAW_LEG_EPS && Math.abs(usd) <= RAW_LEG_EPS) {
    if (typeof snap.value_eur === 'number' && !isNaN(snap.value_eur)) eur = snap.value_eur
    if (typeof snap.value_usd === 'number' && !isNaN(snap.value_usd)) usd = snap.value_usd
  }

  if (shouldStripEurAsYearMisimport(eur, snap, 0)) eur = 0

  return { eur, usd }
}

/** When no rows or row sums are empty, use top-level keys and then name-heuristic EUR/USD numbers. */
function rawLegsFromTopLevelFallback(snap: Record<string, unknown>): { eur: number; usd: number } {
  const rows = snap.rows as SheetRow[] | undefined
  let rowUsd = 0
  let rowEur = 0
  if (rows && rows.length > 0) {
    rowUsd = parseUsdValue(rows)
    const eurParsed = parseEurValue(rows)
    rowEur = shouldStripEurAsYearMisimport(eurParsed, snap, rowUsd) ? 0 : eurParsed
  }

  if (!hasExplicitBalanceRelevantData(snap, rows, rowEur, rowUsd)) {
    return { eur: 0, usd: 0 }
  }

  const top = snapshotTopLevelStorageLegs(snap)
  if (Math.abs(top.eur) > RAW_LEG_EPS || Math.abs(top.usd) > RAW_LEG_EPS) return top
  const te = topLevelCurrencyNumbers(snap, 'eur')
  const tu = topLevelCurrencyNumbers(snap, 'usd')
  if (te !== 0 || tu !== 0) return { eur: te, usd: tu }
  return { eur: 0, usd: 0 }
}

/**
 * True when the API snapshot has no EUR/USD storage legs the dashboard would use.
 * Neotoma may still hold balances on observations that are not projected into `snapshot`.
 */
export function financialAccountSnapshotMissingMoney(entity: Entity | null | undefined): boolean {
  if (!entity || entity.entity_type !== 'financial_account') return false
  const raw = getEntityRawStorageLegs(entity)
  return Math.abs(raw.eur) <= RAW_LEG_EPS && Math.abs(raw.usd) <= RAW_LEG_EPS
}

/**
 * Raw EUR and USD totals from sheet rows and/or explicit snapshot fields (no FX cross-implication).
 * When `rows` exist but omit a currency, fills that leg from `balance_eur` / `balance_usd` / `balance`+`currency`
 * (and top-level fallbacks) so USD brokerage balances are not invisible to “stored” and display heuristics.
 */
export function getEntityRawStorageLegs(entity: Entity): { eur: number; usd: number } {
  const snap = entity.snapshot
  if (!snap) return { eur: 0, usd: 0 }

  const rows = snap.rows as SheetRow[] | undefined
  const usdRowHint = rows && rows.length > 0 ? parseUsdValue(rows) : 0

  let out: { eur: number; usd: number }
  if (!rows || rows.length === 0) {
    out = rawLegsFromTopLevelFallback(snap)
  } else {
    let eur = parseEurValue(rows)
    let usd = parseUsdValue(rows)
    const top = snapshotTopLevelStorageLegs(snap)

    if (Math.abs(eur) <= RAW_LEG_EPS && Math.abs(top.eur) > RAW_LEG_EPS) eur = top.eur
    if (Math.abs(usd) <= RAW_LEG_EPS && Math.abs(top.usd) > RAW_LEG_EPS) usd = top.usd

    if (Math.abs(eur) <= RAW_LEG_EPS && Math.abs(usd) <= RAW_LEG_EPS) {
      out = rawLegsFromTopLevelFallback(snap)
    } else {
      out = { eur, usd }
    }
  }

  return finalizeRawLegsAfterYearNoise(out.eur, out.usd, snap, usdRowHint)
}

/**
 * Comparable EUR for **UI display** when Neotoma storage is unambiguous:
 * - **EUR-only** raw legs → use stored EUR (no cross-rate guess).
 * - **USD-only** raw legs → implied EUR via {@link roundEurFromUsd} at `usdPerEur` (valuation-date rate).
 * - **Both** or **neither** → use `canonicalEur` ({@link getEntityCanonicalEur} max-of-legs logic).
 *
 * Avoids showing a large EUR primary (from a stale EUR field) when the snapshot’s meaningful balance is USD-only.
 */
export function comparableEurFromStorageForDisplay(
  entity: Entity | null | undefined,
  canonicalEur: number,
  usdPerEur: number,
): number {
  if (!entity?.snapshot) return canonicalEur
  const snap = entity.snapshot
  const raw = getEntityRawStorageLegs(entity)
  const hasEur = Math.abs(raw.eur) > RAW_LEG_EPS
  const hasUsd = Math.abs(raw.usd) > RAW_LEG_EPS
  if (hasEur && !hasUsd) return raw.eur
  if (hasUsd && !hasEur) return roundEurFromUsd(raw.usd, usdPerEur)

  if (hasEur && hasUsd) {
    const ccy = String(snap.currency ?? '').toUpperCase()
    if (ccy === 'USD') return roundEurFromUsd(raw.usd, usdPerEur)
    if (ccy === 'EUR' || ccy === 'EURO') return raw.eur
    return canonicalEur
  }

  return canonicalEur
}

/**
 * True when {@link comparableEurFromStorageForDisplay} uses the **USD** raw leg (converted to EUR at `usdPerEur`).
 * Used for EUR-primary FX tooltips.
 */
export function isDisplayBasisUsdSourced(entity: Entity | null | undefined): boolean {
  if (!entity?.snapshot) return false
  const raw = getEntityRawStorageLegs(entity)
  const hasEur = Math.abs(raw.eur) > RAW_LEG_EPS
  const hasUsd = Math.abs(raw.usd) > RAW_LEG_EPS
  if (!hasUsd) return false
  const ccy = String(entity.snapshot.currency ?? '').toUpperCase()
  if (!hasEur) return true
  return ccy === 'USD' || ccy === 'US'
}

/**
 * True when {@link comparableEurFromStorageForDisplay} uses the **EUR** raw leg (not USD converted to EUR).
 * Symmetric to {@link isDisplayBasisUsdSourced} for EUR-primary FX tooltips and native EUR display.
 */
export function isDisplayBasisEurSourced(entity: Entity | null | undefined): boolean {
  if (!entity?.snapshot) return false
  const raw = getEntityRawStorageLegs(entity)
  const hasEur = Math.abs(raw.eur) > RAW_LEG_EPS
  const hasUsd = Math.abs(raw.usd) > RAW_LEG_EPS
  if (!hasEur) return false
  if (!hasUsd) return true
  const ccy = String(entity.snapshot.currency ?? '').toUpperCase()
  return ccy === 'EUR' || ccy === 'EURO'
}

/**
 * EUR amount the UI uses for a row (after {@link comparableEurFromStorageForDisplay}).
 * Use for table sort and net-worth sums so ordering matches “Balance (display · stored)”.
 */
export function getEntityMonetaryDisplayBasisEur(entity: Entity, fx: UsdPerEurInput = FALLBACK_USD_PER_EUR): number {
  const resolve = resolvePair(fx)
  const usdPerEur = resolve(entity)
  const canonical = getEntityCanonicalEur(entity, fx)
  const basis = comparableEurFromStorageForDisplay(entity, canonical, usdPerEur)
  return applyFinancialAccountLiabilityDisplaySign(basis, entity)
}

/** Implied USD total for a strategy bucket (per-entity valuation rate × display-basis EUR). */
export function bucketNetWorthUsd(bucket: BucketAggregate, fx: UsdPerEurInput = FALLBACK_USD_PER_EUR): number {
  const resolve = resolvePair(fx)
  return bucket.entities.reduce(
    (sum, entity) => sum + getEntityMonetaryDisplayBasisEur(entity, fx) * resolve(entity),
    0,
  )
}

export function groupByStrategyBucket(
  accounts: Entity[],
  fx: UsdPerEurInput = FALLBACK_USD_PER_EUR,
): BucketAggregate[] {
  const map = new Map<string, BucketAggregate>()

  for (const entity of accounts) {
    const bucket = snapshotField<string>(entity.snapshot, 'strategy_bucket') || 'uncategorized'
    const valueEur = getEntityMonetaryDisplayBasisEur(entity, fx)

    const existing = map.get(bucket)
    if (existing) {
      existing.totalEur += valueEur
      existing.count += 1
      existing.entities.push(entity)
    } else {
      map.set(bucket, { bucket, totalEur: valueEur, count: 1, entities: [entity] })
    }
  }

  return Array.from(map.values()).sort((a, b) => b.totalEur - a.totalEur)
}

export function groupByFilingTag(accounts: Entity[]): Record<string, Entity[]> {
  const groups: Record<string, Entity[]> = {}

  for (const entity of accounts) {
    const tags = snapshotField<string[]>(entity.snapshot, 'filing_tags')
    const tagList = tags && Array.isArray(tags) && tags.length > 0 ? tags : ['none']

    for (const tag of tagList) {
      if (!groups[tag]) groups[tag] = []
      groups[tag].push(entity)
    }
  }

  return groups
}

export function groupByField(entities: Entity[], field: string): Record<string, Entity[]> {
  const groups: Record<string, Entity[]> = {}

  for (const entity of entities) {
    const val = snapshotField<string>(entity.snapshot, field) || 'other'
    if (!groups[val]) groups[val] = []
    groups[val].push(entity)
  }

  return groups
}

/** Sum of per-account **display-basis** EUR (matches row labels in Accounts / Overview). */
export function totalNetWorthEur(accounts: Entity[], fx: UsdPerEurInput = FALLBACK_USD_PER_EUR): number {
  return accounts.reduce((sum, entity) => sum + getEntityMonetaryDisplayBasisEur(entity, fx), 0)
}

/**
 * Implied USD total: each account's display-basis EUR × that account's USD/EUR rate (valuation date when known).
 */
export function totalNetWorthUsd(accounts: Entity[], fx: UsdPerEurInput = FALLBACK_USD_PER_EUR): number {
  const resolve = resolvePair(fx)
  return accounts.reduce((sum, entity) => {
    const eur = getEntityMonetaryDisplayBasisEur(entity, fx)
    return sum + eur * resolve(entity)
  }, 0)
}
