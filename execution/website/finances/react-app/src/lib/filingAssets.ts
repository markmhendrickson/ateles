import type { Entity, Relationship } from '@/types/neotoma'
import { normalizeFilingTags, snapshotField } from '@/lib/formatters'
import { DEFAULT_FILING_YEAR } from '@/constants/filingYears'
import { applyModeloWorkbookQ4Overlay } from '@/lib/modeloWorkbookQ4Overlay'
import { excludeRedundantWorkbookParentAccounts } from '@/lib/workbookAggregateOverlap'

export function isGoodsEntityType(entityType: string | undefined | null): boolean {
  if (!entityType) return false
  const t = entityType.toLowerCase()
  return t === 'goods' || t === 'good'
}

const FILING_ID_KEYS = [
  'tax_filing_id',
  'filing_entity_id',
  'tax_filing_entity_id',
  'linked_tax_filing_id',
] as const

export function goodsSnapshotReferencesFiling(goods: Entity, filingEntityId: string): boolean {
  const s = goods.snapshot ?? {}
  for (const k of FILING_ID_KEYS) {
    const v = snapshotField(s, k)
    if (v != null && String(v) === filingEntityId) return true
  }
  return false
}

export function goodsSnapshotMatchesFilingScope(goods: Entity, filing: Entity): boolean {
  if (goodsSnapshotReferencesFiling(goods, filing.entity_id)) return false
  const fs = filing.snapshot ?? {}
  const gs = goods.snapshot ?? {}
  const filingYear = snapshotField<number | string>(fs, 'tax_year')
  const goodsYear = snapshotField<number | string>(gs, 'tax_year')
  if (filingYear == null || goodsYear == null) return false
  if (String(filingYear) !== String(goodsYear)) return false
  const filingForm = snapshotField<string>(fs, 'form_code')
  const goodsForm = snapshotField<string>(gs, 'form_code')
  if (!filingForm || !goodsForm) return false
  return String(filingForm) === String(goodsForm)
}

/** Same rule as Modelo 720 / 721 tabs for `tax_year_context`. */
export function matchesTaxYearContext(entity: Entity, filingYear: number): boolean {
  const y = snapshotField<number | string>(entity.snapshot, 'tax_year_context')
  if (y == null) return filingYear === DEFAULT_FILING_YEAR
  return Number(y) === filingYear
}

/** True when `tax_year_context` matches, or snapshot `tax_year` matches the filing year. */
export function accountMatchesFilingTaxYear(entity: Entity, filingYear: number): boolean {
  if (matchesTaxYearContext(entity, filingYear)) return true
  const ty = snapshotField<number | string>(entity.snapshot, 'tax_year')
  if (ty == null) return false
  return Number(ty) === filingYear
}

function filingYearNumber(filing: Entity): number | null {
  const raw = snapshotField<number | string>(filing.snapshot, 'tax_year')
  if (raw == null) return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
}

const normalizeFilingTagList = normalizeFilingTags

const MODELO_SCOPE_TAGS = new Set(['720', '721', 'equity'])

/**
 * Stock / private equity / options workbook lines (`filing_tags` includes `equity`).
 * Aligns with Modelo 720 “Workbook equity” rows: filing year, prior-year anchor, or snapshot `tax_year`.
 */
export function equityWorkbookMatchesFilingYear(entity: Entity, filingYear: number): boolean {
  const tags = normalizeFilingTagList(entity.snapshot)
  if (!tags.includes('equity')) return false
  const yRaw = snapshotField<number | string>(entity.snapshot, 'tax_year_context')
  const yNum = yRaw != null && yRaw !== '' && Number.isFinite(Number(yRaw)) ? Number(yRaw) : null
  const tyRaw = snapshotField<number | string>(entity.snapshot, 'tax_year')
  const tyNum =
    tyRaw != null && tyRaw !== '' && Number.isFinite(Number(tyRaw)) ? Number(tyRaw) : null

  if (yNum != null) {
    if (yNum === filingYear || yNum === filingYear - 1) return true
  }
  if (tyNum != null) {
    if (tyNum === filingYear || tyNum === filingYear - 1) return true
  }
  if (yNum == null && tyNum == null) return filingYear === DEFAULT_FILING_YEAR
  return false
}

/**
 * All accounts that belong to the Modelo / workbook scope for this tax year — not filtered by the
 * filing’s `form_code`, so both 720 and 721 `tax_filing` pages show the same year-wide asset set.
 */
function filterModeloScopedAccountsForYear(accounts: Entity[], filingYear: number): Entity[] {
  return accounts.filter((entity) => {
    const tags = normalizeFilingTagList(entity.snapshot)
    const type = snapshotField<string>(entity.snapshot, 'account_type')
    const custody = type != null && type.toLowerCase().includes('custod')
    const workbook = snapshotField<string>(entity.snapshot, 'modelo_workbook_source_file') != null
    const hasTag = tags.some((t) => MODELO_SCOPE_TAGS.has(t))
    if (!hasTag && !custody && !workbook) return false

    if (accountMatchesFilingTaxYear(entity, filingYear)) return true
    if (tags.includes('equity') && equityWorkbookMatchesFilingYear(entity, filingYear)) return true
    return false
  })
}

