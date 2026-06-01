const eurFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'EUR',
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
})

const eurDetailFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'EUR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

const usdFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
})

const numberFormatter = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
})

const percentFormatter = new Intl.NumberFormat('en-US', {
  style: 'percent',
  minimumFractionDigits: 1,
  maximumFractionDigits: 2,
})

export function formatEur(value: number | undefined | null, detailed = false): string {
  if (value == null) return '—'
  return detailed ? eurDetailFormatter.format(value) : eurFormatter.format(value)
}

export function formatUsd(value: number | undefined | null): string {
  if (value == null) return '—'
  return usdFormatter.format(value)
}

export function formatCurrency(value: number | undefined | null, currency?: string): string {
  if (value == null) return '—'
  if (currency === 'USD') return formatUsd(value)
  return formatEur(value)
}

export function formatNumber(value: number | undefined | null): string {
  if (value == null) return '—'
  return numberFormatter.format(value)
}

export function formatPercent(value: number | undefined | null): string {
  if (value == null) return '—'
  return percentFormatter.format(value / 100)
}

export function formatDate(value: string | undefined | null): string {
  if (!value) return '—'
  const d = new Date(value)
  if (isNaN(d.getTime())) return value
  return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })
}

export function formatDateShort(value: string | undefined | null): string {
  if (!value) return '—'
  const d = new Date(value)
  if (isNaN(d.getTime())) return value
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

export function formatRelativeTime(value: string | undefined | null): string {
  if (!value) return '—'
  const d = new Date(value)
  if (isNaN(d.getTime())) return value
  const now = Date.now()
  const diffMs = now - d.getTime()
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24))

  if (diffDays === 0) return 'today'
  if (diffDays === 1) return 'yesterday'
  if (diffDays < 30) return `${diffDays}d ago`
  if (diffDays < 365) return `${Math.floor(diffDays / 30)}mo ago`
  return `${Math.floor(diffDays / 365)}y ago`
}

export function snapshotField<T>(snapshot: Record<string, unknown> | null | undefined, key: string): T | undefined {
  if (!snapshot) return undefined
  if (key in snapshot) return snapshot[key] as T | undefined
  return undefined
}

/** Read a dotted path from a snapshot object, e.g. `meta.as_of`. */
export function snapshotPathValue(
  snapshot: Record<string, unknown> | null | undefined,
  path: string,
): unknown {
  if (!snapshot || !path.trim()) return undefined
  const parts = path.split('.').map(p => p.trim()).filter(Boolean)
  let cur: unknown = snapshot
  for (const p of parts) {
    if (cur == null || typeof cur !== 'object') return undefined
    cur = (cur as Record<string, unknown>)[p]
  }
  return cur
}

/** First non-empty snapshot value among keys (by key order). */
export function coalesceSnapshot<T>(snapshot: Record<string, unknown> | null | undefined, keys: string[]): T | undefined {
  if (!snapshot) return undefined
  for (const k of keys) {
    if (!(k in snapshot)) continue
    const v = snapshot[k] as T | undefined
    if (v == null) continue
    if (typeof v === 'string' && v.trim() === '') continue
    return v
  }
  return undefined
}

/**
 * Normalize filing_tags from either string[] or comma-separated string to string[].
 * Handles the schema inconsistency where some entities store tags as a string.
 */
export function normalizeFilingTags(snapshot: Record<string, unknown> | null | undefined): string[] {
  const raw = coalesceSnapshot<string[] | string>(snapshot, ['filing_tags'])
  if (raw == null) return []
  if (Array.isArray(raw)) return raw.map((t) => String(t).trim()).filter(Boolean)
  const s = String(raw).trim()
  if (!s) return []
  return s.split(',').map((t) => t.trim()).filter(Boolean)
}
