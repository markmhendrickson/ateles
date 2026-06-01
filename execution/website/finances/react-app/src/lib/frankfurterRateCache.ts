/**
 * Persistent + in-memory cache for Frankfurter EUR→USD rates so repeated dates
 * (and full page reloads) do not re-hit the API.
 *
 * Historical ECB fixings for a calendar request date are immutable; we cache them
 * indefinitely in localStorage (with in-session memory for speed).
 */

import type { FrankfurterLatestEurUsd } from '@/lib/frankfurterClient'

const HISTORICAL_LS_PREFIX = 'finances-frankfurter:h1:'
const LATEST_LS_KEY = 'finances-frankfurter:latest:v1'
const LATEST_MAX_AGE_MS = 60 * 60 * 1000

const historicalMemory = new Map<string, FrankfurterLatestEurUsd>()

function canUseLocalStorage(): boolean {
  try {
    return typeof localStorage !== 'undefined'
  } catch {
    return false
  }
}

export function getCachedHistoricalRate(isoDate: string): FrankfurterLatestEurUsd | null {
  const key = isoDate.trim()
  if (!/^\d{4}-\d{2}-\d{2}$/.test(key)) return null

  const mem = historicalMemory.get(key)
  if (mem) return mem

  if (!canUseLocalStorage()) return null
  try {
    const raw = localStorage.getItem(HISTORICAL_LS_PREFIX + key)
    if (!raw) return null
    const p = JSON.parse(raw) as { usdPerEur?: number; date?: string }
    if (typeof p.usdPerEur !== 'number' || !Number.isFinite(p.usdPerEur) || p.usdPerEur <= 0) {
      return null
    }
    const v: FrankfurterLatestEurUsd = { usdPerEur: p.usdPerEur, date: String(p.date ?? key) }
    historicalMemory.set(key, v)
    return v
  } catch {
    return null
  }
}

export function setCachedHistoricalRate(isoDate: string, value: FrankfurterLatestEurUsd): void {
  const key = isoDate.trim()
  historicalMemory.set(key, value)
  if (!canUseLocalStorage()) return
  try {
    localStorage.setItem(
      HISTORICAL_LS_PREFIX + key,
      JSON.stringify({ usdPerEur: value.usdPerEur, date: value.date }),
    )
  } catch {
    /* quota or private mode */
  }
}

type LatestStored = { usdPerEur: number; date: string; storedAt: number }

export function getCachedLatestRate(): FrankfurterLatestEurUsd | null {
  if (!canUseLocalStorage()) return null
  try {
    const raw = localStorage.getItem(LATEST_LS_KEY)
    if (!raw) return null
    const p = JSON.parse(raw) as LatestStored
    if (
      typeof p.usdPerEur !== 'number' ||
      !Number.isFinite(p.usdPerEur) ||
      p.usdPerEur <= 0 ||
      typeof p.storedAt !== 'number'
    ) {
      return null
    }
    if (Date.now() - p.storedAt > LATEST_MAX_AGE_MS) return null
    return { usdPerEur: p.usdPerEur, date: String(p.date ?? '') }
  } catch {
    return null
  }
}

export function setCachedLatestRate(value: FrankfurterLatestEurUsd): void {
  if (!canUseLocalStorage()) return
  try {
    const payload: LatestStored = {
      usdPerEur: value.usdPerEur,
      date: value.date,
      storedAt: Date.now(),
    }
    localStorage.setItem(LATEST_LS_KEY, JSON.stringify(payload))
  } catch {
    /* quota */
  }
}
