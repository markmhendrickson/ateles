/**
 * Tax years shown as tabs under each Modelo subpage (newest first).
 * TODO: Derive from stored tax_filing entities via API instead of hardcoding.
 */
export const FILING_YEARS = [2026, 2025, 2024] as const

export type FilingYear = (typeof FILING_YEARS)[number]

export const DEFAULT_FILING_YEAR: FilingYear = 2025

/**
 * Shared query limit for all filing / Modelo / export account fetches so every surface
 * operates on the same entity set. CLI export paginates independently.
 */
export const FILING_ACCOUNT_QUERY_LIMIT = 8000

export function isFilingYear(s: string | undefined): s is `${FilingYear}` {
  if (!s || !/^\d{4}$/.test(s)) return false
  return (FILING_YEARS as readonly number[]).includes(Number(s))
}
