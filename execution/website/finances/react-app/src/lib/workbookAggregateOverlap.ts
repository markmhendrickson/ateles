import type { Entity } from '@/types/neotoma'
import { snapshotField } from '@/lib/formatters'
import { isModeloWorkbookImportAccount } from '@/lib/modeloWorkbookQ4Overlay'

/**
 * Modelo workbook imports use `registry_id` like `modelo_workbook_2025_coinbase` (or `_kraken`) for a platform
 * total and `modelo_workbook_2025_coinbase_btc` for line items. Keeping both double-counts in UI
 * and exports. Drop the parent row when any strictly longer child id shares the same prefix.
 */
export function excludeRedundantWorkbookParentAccounts(
  entities: Entity[],
  registryLookupPool: Entity[],
): Entity[] {
  const rids = new Set(
    registryLookupPool
      .map((e) => String(snapshotField<string>(e.snapshot, 'registry_id') ?? '').toLowerCase())
      .filter(Boolean),
  )

  return entities.filter((e) => {
    if (e.entity_type !== 'financial_account') return true
    if (!isModeloWorkbookImportAccount(e)) return true
    const rid = String(snapshotField<string>(e.snapshot, 'registry_id') ?? '').toLowerCase()
    const m = rid.match(/^modelo_workbook_(\d+)_(.+)$/)
    if (!m) return true
    const year = m[1]
    const slug = m[2]
    const childPrefix = `modelo_workbook_${year}_${slug}_`
    for (const other of rids) {
      if (other === rid) continue
      if (other.startsWith(childPrefix) && other.length > rid.length) return false
    }
    return true
  })
}
