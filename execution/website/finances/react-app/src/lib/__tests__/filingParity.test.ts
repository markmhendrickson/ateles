import { describe, it, expect } from 'vitest'
import { normalizeFilingTags } from '@/lib/formatters'
import {
  matchesTaxYearContext,
  accountMatchesFilingTaxYear,
  equityWorkbookMatchesFilingYear,
} from '@/lib/filingAssets'
import { collectModeloScope } from '@/lib/modeloScope'
import type { Entity } from '@/types/neotoma'

function makeEntity(snapshot: Record<string, unknown>, id = 'e-1'): Entity {
  return {
    entity_id: id,
    entity_type: 'financial_account',
    canonical_name: snapshot.canonical_name as string | undefined ?? null,
    snapshot,
  } as Entity
}

describe('normalizeFilingTags', () => {
  it('returns empty for null snapshot', () => {
    expect(normalizeFilingTags(null)).toEqual([])
  })

  it('returns empty for missing filing_tags', () => {
    expect(normalizeFilingTags({ foo: 'bar' })).toEqual([])
  })

  it('normalizes array tags', () => {
    expect(normalizeFilingTags({ filing_tags: ['720', '721'] })).toEqual(['720', '721'])
  })

  it('normalizes string tags (comma-separated)', () => {
    expect(normalizeFilingTags({ filing_tags: '720, 721' })).toEqual(['720', '721'])
  })

  it('handles single string tag', () => {
    expect(normalizeFilingTags({ filing_tags: '720' })).toEqual(['720'])
  })

  it('strips whitespace', () => {
    expect(normalizeFilingTags({ filing_tags: ' 720 , equity ' })).toEqual(['720', 'equity'])
  })
})

describe('matchesTaxYearContext', () => {
  it('matches explicit tax_year_context', () => {
    const e = makeEntity({ tax_year_context: 2025 })
    expect(matchesTaxYearContext(e, 2025)).toBe(true)
    expect(matchesTaxYearContext(e, 2024)).toBe(false)
  })

  it('null tax_year_context matches only DEFAULT_FILING_YEAR', () => {
    const e = makeEntity({})
    expect(matchesTaxYearContext(e, 2025)).toBe(true)
    expect(matchesTaxYearContext(e, 2024)).toBe(false)
  })
})

describe('accountMatchesFilingTaxYear', () => {
  it('matches via tax_year_context', () => {
    const e = makeEntity({ tax_year_context: 2025 })
    expect(accountMatchesFilingTaxYear(e, 2025)).toBe(true)
  })

  it('falls back to tax_year when tax_year_context is missing', () => {
    const e = makeEntity({ tax_year: 2025 })
    expect(accountMatchesFilingTaxYear(e, 2025)).toBe(true)
    expect(accountMatchesFilingTaxYear(e, 2024)).toBe(false)
  })

  it('matches either tax_year_context or tax_year (union)', () => {
    const e = makeEntity({ tax_year_context: 2024, tax_year: 2025 })
    expect(accountMatchesFilingTaxYear(e, 2024)).toBe(true)
    expect(accountMatchesFilingTaxYear(e, 2025)).toBe(true)
    expect(accountMatchesFilingTaxYear(e, 2023)).toBe(false)
  })
})

describe('equityWorkbookMatchesFilingYear', () => {
  it('matches current filing year', () => {
    const e = makeEntity({ filing_tags: ['equity'], tax_year_context: 2025 })
    expect(equityWorkbookMatchesFilingYear(e, 2025)).toBe(true)
  })

  it('matches prior-year anchor', () => {
    const e = makeEntity({ filing_tags: ['equity'], tax_year_context: 2024 })
    expect(equityWorkbookMatchesFilingYear(e, 2025)).toBe(true)
  })

  it('rejects two years back', () => {
    const e = makeEntity({ filing_tags: ['equity'], tax_year_context: 2023 })
    expect(equityWorkbookMatchesFilingYear(e, 2025)).toBe(false)
  })

  it('rejects non-equity tagged entities', () => {
    const e = makeEntity({ filing_tags: ['720'], tax_year_context: 2025 })
    expect(equityWorkbookMatchesFilingYear(e, 2025)).toBe(false)
  })

  it('falls back to tax_year for equity', () => {
    const e = makeEntity({ filing_tags: ['equity'], tax_year: 2025 })
    expect(equityWorkbookMatchesFilingYear(e, 2025)).toBe(true)
  })
})

describe('collectModeloScope parity', () => {
  const accounts720 = [
    makeEntity({ filing_tags: ['720'], tax_year_context: 2025, registry_id: 'bank_a', balance_eur: 1000 }, 'a-720'),
    makeEntity({ filing_tags: ['720'], tax_year_context: 2024, registry_id: 'bank_b', balance_eur: 500 }, 'b-720'),
  ]
  const accountsEquity = [
    makeEntity({ filing_tags: ['equity'], tax_year_context: 2025, registry_id: 'eq_a', balance_eur: 2000 }, 'a-eq'),
    makeEntity({ filing_tags: ['equity'], tax_year_context: 2024, registry_id: 'eq_b', balance_eur: 300 }, 'b-eq'),
  ]
  const accounts721 = [
    makeEntity({ filing_tags: ['721'], tax_year_context: 2025, registry_id: 'custody_cb', balance_eur: 500 }, 'a-721'),
    makeEntity({ filing_tags: ['721'], tax_year_context: 2024, registry_id: 'custody_kr', balance_eur: 10 }, 'b-721'),
  ]
  const all = [...accounts720, ...accountsEquity, ...accounts721]

  it('720 scope includes only matching year', () => {
    const scope = collectModeloScope(all, 2025)
    const ids = scope.accounts720.map((e) => e.entity_id)
    expect(ids).toContain('a-720')
    expect(ids).not.toContain('b-720')
  })

  it('equity scope includes prior-year anchor', () => {
    const scope = collectModeloScope(all, 2025)
    const ids = scope.equityRows.map((e) => e.entity_id)
    expect(ids).toContain('a-eq')
    expect(ids).toContain('b-eq')
  })

  it('721 scope includes only matching year', () => {
    const scope = collectModeloScope(all, 2025)
    const ids = scope.accounts721.map((e) => e.entity_id)
    expect(ids).toContain('a-721')
    expect(ids).not.toContain('b-721')
  })

  it('allRows is union of 720 + equity + 721', () => {
    const scope = collectModeloScope(all, 2025)
    expect(scope.allRows.length).toBe(
      scope.accounts720.length + scope.equityRows.length + scope.accounts721.length,
    )
  })

  it('scope with tax_year fallback includes entities missing tax_year_context', () => {
    const withTaxYear = makeEntity({ filing_tags: ['720'], tax_year: 2025, registry_id: 'ty_only', balance_eur: 100 }, 'ty-only')
    const scope = collectModeloScope([...all, withTaxYear], 2025)
    const ids = scope.accounts720.map((e) => e.entity_id)
    expect(ids).toContain('ty-only')
  })

  it('scope uses normalized tags (comma-separated string)', () => {
    const strTags = makeEntity({ filing_tags: '720, equity', tax_year_context: 2025, registry_id: 'str_tags', balance_eur: 50 }, 'str-tags')
    const scope = collectModeloScope([...all, strTags], 2025)
    const in720 = scope.accounts720.map((e) => e.entity_id)
    const inEquity = scope.equityRows.map((e) => e.entity_id)
    expect(in720).toContain('str-tags')
    expect(inEquity).toContain('str-tags')
  })
})
