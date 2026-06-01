import { useMemo } from 'react'
import { useQueries } from '@tanstack/react-query'
import { getEntityObservations } from '@/api/entities'
import { financialAccountSnapshotMissingMoney } from '@/lib/aggregations'
import type { Entity, Observation } from '@/types/neotoma'

function observationPayload(obs: Observation): Record<string, unknown> | null {
  const o = obs as unknown as Record<string, unknown>
  const fields = o.fields
  if (fields && typeof fields === 'object' && !Array.isArray(fields)) {
    return fields as Record<string, unknown>
  }
  const data = o.data
  if (data && typeof data === 'object' && !Array.isArray(data)) {
    return data as Record<string, unknown>
  }
  return null
}

function fieldsHaveMoney(f: Record<string, unknown>): boolean {
  return (
    typeof f.account_value === 'number' ||
    typeof f.balance_eur === 'number' ||
    typeof f.balance_value === 'number' ||
    typeof f.ending_balance_usd === 'number' ||
    typeof f.ending_account_value_eur === 'number'
  )
}

/** Prefer an observation for this tax year; else first observation with balance fields (API order: newest first). */
function pickFieldsForFilingYear(observations: Observation[], filingYear: number): Record<string, unknown> | null {
  for (const obs of observations) {
    const f = observationPayload(obs)
    if (!f || !fieldsHaveMoney(f)) continue
    const ty = f.tax_year
    const tyc = f.tax_year_context
    const tyN = ty != null && String(ty).trim() !== '' ? Number(ty) : NaN
    const tycN = tyc != null && String(tyc).trim() !== '' ? Number(tyc) : NaN
    if (tyN === filingYear || tycN === filingYear) return f
  }
  for (const obs of observations) {
    const f = observationPayload(obs)
    if (f && fieldsHaveMoney(f)) return f
  }
  return null
}

/**
 * Merges the latest matching observation `fields` into `snapshot` when the Neotoma API snapshot omits balances
 * (common for `modelo_workbook_import` rows) so filing views match stored observations.
 */
export function useObservationHydratedFinancialAccounts(
  entities: Entity[] | undefined,
  filingYear: number | null | undefined,
  enabled = true,
): { entities: Entity[] | undefined; isLoading: boolean } {
  const targetYear = filingYear != null && Number.isFinite(filingYear) && filingYear > 0 ? filingYear : null

  const ids = useMemo(() => {
    if (!entities?.length || !targetYear || !enabled) return [] as string[]
    return entities.filter((e) => financialAccountSnapshotMissingMoney(e)).map((e) => e.entity_id)
  }, [entities, targetYear, enabled])

  const queries = useQueries({
    queries: ids.map((entityId) => ({
      queryKey: ['observations', 'hydrate-financial-account', entityId, targetYear],
      queryFn: async () => {
        const list = await getEntityObservations(entityId, { limit: 80 })
        const fields = pickFieldsForFilingYear(list, targetYear!)
        return { entityId, fields }
      },
      enabled: !!targetYear && ids.length > 0,
      staleTime: 120_000,
    })),
  })

  const isLoading = queries.some((q) => q.isPending)

  const dataStamp = queries.map((q) => `${q.fetchStatus}:${q.dataUpdatedAt}`).join('|')
  const idList = ids.join('|')

  const merged = useMemo(() => {
    if (!entities) return undefined
    if (!targetYear || ids.length === 0) return entities

    const byId = new Map<string, Record<string, unknown>>()
    for (let i = 0; i < ids.length; i++) {
      const payload = queries[i]?.data
      const f = payload?.fields
      if (f && Object.keys(f).length > 0) {
        byId.set(payload.entityId, f)
      }
    }

    return entities.map((e) => {
      const fields = byId.get(e.entity_id)
      if (!fields || !e.snapshot) return e
      return {
        ...e,
        snapshot: { ...fields, ...e.snapshot },
      }
    })
  }, [entities, targetYear, dataStamp, idList])

  return { entities: merged, isLoading }
}
