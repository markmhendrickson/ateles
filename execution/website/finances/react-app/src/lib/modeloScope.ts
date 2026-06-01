import type { Entity } from '@/types/neotoma'
import { normalizeFilingTags, snapshotField } from '@/lib/formatters'
import { getEntityRawStorageLegs } from '@/lib/aggregations'
import { prepareFinancialAccountList } from '@/lib/financialAccountDedup'
import {
  accountMatchesFilingTaxYear,
  equityWorkbookMatchesFilingYear,
} from '@/lib/filingAssets'
import { applyModeloWorkbookQ4Overlay } from '@/lib/modeloWorkbookQ4Overlay'

/**
 * Canonical storage contract for filing-critical `financial_account` rows.
 *
 * Read precedence for EUR display:
 *   1. `balance_eur` (pre-computed filing EUR, stored by import scripts)
 *   2. `balance_value` + `balance_currency='USD'` → convert at pinned filing FX
 *   3. `account_value` + `account_value_currency` (legacy top-level field)
 *   4. Computed display fallback via `getEntityMonetaryDisplayBasisEur`
 *
 * All surfaces (filing UI, workbook export, CLI export, coverage report)
 * should resolve balances via the same `aggregations.ts` cascade.
 */
export const FILING_CONTRACT_REQUIRED_FIELDS = [
  'registry_id',
  'filing_tags',
] as const

export const FILING_CONTRACT_RECOMMENDED_FIELDS = [
  'tax_year_context',
  'balance_date',
  'balance_value',
  'balance_currency',
  'balance_eur',
  'modelo_bien',
] as const

export interface ModeloScope {
  accounts720: Entity[]
  equityRows: Entity[]
  accounts721: Entity[]
  allRows: Entity[]
}

export function collectModeloScope(entities: Entity[], filingYear: number): ModeloScope {
  const scoped = prepareFinancialAccountList(entities)
  const pool = entities

  const accounts720 = scoped
    .filter((e) => {
      const tags = normalizeFilingTags(e.snapshot)
      return tags.includes('720') && accountMatchesFilingTaxYear(e, filingYear)
    })
    .map((e) => applyModeloWorkbookQ4Overlay(e, pool, filingYear))

  const equityRows = scoped
    .filter((e) => equityWorkbookMatchesFilingYear(e, filingYear))
    .map((e) => applyModeloWorkbookQ4Overlay(e, pool, filingYear))

  const accounts721 = scoped.filter((e) => {
    const tags = normalizeFilingTags(e.snapshot)
    const type = snapshotField<string>(e.snapshot, 'account_type')
    const is721 = tags.includes('721') || (type != null && type.toLowerCase().includes('custod'))
    if (!is721) return false
    return accountMatchesFilingTaxYear(e, filingYear)
  })

  return {
    accounts720,
    equityRows,
    accounts721,
    allRows: [...accounts720, ...equityRows, ...accounts721],
  }
}

export interface ModeloCoverageIssue {
  entityId: string
  registryId: string
  accountName: string
  issue: string
}

/**
 * Enforce Neotoma-first readiness before generating filing copies.
 * Validates both required and recommended fields per the filing storage contract.
 */
export function findModeloCoverageIssues(scope: ModeloScope): ModeloCoverageIssue[] {
  const issues: ModeloCoverageIssue[] = []

  for (const e of scope.allRows) {
    const registryId = String(snapshotField<string>(e.snapshot, 'registry_id') ?? '')
    const accountName = String(
      snapshotField<string>(e.snapshot, 'account_name') ??
        snapshotField<string>(e.snapshot, 'display_name_en') ??
        snapshotField<string>(e.snapshot, 'canonical_name') ??
        e.canonical_name ??
        e.entity_id,
    )
    const modeloBien = String(
      snapshotField<string>(e.snapshot, 'modelo_bien') ??
        snapshotField<string>(e.snapshot, 'modelo_bien_hint') ??
        '',
    )
    const raw = getEntityRawStorageLegs(e)
    const hasBalance = Math.abs(raw.eur) > 1e-9 || Math.abs(raw.usd) > 1e-9
    const accountValue = Number(snapshotField<number | string>(e.snapshot, 'account_value') ?? 0)
    const hasTopLevelBalance = Number.isFinite(accountValue) && Math.abs(accountValue) > 1e-9
    const hasAnyBalance = hasBalance || hasTopLevelBalance
    const closed = String(snapshotField<string>(e.snapshot, 'account_status') ?? '').toLowerCase() === 'closed'

    if (!registryId) {
      issues.push({
        entityId: e.entity_id,
        registryId: '',
        accountName,
        issue: 'missing `registry_id`',
      })
    }
    if (!modeloBien) {
      issues.push({
        entityId: e.entity_id,
        registryId,
        accountName,
        issue: 'missing `modelo_bien` / `modelo_bien_hint`',
      })
    }
    if (!closed && !hasAnyBalance) {
      issues.push({
        entityId: e.entity_id,
        registryId,
        accountName,
        issue: 'missing non-zero balance for active account (`account_value` and eur/usd legs are empty)',
      })
    }

    const tags = normalizeFilingTags(e.snapshot)
    if (tags.length === 0) {
      issues.push({
        entityId: e.entity_id,
        registryId,
        accountName,
        issue: 'missing `filing_tags` (required by filing contract)',
      })
    }

    const hasTaxYearContext = snapshotField(e.snapshot, 'tax_year_context') != null
    const hasTaxYear = snapshotField(e.snapshot, 'tax_year') != null
    if (!hasTaxYearContext && !hasTaxYear) {
      issues.push({
        entityId: e.entity_id,
        registryId,
        accountName,
        issue: 'missing both `tax_year_context` and `tax_year` (recommended by filing contract)',
      })
    }

    const hasBalanceDate = snapshotField(e.snapshot, 'balance_date') != null
    const hasStatementDate = snapshotField(e.snapshot, 'last_statement_date') != null
    if (!hasBalanceDate && !hasStatementDate) {
      issues.push({
        entityId: e.entity_id,
        registryId,
        accountName,
        issue: 'missing `balance_date` / `last_statement_date` (FX rate will use year-end fallback)',
      })
    }
  }

  return issues
}

