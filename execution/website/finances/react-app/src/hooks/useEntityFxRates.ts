import { useMemo } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { useFxRate } from '@/context/FxRateContext'
import { fetchFrankfurterEurUsdForDate } from '@/lib/frankfurterClient'
import { collectUniqueFxDates, getEntityFxAsOfDate } from '@/lib/entityFxDate'
import type { UsdPerEurResolver } from '@/lib/aggregations'
import type { Entity } from '@/types/neotoma'

export interface UseEntityFxRatesOptions {
  /**
   * Override the fallback USD-per-EUR used when an entity has no per-entity FX date.
   * When omitted, the live latest ECB rate from {@link useFxRate} is used.
   * Filing pages should pass the year-end ECB rate so display matches workbook export.
   */
  fallbackUsdPerEur?: number
}

/**
 * Loads Frankfurter ECB USD-per-EUR for each unique {@link getEntityFxAsOfDate} in the batch.
 * Entities without a date use the provided `fallbackUsdPerEur` or {@link useFxRate} latest.
 */
export function useEntityFxRates(
  entities: Entity[] | undefined,
  options?: UseEntityFxRatesOptions,
): {
  resolveUsdPerEur: UsdPerEurResolver
  latestUsdPerEur: number
  datesPending: boolean
} {
  const { usdPerEur: latest } = useFxRate()
  const fallback = options?.fallbackUsdPerEur ?? latest
  const dates = useMemo(() => collectUniqueFxDates(entities), [entities])

  const results = useQueries({
    queries: dates.map((date) => ({
      queryKey: ['frankfurter', 'historical', date, 'EUR', 'USD'] as const,
      queryFn: () => fetchFrankfurterEurUsdForDate(date),
      staleTime: Number.POSITIVE_INFINITY,
      gcTime: 7 * 24 * 60 * 60 * 1000,
      retry: 1,
    })),
  })

  const ratesByDate = useMemo(() => {
    const m: Record<string, number> = {}
    dates.forEach((date, i) => {
      const q = results[i]
      const v = q?.data?.usdPerEur
      if (typeof v === 'number' && Number.isFinite(v) && v > 0) m[date] = v
    })
    return m
  }, [dates, results])

  const datesPending = results.some((r) => r.isPending)

  const resolveUsdPerEur = useMemo<UsdPerEurResolver>(() => {
    return (entity: Entity) => {
      const iso = getEntityFxAsOfDate(entity)
      if (iso && ratesByDate[iso] != null) return ratesByDate[iso]!
      return fallback
    }
  }, [ratesByDate, fallback])

  return { resolveUsdPerEur, latestUsdPerEur: fallback, datesPending }
}

/**
 * Fetch the ECB USD-per-EUR for a tax year's Dec 31 so filing pages and workbook export
 * use the same fallback rate for entities without an explicit FX date.
 */
export function useFilingYearEndFxRate(filingYear: number): number | undefined {
  const isoDate = `${filingYear}-12-31`
  const q = useQuery({
    queryKey: ['frankfurter', 'historical', isoDate, 'EUR', 'USD'] as const,
    queryFn: () => fetchFrankfurterEurUsdForDate(isoDate),
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: 7 * 24 * 60 * 60 * 1000,
    retry: 1,
  })
  return q.data?.usdPerEur
}
