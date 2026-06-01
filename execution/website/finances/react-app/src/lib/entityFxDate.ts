import type { Entity } from '@/types/neotoma'
import { snapshotField } from '@/lib/formatters'

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

/**
 * Normalize snapshot date strings to YYYY-MM-DD for Frankfurter historical API.
 */
export function normalizeFxIsoDate(raw: unknown): string | null {
  if (raw == null) return null
  const s = String(raw).trim()
  if (!s) return null
  if (ISO_DATE.test(s)) return s
  const t = Date.parse(s)
  if (Number.isNaN(t)) return null
  return new Date(t).toISOString().slice(0, 10)
}

/**
 * Best-effort valuation / statement as-of for FX.
 * Prefers canonical `balance_date` when available; falls back to legacy field names.
 */
export function getEntityFxAsOfDate(entity: Entity): string | null {
  const snap = entity.snapshot
  if (!snap) return null

  const canonical = normalizeFxIsoDate(snapshotField<string>(snap, 'balance_date'))
  if (canonical) return canonical

  const keys = [
    'last_statement_date',
    'statement_as_of_date',
    'statement_period_end',
    'assets_sheet_as_of_date',
  ] as const

  for (const k of keys) {
    const v = (snap as Record<string, unknown>)[k]
    const iso = normalizeFxIsoDate(v)
    if (iso) return iso
  }

  const fromField = normalizeFxIsoDate(snapshotField<string>(snap, 'as_of_date'))
  if (fromField) return fromField

  // Many account imports store valuation date on account_value_as_of_date.
  const fromAccountValue = normalizeFxIsoDate(snapshotField<string>(snap, 'account_value_as_of_date'))
  if (fromAccountValue) return fromAccountValue

  // Modelo workbook imports are year-end snapshots; infer Dec 31 from tax year context.
  if (snapshotField<string>(snap, 'observation_kind') === 'modelo_workbook_import') {
    const rawTaxYear = snapshotField<number | string>(snap, 'tax_year_context') ?? snapshotField<number | string>(snap, 'tax_year')
    if (rawTaxYear != null && rawTaxYear !== '') {
      const n = Number(rawTaxYear)
      if (Number.isFinite(n)) {
        const year = Math.trunc(n)
        if (year >= 1900 && year <= 9999) {
          return `${String(year).padStart(4, '0')}-12-31`
        }
      }
    }
  }

  return null
}

/** Unique sorted ISO dates for a batch of entities (for batched Frankfurter queries). */
export function collectUniqueFxDates(entities: Entity[] | undefined): string[] {
  if (!entities?.length) return []
  const set = new Set<string>()
  for (const e of entities) {
    const d = getEntityFxAsOfDate(e)
    if (d && ISO_DATE.test(d)) set.add(d)
  }
  return Array.from(set).sort()
}
