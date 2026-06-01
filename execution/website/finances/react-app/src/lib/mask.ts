/**
 * Deterministic client-side masking for screen-share privacy.
 * Same input + salt → same output until salt rotates.
 * Amounts/counts/percents stay in realistic ranges vs the underlying value.
 */

export type MaskNumericKind = 'money' | 'count' | 'percent' | 'scalar'

import { splitKeySegments } from './propertyLabels'

export function fnv1a(str: string): number {
  let h = 2166136261
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

/** First segment of plausible institution-style names (fictional composites). */
const INSTITUTION_A = [
  'Meridian', 'Pacific', 'Harbor', 'Colonial', 'Metro', 'Summit', 'Union', 'Continental',
  'Northpoint', 'Westlake', 'Fairview', 'Bradford', 'Sterling', 'Crescent', 'Oakmont',
  'Riverside', 'Granite', 'Cascade', 'Alpine', 'Bayview', 'First Colonial', 'Lakeview',
]

/** Second segment (avoids real trademarks; reads like bank / broker naming). */
const INSTITUTION_B = [
  'Trust', 'National', 'Federal', 'Community', 'Mutual', 'Private Bank', 'Wealth',
  'Securities', 'Capital', 'Advisors', 'Savings', 'Credit Union', 'Asset Management',
]

const ACCOUNT_PRODUCTS = [
  'Checking', 'Savings', 'Brokerage', 'Traditional IRA', 'Roth IRA', 'Custody',
  'Money market', 'Cash management', 'Margin account', 'Business checking',
  'Trust account', 'Investment account', 'SEP IRA', 'Portfolio',
]

const CRYPTO_VENUE = [
  'Cold custody',
  'Exchange subaccount',
  'Hardware wallet',
  'Staking wallet',
  'Venue custody',
  'Institutional custody',
]

const VENDOR_SUFFIX = [
  'Services',
  'Solutions',
  'Partners',
  'Holdings',
  'Group',
  'LLC',
  'Utilities',
  'Retail',
]

/** Plausible personal-style stand-ins when input looks like a person name. */
const GIVEN_STANDIN = [
  'Alex', 'Jordan', 'Morgan', 'Casey', 'Riley', 'Taylor', 'Quinn', 'Avery', 'Jamie', 'Reese',
]

const FAMILY_STANDIN = [
  'Nguyen', 'Patel', 'Kowalski', 'Lindqvist', 'Okonkwo', 'Fernández', 'Nakamura', 'Bergsson',
]

/** Heuristic from call-site key (e.g. maskNumber(n, 'tx-tot')). */
export function inferMaskKind(_n: number, key: string): MaskNumericKind {
  const k = key.toLowerCase()
  if (/\bpct\b|percent|apr|\byield\b/.test(k)) return 'percent'
  if (
    /\b(pg|pgmax|tx-a|tx-b|acct-count|m721n|m720c|rex-n|loans-n|ex-tot|exo|obsc|rowsc|tx-tot)\b|etype:|^arr:|^rows:|fmt-num\b/.test(k) ||
    /_count$|^count$|item_count|entity_count|num_entities/i.test(k)
  ) {
    return 'count'
  }
  if (/chart:|pie:|hdr-|fmt-eur|fmt-usd|balance|amount|principal|payment|fee_eur|fee_usd|yearly|total_eur|total_usd/.test(k)) {
    return 'money'
  }
  return 'money'
}

/** JSON field name → kind (uses last path segment). */
function inferMaskKindFromKey(leafKey: string): MaskNumericKind {
  const k = leafKey.toLowerCase()
  if (/pct|percent|apr|yield|_rate$|^rate$/.test(k) && !/strategy|separate|corporate/.test(k)) return 'percent'
  if (
    /count|length|qty|quantity|items|offset|limit|page|num_|number_of|observation|version|index|rank|order/.test(k)
  ) {
    return 'count'
  }
  if (
    /amount|eur|usd|balance|price|value|principal|payment|fee|cost|total|market|nav|equity|debt|income|salary|tax/.test(
      k,
    )
  ) {
    return 'money'
  }
  return 'scalar'
}

/** Currency-like: tight multiplicative jitter + human rounding. */
export function maskMoney(value: number, salt: string, key = ''): number {
  if (!Number.isFinite(value)) return value
  const sign = value < 0 ? -1 : 1
  const abs = Math.abs(value)
  if (abs === 0) return 0
  const h = fnv1a(`${salt}:money:${key}:${abs.toFixed(4)}`)
  const scale = 0.9 + ((h % 2001) / 2001) * 0.2
  const wobble = 1 + ((((h >> 8) % 17) - 8) / 1000)
  let out = abs * scale * wobble
  if (out >= 1_000_000) out = Math.round(out / 1000) * 1000
  else if (out >= 100_000) out = Math.round(out / 100) * 100
  else if (out >= 10_000) out = Math.round(out / 50) * 50
  else if (out >= 1_000) out = Math.round(out / 10) * 10
  else if (out >= 100) out = Math.round(out * 10) / 10
  else out = Math.round(out * 100) / 100
  return Math.max(0, out) * sign
}

/** Counts / indices / page numbers: stay integer, small % drift. */
export function maskCount(value: number, salt: string, key = ''): number {
  if (!Number.isFinite(value)) return value
  const n = Math.max(0, Math.floor(Math.abs(value + 1e-9)))
  if (n === 0) return 0
  const h = fnv1a(`${salt}:cnt:${key}:${n}`)
  const deltaPct = ((h % 21) - 10) / 100
  let out = Math.round(n * (1 + deltaPct))
  if (out < 1 && n >= 1) out = 1 + (h % 12)
  if (out === n && n > 2) out = n + ((h >> 8) % 5) - 2
  return Math.max(0, out)
}

/** Stored like formatPercent input: 3.25 means 3.25%. */
export function maskPercent(value: number, salt: string, key = ''): number {
  if (!Number.isFinite(value)) return value
  const sign = value < 0 ? -1 : 1
  const abs = Math.abs(value)
  if (abs === 0) return 0
  const h = fnv1a(`${salt}:pct:${key}:${abs}`)
  const scale = 0.93 + ((h % 1401) / 1401) * 0.14
  let out = abs * scale
  out = Math.round(out * 1000) / 1000
  // APR-like (small) vs allocation / share (0–100)
  if (abs <= 35) out = Math.min(40, Math.max(0.01, out))
  else out = Math.min(100, Math.max(0.01, out))
  return out * sign
}

/** Generic numeric (metadata, ratios): modest jitter, preserve integer if input integer. */
export function maskScalar(value: number, salt: string, key = ''): number {
  if (!Number.isFinite(value)) return value
  const sign = value < 0 ? -1 : 1
  const abs = Math.abs(value)
  if (abs === 0) return 0
  const h = fnv1a(`${salt}:sc:${key}:${abs}`)
  const scale = 0.93 + ((h % 1401) / 1401) * 0.14
  let out = abs * scale
  if (Number.isInteger(value)) return Math.max(0, Math.round(out)) * sign
  if (abs >= 1000) out = Math.round(out / 10) * 10
  else if (abs >= 100) out = Math.round(out * 10) / 10
  else out = Math.round(out * 1000) / 1000
  return out * sign
}

/** Backward-compatible name: routes by inferred kind. */
export function maskNumeric(value: number, salt: string, key = ''): number {
  const kind = inferMaskKind(value, key)
  switch (kind) {
    case 'money':
      return maskMoney(value, salt, key)
    case 'count':
      return maskCount(value, salt, key)
    case 'percent':
      return maskPercent(value, salt, key)
    default:
      return maskScalar(value, salt, key)
  }
}

const ISO_DATE_LIKE =
  /^\d{4}-\d{2}-\d{2}(T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$/

function maskDateString(s: string, salt: string): string {
  const d = new Date(s)
  if (isNaN(d.getTime())) return maskLabel(s, salt)
  const h = fnv1a(`${salt}:date:${s}`)
  const shiftDays = (h % 601) - 300
  const copy = new Date(d.getTime())
  copy.setUTCDate(copy.getUTCDate() + shiftDays)
  if (s.includes('T')) return copy.toISOString().replace(/\.\d{3}Z$/, 'Z')
  return copy.toISOString().slice(0, 10)
}

function maskDigits4(h: number): string {
  return String(1000 + (h % 9000)).padStart(4, '0')
}

/** Financial / custody / vendor–like labels; deterministic from input + salt. */
function maskFinancialStyleLabel(h: number): string {
  const instA = INSTITUTION_A[h % INSTITUTION_A.length]
  const instB = INSTITUTION_B[(h >>> 7) % INSTITUTION_B.length]
  const product = ACCOUNT_PRODUCTS[(h >>> 14) % ACCOUNT_PRODUCTS.length]
  const tail = maskDigits4(h >>> 21)
  const institution = `${instA} ${instB}`.replace(/\s+/g, ' ').trim()
  const mode = h % 6

  switch (mode) {
    case 0:
      return `${institution} — ${product}`
    case 1:
      return `${institution}, ${product}`
    case 2:
      return `${product} · ${institution}`
    case 3:
      return `${institution} ····${tail}`
    case 4:
      return `Personal ${product.toLowerCase()} · ${institution}`
    default:
      return `${institution} (${product})`
  }
}

function maskCryptoStyleLabel(h: number): string {
  const venue = CRYPTO_VENUE[h % CRYPTO_VENUE.length]
  const tail = maskDigits4(h >>> 11)
  const inst = INSTITUTION_A[(h >>> 7) % INSTITUTION_A.length]
  return h % 2 === 0 ? `${venue} · ${inst} ····${tail}` : `${inst} ${venue.toLowerCase()} ····${tail}`
}

function maskVendorStyleLabel(h: number, seed: string): string {
  const a = INSTITUTION_A[h % INSTITUTION_A.length]
  const sfx = VENDOR_SUFFIX[(h >>> 9) % VENDOR_SUFFIX.length]
  const short = seed.length <= 14
  if (short) return `${a} ${sfx}`
  return `${a} ${sfx} · ref ${maskDigits4(h >>> 16)}`
}

function looksLikePersonName(s: string): boolean {
  return /^[A-Z][a-z]+ [A-Z][a-z]+(-[A-Z][a-z]+)?$/.test(s.trim())
}

/** Human-style stand-in for account / custody / person / vendor names. */
export function maskLabel(input: string, salt: string): string {
  const t = input.trim()
  if (!t) return input
  if (/^ent_[a-f0-9-]+$/i.test(t)) {
    const h = fnv1a(salt + t)
    return `ent_${h.toString(16).padStart(12, '0').slice(0, 12)}`
  }
  const h = fnv1a(t + salt)
  const lower = t.toLowerCase()

  if (
    /\b(btc|eth|crypto|coin|wallet|custody|ledger|staking|defi|exchange|on-?chain)\b/i.test(t) ||
    /0x[a-f0-9]{8}/i.test(t)
  ) {
    return maskCryptoStyleLabel(h)
  }

  if (looksLikePersonName(t)) {
    const g = GIVEN_STANDIN[h % GIVEN_STANDIN.length]
    const f = FAMILY_STANDIN[(h >>> 8) % FAMILY_STANDIN.length]
    return `${g} ${f}`
  }

  if (
    /\b(ira|401k|broker|bank|savings|checking|mortgage|loan|credit|custody|deposit)\b/i.test(lower) ||
    /\b(acct|account)\b/i.test(lower) ||
    t.length >= 28
  ) {
    return maskFinancialStyleLabel(h)
  }

  if (/\b(llc|inc|ltd|corp|gmbh|s\.l\.|s\.a\.)\b/i.test(t)) {
    return `${INSTITUTION_A[h % INSTITUTION_A.length]} ${VENDOR_SUFFIX[(h >>> 10) % VENDOR_SUFFIX.length]}`
  }

  if (t.split(/\s+/).length >= 3 || t.length >= 18) {
    return maskFinancialStyleLabel(h)
  }

  return maskVendorStyleLabel(h, t)
}

/**
 * Obfuscate `entity_type` / `event_type` slugs (snake_case) without bank-style masking.
 * Do not pass humanized strings like "Financial Account" into {@link maskLabel} — they match
 * financial heuristics and render fake institution names.
 */
export function maskTaxonomyLabel(slug: string, salt: string): string {
  const t = slug.trim()
  if (!t) return '—'
  const h = fnv1a(t + salt)
  const parts = splitKeySegments(t)
  const initials =
    parts.length > 0
      ? parts
          .map(p => p.charAt(0).toUpperCase())
          .join('')
          .slice(0, 5)
      : 'T'
  const id = (h >>> 0).toString(16).slice(0, 5)
  return `${initials}···${id}`
}

/** Registry ids, file names, long blobs. */
export function maskFreeform(text: string, salt: string): string {
  const t = text.trim()
  if (!t) return t
  if (t.length <= 2) return '••'
  if (ISO_DATE_LIKE.test(t)) return maskDateString(t, salt)
  if (/^-?\d+(\.\d+)?$/.test(t)) {
    const n = parseFloat(t)
    return String(maskNumeric(Number.isFinite(n) ? n : 0, salt, `s:${t}`))
  }
  if (/^-?\d+(\.\d+)?\s*%$/.test(t)) {
    const n = parseFloat(t)
    const p = maskPercent(Number.isFinite(n) ? n : 0, salt, 'strpct')
    return `${p}%`
  }
  if (t.length > 48) {
    const h = fnv1a(t + salt).toString(16).slice(0, 10)
    return `doc_${h}…`
  }
  if (/^custody_[a-z0-9_]+$/i.test(t)) {
    const h = fnv1a(t + salt).toString(16).slice(0, 8)
    return `custody_${h}`
  }
  return maskLabel(t, salt)
}

export function maskDeepValue(val: unknown, salt: string, keyPath = ''): unknown {
  if (val === null || val === undefined) return val
  if (typeof val === 'number' && Number.isFinite(val)) {
    const leaf = keyPath.split('.').pop() ?? ''
    const kind = inferMaskKindFromKey(leaf)
    switch (kind) {
      case 'money':
        return maskMoney(val, salt, keyPath)
      case 'count':
        return maskCount(val, salt, keyPath)
      case 'percent':
        return maskPercent(val, salt, keyPath)
      default:
        return maskScalar(val, salt, keyPath)
    }
  }
  if (typeof val === 'string') {
    const ts = val.trim()
    if (ISO_DATE_LIKE.test(ts)) return maskDateString(ts, salt)
    if (/^-?\d+(\.\d+)?$/.test(ts)) {
      const n = parseFloat(ts)
      const leaf = keyPath.split('.').pop() ?? ''
      const kind = inferMaskKindFromKey(leaf)
      const masked =
        kind === 'money'
          ? maskMoney(n, salt, keyPath)
          : kind === 'count'
            ? maskCount(n, salt, keyPath)
            : kind === 'percent'
              ? maskPercent(n, salt, keyPath)
              : maskScalar(n, salt, keyPath)
      return String(masked)
    }
    return maskFreeform(val, salt)
  }
  if (Array.isArray(val)) return val.map((x, i) => maskDeepValue(x, salt, `${keyPath}[${i}]`))
  if (typeof val === 'object') {
    const o = val as Record<string, unknown>
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(o)) {
      const path = keyPath ? `${keyPath}.${k}` : k
      out[k] = maskDeepValue(v, salt, path)
    }
    return out
  }
  return val
}

export function newMaskSalt(): string {
  if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
    const a = new Uint8Array(8)
    crypto.getRandomValues(a)
    return Array.from(a, b => b.toString(16).padStart(2, '0')).join('')
  }
  return `${Date.now().toString(16)}${Math.random().toString(16).slice(2, 10)}`
}
