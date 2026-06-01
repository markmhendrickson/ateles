import type { Entity } from '@/types/neotoma'
import { DEFAULT_FILING_YEAR } from '@/constants/filingYears'
import { coalesceSnapshot, normalizeFilingTags, snapshotField } from '@/lib/formatters'
import { deriveFinancialAccountInstitution, deriveFinancialAccountName } from '@/lib/humanize'

const Q4_OVERLAY_KEYS = [
  'q4_average_balance_eur',
  'q4_average_balance_original',
  'q4_average_balance_currency',
  'q4_vs_year_end_delta_eur',
  'q4_vs_year_end_delta_pct',
  'q4_reconciliation_status',
] as const

const normalizeFilingTagList = normalizeFilingTags

function normLabel(s: string | undefined | null): string {
  return String(s ?? '')
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
    .replace(/\s+/g, ' ')
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function entityTaxYear(e: Entity): number | null {
  const tcx = snapshotField<number>(e.snapshot, 'tax_year_context')
  if (tcx != null && Number.isFinite(tcx)) return tcx
  const ty = snapshotField<number | string>(e.snapshot, 'tax_year')
  if (ty == null || ty === '') return null
  const n = Number(ty)
  return Number.isFinite(n) ? n : null
}

/** Same year window as `equityWorkbookMatchesFilingYear` in `filingAssets.ts` (avoid import cycle). */
function equityWorkbookMatchesFilingYear(entity: Entity, filingYear: number): boolean {
  const tags = normalizeFilingTagList(entity.snapshot)
  if (!tags.includes('equity')) return false
  const y = snapshotField<number>(entity.snapshot, 'tax_year_context')
  const tyRaw = snapshotField<number | string>(entity.snapshot, 'tax_year')
  const tyNum =
    tyRaw != null && tyRaw !== '' && Number.isFinite(Number(tyRaw)) ? Number(tyRaw) : null

  if (y != null) {
    if (y === filingYear || y === filingYear - 1) return true
  }
  if (tyNum != null) {
    if (tyNum === filingYear || tyNum === filingYear - 1) return true
  }
  if (y == null && tyNum == null) return filingYear === DEFAULT_FILING_YEAR
  return false
}

function recipientEligibleForQ4Overlay(entity: Entity, filingYear: number): boolean {
  const rTags = normalizeFilingTagList(entity.snapshot)
  if (rTags.includes('720')) {
    return entityTaxYear(entity) === filingYear
  }
  if (rTags.includes('equity')) {
    return equityWorkbookMatchesFilingYear(entity, filingYear)
  }
  return false
}

function donorMatchesFilingYear(donor: Entity, filingYear: number): boolean {
  const dTags = normalizeFilingTagList(donor.snapshot)
  if (dTags.includes('720')) return entityTaxYear(donor) === filingYear
  if (dTags.includes('equity')) return equityWorkbookMatchesFilingYear(donor, filingYear)
  return false
}

/** True for rows created by `import-modelo-workbook-neotoma.ts` (`modelo_workbook_*` registry_id). */
export function isModeloWorkbookImportAccount(e: Entity): boolean {
  if (e.entity_type !== 'financial_account') return false
  const rid = String(snapshotField<string>(e.snapshot, 'registry_id') ?? '')
  if (rid.startsWith('modelo_workbook_')) return true
  return snapshotField<string>(e.snapshot, 'observation_kind') === 'modelo_workbook_import'
}

function donorCarriesQ4Overlay(donor: Entity): boolean {
  const s = donor.snapshot
  if (!s) return false
  return s.q4_average_balance_eur != null
}

function recipientHaystack(e: Entity): string {
  const parts = [
    coalesceSnapshot<string>(e.snapshot, ['account_name']),
    deriveFinancialAccountName(e),
    coalesceSnapshot<string>(e.snapshot, ['modelo_bien', 'modelo_bien_hint']),
    deriveFinancialAccountInstitution(e),
    e.canonical_name,
  ]
  return normLabel(parts.filter(Boolean).join(' '))
}

/**
 * Match short workbook `Bien` labels (e.g. "Individual", "SEP") to canonical Neotoma account names
 * ("Schwab Individual VTSAX") so Q4 fields from `modelo_workbook_*` imports show on filing-linked rows.
 */
function matchScore(recipient: Entity, donorBienLabel: string): number {
  const hay = recipientHaystack(recipient)
  const bien = normLabel(donorBienLabel)
  if (!hay || !bien) return 0

  const hint = normLabel(coalesceSnapshot<string>(recipient.snapshot, ['modelo_bien_hint', 'modelo_bien']))
  if (hint && hint === bien) return 0.99

  if (hay === bien) return 1
  if (hay.includes(bien)) return 0.95

  const words = bien.split(' ').filter((w) => w.length > 1)
  if (words.length === 0) return 0

  if (words.length === 1 && words[0].length <= 5) {
    const w = words[0]
    const re = new RegExp(`(^|\\s)${escapeRegExp(w)}(\\s|$)`)
    return re.test(hay) ? 0.9 : 0
  }

  const hits = words.filter((w) => hay.includes(w)).length
  const ratio = hits / words.length
  if (ratio >= 1) return 0.88
  if (ratio >= 0.5) return 0.55 + 0.3 * ratio
  return 0
}

const MIN_MATCH_SCORE = 0.65

function pickQ4FieldsFromDonor(donor: Entity): Record<string, unknown> {
  const s = donor.snapshot ?? {}
  const out: Record<string, unknown> = {}
  for (const k of Q4_OVERLAY_KEYS) {
    if (k in s && s[k as keyof typeof s] != null) {
      out[k] = s[k as keyof typeof s]
    }
  }
  return out
}

/**
 * If this account has no Q4 snapshot fields, copy them from the best-matching
 * `modelo_workbook_*` row for the same tax year (Modelo 720 brokerage lines and
 * workbook `equity` rows).
 */
export function applyModeloWorkbookQ4Overlay(
  entity: Entity,
  allAccounts: Entity[],
  filingYear: number,
): Entity {
  if (entity.entity_type !== 'financial_account') return entity

  if (snapshotField<number>(entity.snapshot, 'q4_average_balance_eur') != null) return entity
  // Still try donors when the workbook row only recorded `missing_q4_average`.
  const st = snapshotField<string>(entity.snapshot, 'q4_reconciliation_status')
  if (st != null && st !== 'missing_q4_average') return entity

  if (!recipientEligibleForQ4Overlay(entity, filingYear)) return entity

  let best: { donor: Entity; score: number } | null = null

  for (const d of allAccounts) {
    if (d.entity_id === entity.entity_id) continue
    if (!isModeloWorkbookImportAccount(d)) continue
    if (!donorCarriesQ4Overlay(d)) continue
    if (!donorMatchesFilingYear(d, filingYear)) continue

    const bien = String(snapshotField<string>(d.snapshot, 'modelo_workbook_bien_label') ?? '').trim()
    if (!bien) continue

    const score = matchScore(entity, bien)
    if (score < MIN_MATCH_SCORE) continue
    if (!best || score > best.score) best = { donor: d, score }
  }

  if (!best) return entity

  const patch = pickQ4FieldsFromDonor(best.donor)
  if (Object.keys(patch).length === 0) return entity

  return {
    ...entity,
    snapshot: {
      ...entity.snapshot,
      ...patch,
      q4_overlay_source_entity_id: best.donor.entity_id,
      q4_overlay_match_score: best.score,
    },
  }
}

export function applyModeloWorkbookQ4OverlayToMany(
  entities: Entity[],
  allAccounts: Entity[],
  filingYear: number,
): Entity[] {
  return entities.map((e) => applyModeloWorkbookQ4Overlay(e, allAccounts, filingYear))
}
