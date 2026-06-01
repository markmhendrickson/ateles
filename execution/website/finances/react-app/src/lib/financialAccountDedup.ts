import type { Entity } from '@/types/neotoma'
import { snapshotField } from '@/lib/formatters'
import { excludeRedundantWorkbookParentAccounts } from '@/lib/workbookAggregateOverlap'

/**
 * Deduplicate financial_account entities by registry_id.
 *
 * Server-side entity merge via Neotoma `merge_entities` should prevent duplicates from
 * reaching the frontend. This function is a safety net that picks the most recent entity
 * per registry_id if duplicates somehow still appear.
 */
export function dedupeFinancialAccountsByRegistry(entities: Entity[]): Entity[] {
  const chosen = new Map<string, Entity>()
  for (const entity of entities) {
    const key = String(snapshotField<string>(entity.snapshot, 'registry_id') ?? entity.entity_id)
    const current = chosen.get(key)
    if (!current) {
      chosen.set(key, entity)
      continue
    }
    const tsA = Date.parse(entity.last_observation_at ?? entity.updated_at ?? '') || 0
    const tsB = Date.parse(current.last_observation_at ?? current.updated_at ?? '') || 0
    if (tsA > tsB) {
      chosen.set(key, entity)
    }
  }
  return [...chosen.values()]
}

/** Dedupe by registry_id, then drop Modelo workbook parent rows superseded by per-asset children. */
export function prepareFinancialAccountList(entities: Entity[]): Entity[] {
  const deduped = dedupeFinancialAccountsByRegistry(entities)
  return excludeRedundantWorkbookParentAccounts(deduped, entities)
}
