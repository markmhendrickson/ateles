/**
 * ECB reference rates via Frankfurter (https://www.frankfurter.app/).
 * Dev: proxied through Vite `/frankfurter-api` to avoid CORS issues.
 * Prod: direct `https://api.frankfurter.app` (Frankfurter sends Access-Control-Allow-Origin: *).
 * Override with `VITE_FRANKFURTER_BASE` (no trailing slash), e.g. your own reverse proxy.
 *
 * **Caching:** {@link fetchFrankfurterEurUsdForDate} uses localStorage + in-flight dedupe
 * ({@link frankfurterRateCache}); historical dates are immutable. {@link fetchFrankfurterLatestEurUsd}
 * uses a 1h localStorage hint so reloads avoid an immediate network call.
 */

import {
  getCachedHistoricalRate,
  getCachedLatestRate,
  setCachedHistoricalRate,
  setCachedLatestRate,
} from '@/lib/frankfurterRateCache'

function frankfurterOrigin(): string {
  const env = (import.meta.env.VITE_FRANKFURTER_BASE as string | undefined)?.replace(/\/$/, '')
  if (env) return env
  if (import.meta.env.DEV) return '/frankfurter-api'
  return 'https://api.frankfurter.app'
}

export interface FrankfurterLatestEurUsd {
  /** USD per 1 EUR (e.g. 1.175). */
  usdPerEur: number
  /** ECB fixing date from API (YYYY-MM-DD). */
  date: string
}

async function fetchFrankfurterEurUsd(urlPath: string): Promise<FrankfurterLatestEurUsd> {
  const base = frankfurterOrigin().replace(/\/$/, '')
  const qs = new URLSearchParams({ from: 'EUR', to: 'USD' }).toString()
  const url = `${base}${urlPath}?${qs}`

  const res = await fetch(url, {
    headers: { Accept: 'application/json' },
  })
  if (!res.ok) {
    throw new Error(`Frankfurter HTTP ${res.status}`)
  }
  const j = (await res.json()) as { date?: string; rates?: { USD?: number } }
  const usd = j?.rates?.USD
  if (typeof usd !== 'number' || !Number.isFinite(usd)) {
    throw new Error('Frankfurter: missing or invalid rates.USD')
  }
  return { usdPerEur: usd, date: String(j.date ?? '') }
}

const historicalInFlight = new Map<string, Promise<FrankfurterLatestEurUsd>>()

export async function fetchFrankfurterLatestEurUsd(): Promise<FrankfurterLatestEurUsd> {
  const cached = getCachedLatestRate()
  if (cached) return cached

  const live = await fetchFrankfurterEurUsd('/latest')
  setCachedLatestRate(live)
  return live
}

/** ECB fixing for a calendar day (YYYY-MM-DD). Frankfurter uses the ECB series (weekends roll to prior business day). */
export async function fetchFrankfurterEurUsdForDate(isoDate: string): Promise<FrankfurterLatestEurUsd> {
  const d = isoDate.trim()
  if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) {
    throw new Error(`Frankfurter: invalid date ${isoDate}`)
  }

  const cached = getCachedHistoricalRate(d)
  if (cached) return cached

  let inflight = historicalInFlight.get(d)
  if (!inflight) {
    inflight = fetchFrankfurterEurUsd(`/${d}`).then((r) => {
      setCachedHistoricalRate(d, r)
      return r
    })
    historicalInFlight.set(d, inflight)
    inflight.finally(() => {
      historicalInFlight.delete(d)
    })
  }
  return inflight
}
