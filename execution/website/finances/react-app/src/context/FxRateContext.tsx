import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchFrankfurterLatestEurUsd } from '@/lib/frankfurterClient'
import { FALLBACK_USD_PER_EUR } from '@/lib/aggregations'

export interface FxRateState {
  /** USD per one EUR (Frankfurter latest ECB fixing when available). */
  usdPerEur: number
  /** Frankfurter `date` field when live; empty if using fallback. */
  rateDate: string | null
  isLoading: boolean
  isError: boolean
  error: Error | null
}

const FxRateContext = createContext<FxRateState | null>(null)

export function FxRateProvider({ children }: { children: ReactNode }) {
  const q = useQuery({
    queryKey: ['frankfurter', 'latest', 'EUR', 'USD'],
    queryFn: fetchFrankfurterLatestEurUsd,
    /** Aligns with 1h localStorage seed in `fetchFrankfurterLatestEurUsd` (avoids hammering on reload). */
    staleTime: 60 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    retry: 2,
  })

  const value = useMemo<FxRateState>(() => {
    const live = q.data
    const useLive = live && Number.isFinite(live.usdPerEur) && live.usdPerEur > 0
    return {
      usdPerEur: useLive ? live.usdPerEur : FALLBACK_USD_PER_EUR,
      rateDate: useLive ? live.date || null : null,
      isLoading: q.isLoading,
      isError: q.isError,
      error: q.error instanceof Error ? q.error : q.error ? new Error(String(q.error)) : null,
    }
  }, [q.data, q.isLoading, q.isError, q.error])

  return <FxRateContext.Provider value={value}>{children}</FxRateContext.Provider>
}

export function useFxRate(): FxRateState {
  const ctx = useContext(FxRateContext)
  if (!ctx) {
    throw new Error('useFxRate must be used within FxRateProvider')
  }
  return ctx
}
