import { useEntities } from '@shared/hooks/useEntities'
import { FILING_ACCOUNT_QUERY_LIMIT } from '@/constants/filingYears'
import type { QueryParams } from '@/types/neotoma'

export { useEntities }

export function useEntitiesByType(entityType: string, extra?: Partial<QueryParams>) {
  const defaultLimit = entityType === 'financial_account' ? FILING_ACCOUNT_QUERY_LIMIT : 500
  return useEntities({
    entity_type: entityType,
    include_snapshots: true,
    limit: defaultLimit,
    ...extra,
  })
}