function relationshipOtherId(rel: Relationship, filingId: string): string | null {
  if (rel.source_entity_id === filingId) return rel.target_entity_id
  if (rel.target_entity_id === filingId) return rel.source_entity_id
  return null
}

/**
 * Resolve the non-filing end of a relationship.
 * Prefer the `lookup` entity (from full entities query with snapshots) over the
 * relationship-embedded entity, because the relationships endpoint returns
 * related entities without `canonical_name` — causing institution / account-name
 * derivation to fail. Fall back to the embedded entity when it is not in the lookup.
 */
export function resolveRelationshipOtherEntity(
  rel: Relationship,
  filingId: string,
  lookup: Map<string, Entity>,
): Entity | null {
  const oid = relationshipOtherId(rel, filingId)
  if (!oid) return null
  const fromLookup = lookup.get(oid)
  if (fromLookup) return fromLookup
  if (rel.source_entity_id === filingId && rel.target_entity) return rel.target_entity
  if (rel.target_entity_id === filingId && rel.source_entity) return rel.source_entity
  return null
}

function buildEntityLookup(accounts: Entity[], goods: Entity[] | undefined): Map<string, Entity> {
  const m = new Map<string, Entity>()
  for (const e of accounts) m.set(e.entity_id, e)
  for (const g of goods ?? []) m.set(g.entity_id, g)
  return m
}

/**
 * Linked to this filing: include if year matches, both year fields are unset
 * (trust the graph), OR the account is explicitly relationship-linked
 * regardless of year — if the user linked it, it should appear.
 */
function linkedFinancialAccountMatchesFilingYear(_account: Entity, _filingYear: number): boolean {
  return true
}

function linkedFinancialAccountsForYear(
  filing: Entity,
  filingYear: number,
  relationships: Relationship[] | undefined,
  lookup: Map<string, Entity>,
): Entity[] {
  const out: Entity[] = []
  for (const rel of relationships ?? []) {
    const other = resolveRelationshipOtherEntity(rel, filing.entity_id, lookup)
    if (!other || other.entity_type !== 'financial_account') continue
    if (!linkedFinancialAccountMatchesFilingYear(other, filingYear)) continue
    out.push(other)
  }
  return out
}

function linkedFinancialAccountsAnyYear(
  filing: Entity,
  relationships: Relationship[] | undefined,
  lookup: Map<string, Entity>,
): Entity[] {
  const out: Entity[] = []
  for (const rel of relationships ?? []) {
    const other = resolveRelationshipOtherEntity(rel, filing.entity_id, lookup)
    if (!other || other.entity_type !== 'financial_account') continue
    out.push(other)
  }
  return out
}

function appendGoods(
  map: Map<string, Entity>,
  filing: Entity,
  relationships: Relationship[] | undefined,
  queriedGoods: Entity[] | undefined,
  lookup: Map<string, Entity>,
): void {
  for (const rel of relationships ?? []) {
    const other = resolveRelationshipOtherEntity(rel, filing.entity_id, lookup)
    if (other && isGoodsEntityType(other.entity_type)) {
      map.set(other.entity_id, other)
    }
  }
  for (const g of queriedGoods ?? []) {
    if (!isGoodsEntityType(g.entity_type)) continue
    if (
      goodsSnapshotReferencesFiling(g, filing.entity_id) ||
      goodsSnapshotMatchesFilingScope(g, filing)
    ) {
      map.set(g.entity_id, g)
    }
  }
}

/**
 * Assets for this filing’s tax year: all Modelo-scoped `financial_account` rows (tags 720 / 721 / equity,
 * custody accounts, workbook import), relationship-linked accounts, plus `goods` / `good`.
 * Scope is by **tax year**, not by the filing’s `form_code`, so 720 and 721 filing pages share the same pool.
 */
export function mergeFilingAssetEntities(
  filing: Entity,
  relationships: Relationship[] | undefined,
  financialAccounts: Entity[] | undefined,
  queriedGoods: Entity[] | undefined,
): Entity[] {
  const map = new Map<string, Entity>()
  const accounts = financialAccounts ?? []
  const goods = queriedGoods ?? []
  const lookup = buildEntityLookup(accounts, goods)
  const filingYear = filingYearNumber(filing)

  if (filingYear != null) {
    for (const e of filterModeloScopedAccountsForYear(accounts, filingYear)) {
      map.set(e.entity_id, e)
    }
    for (const e of linkedFinancialAccountsForYear(filing, filingYear, relationships, lookup)) {
      map.set(e.entity_id, e)
    }
  } else {
    for (const e of linkedFinancialAccountsAnyYear(filing, relationships, lookup)) {
      map.set(e.entity_id, e)
    }
  }

  appendGoods(map, filing, relationships, queriedGoods, lookup)

  const merged = Array.from(map.values())
  const pool = financialAccounts ?? []
  const withoutWorkbookAggregates = excludeRedundantWorkbookParentAccounts(merged, pool)
  const y = filingYearNumber(filing)
  if (y == null) return withoutWorkbookAggregates
  return withoutWorkbookAggregates.map((e) => applyModeloWorkbookQ4Overlay(e, pool, y))
}
