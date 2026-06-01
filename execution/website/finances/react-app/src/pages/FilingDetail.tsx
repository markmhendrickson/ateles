import { useEffect, useMemo, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import { useEntity, useEntityRelationships } from '@/hooks/useEntity'
import { useEntities, useEntitiesByType } from '@/hooks/useEntities'
import { useObservationHydratedFinancialAccounts } from '@/hooks/useObservationHydratedFinancialAccounts'
import { useEntityFxRates, useFilingYearEndFxRate } from '@/hooks/useEntityFxRates'
import { useObservations } from '@/hooks/useObservations'
import { useMaskMode } from '@/context/MaskModeContext'
import {
  getEntityCanonicalEur,
  getEntityMonetaryDisplayBasisEur,
} from '@/lib/aggregations'
import {
  coalesceSnapshot,
  formatDate,
  formatEur,
  formatPercent,
  normalizeFilingTags,
  snapshotField,
} from '@/lib/formatters'
import { mergeFilingAssetEntities } from '@/lib/filingAssets'
import { dedupeFinancialAccountsByRegistry } from '@/lib/financialAccountDedup'
import { humanizePropertyKey } from '@/lib/propertyLabels'
import {
  humanizeRelationshipType,
  entityTypeLabel,
  entityDisplayName,
  deriveFinancialAccountInstitution,
  deriveFinancialAccountName,
  isWorkflowStatusSnapshotKey,
} from '@/lib/humanize'
import { entityHref, resolveEntityType } from '@/lib/entityRoutes'
import DataCompletenessBar from '@/components/DataCompletenessBar'
import type { Entity, Relationship } from '@/types/neotoma'
import { FILING_ACCOUNT_QUERY_LIMIT, isFilingYear, type FilingYear } from '@/constants/filingYears'
import { useDetailBreadcrumbLabel } from '@/context/BreadcrumbContext'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import EntityTable, { type Column } from '@/components/EntityTable'
import { MonetaryPair } from '@/components/MonetaryPair'
import PropertyLabel from '@/components/PropertyLabel'
import ObservationSourcesSummary from '@/components/ObservationSourcesSummary'
import ObservationTimeline from '@/components/ObservationTimeline'
import DownloadModeloButton from '@/components/DownloadModeloButton'
import WorkflowStatusBadge from '@/components/WorkflowStatusBadge'
import Modelo720 from '@/pages/Modelo720'
import Modelo721 from '@/pages/Modelo721'

function getRelationshipOtherEntity(rel: Relationship, currentId: string): Entity | null {
  if (rel.source_entity_id === currentId) return rel.target_entity ?? null
  if (rel.target_entity_id === currentId) return rel.source_entity ?? null
  return null
}

/** Shared limit so filing detail, Modelo pages, and workbook export all see the same entity set. */
const FILING_PAGE_ACCOUNT_QUERY_LIMIT = FILING_ACCOUNT_QUERY_LIMIT
const DECLARATION_SCOPES = ['720', '721', 'equity', 'other'] as const
type DeclarationScope = (typeof DECLARATION_SCOPES)[number]

function getDeclarationScopes(entity: Entity): DeclarationScope[] {
  const tags = normalizeFilingTags(entity.snapshot)
  const accountType = String(snapshotField<string>(entity.snapshot, 'account_type') ?? '').toLowerCase()
  const scopes = new Set<DeclarationScope>()

  if (tags.includes('720')) scopes.add('720')
  if (tags.includes('721') || accountType.includes('custod')) scopes.add('721')
  if (tags.includes('equity')) scopes.add('equity')
  if (scopes.size === 0) scopes.add('other')

  return DECLARATION_SCOPES.filter(scope => scopes.has(scope))
}

function getPrimaryDeclarationScope(scopes: DeclarationScope[]): DeclarationScope {
  if (scopes.includes('720')) return '720'
  if (scopes.includes('721')) return '721'
  if (scopes.includes('equity')) return 'equity'
  return 'other'
}

function declarationScopeLabel(scope: DeclarationScope): string {
  if (scope === 'other') return 'Other'
  return scope.toUpperCase()
}

export default function FilingDetail() {
  const { id } = useParams<{ id: string }>()
  const { data: entity, isLoading, error } = useEntity(id)
  const relationships = useEntityRelationships(id, { expandEntities: true })
  const financialAccountsQuery = useEntities({
    entity_type: 'financial_account',
    include_snapshots: true,
    limit: FILING_PAGE_ACCOUNT_QUERY_LIMIT,
  })
  const goodsQuery = useEntitiesByType('goods')
  const observations = useObservations(id)
  const { enabled: maskOn, text, freeform, deep, maskNumber } = useMaskMode()
  const [selectedScope, setSelectedScope] = useState<'all' | DeclarationScope>('all')
  const formCode = String(snapshotField<string>(entity?.snapshot, 'form_code') ?? '')
  const entityTaxYear = entity ? Number(snapshotField<number | string>(entity.snapshot, 'tax_year') ?? 0) : 0

  const { entities: hydratedFinancialAccounts } = useObservationHydratedFinancialAccounts(
    financialAccountsQuery.data?.entities,
    entityTaxYear > 0 ? entityTaxYear : null,
    !!entity && entityTaxYear > 0,
  )

  const filingAssets = useMemo(() => {
    if (!entity) return []
    const merged = mergeFilingAssetEntities(
      entity,
      relationships.data,
      hydratedFinancialAccounts ?? financialAccountsQuery.data?.entities,
      goodsQuery.data?.entities,
    )
    const accounts = merged.filter((e) => e.entity_type === 'financial_account')
    const nonAccounts = merged.filter((e) => e.entity_type !== 'financial_account')
    const deduped = [...dedupeFinancialAccountsByRegistry(accounts), ...nonAccounts]
    return [...deduped].sort((a, b) => {
      const ta = a.entity_type === 'financial_account' ? 0 : 1
      const tb = b.entity_type === 'financial_account' ? 0 : 1
      if (ta !== tb) return ta - tb
      const na = entityDisplayName(a).toLowerCase()
      const nb = entityDisplayName(b).toLowerCase()
      return na.localeCompare(nb)
    })
  }, [
    entity,
    relationships.data,
    hydratedFinancialAccounts,
    financialAccountsQuery.data?.entities,
    goodsQuery.data?.entities,
  ])

  const yearEndRate = useFilingYearEndFxRate(entityTaxYear || new Date().getFullYear())
  const { resolveUsdPerEur } = useEntityFxRates(filingAssets, {
    fallbackUsdPerEur: entityTaxYear ? yearEndRate : undefined,
  })

  const relevantScopes = useMemo<DeclarationScope[]>(() => {
    if (formCode === '720') return ['720', 'equity', 'other']
    if (formCode === '721') return ['721', 'other']
    return [...DECLARATION_SCOPES]
  }, [formCode])

  /** Sub-filters only: omit the form code that already defines the page (720/721). */
  const availableScopes = useMemo<DeclarationScope[]>(() => {
    if (formCode === '720') return ['equity', 'other']
    if (formCode === '721') return ['other']
    return [...DECLARATION_SCOPES]
  }, [formCode])

  const declarationScopeByEntityId = useMemo(() => {
    return new Map(filingAssets.map(entity => [entity.entity_id, getDeclarationScopes(entity)]))
  }, [filingAssets])

  const scopedFilingAssets = useMemo(() => {
    return filingAssets.filter(entity => {
      const scopes = declarationScopeByEntityId.get(entity.entity_id) ?? []
      return scopes.some(s => relevantScopes.includes(s))
    })
  }, [declarationScopeByEntityId, filingAssets, relevantScopes])

  const filteredFilingAssets = useMemo(() => {
    const scopeFilter = selectedScope === 'all' ? relevantScopes : [selectedScope]
    return filingAssets.filter(entity => {
      const scopes = declarationScopeByEntityId.get(entity.entity_id) ?? []
      return scopes.some(s => scopeFilter.includes(s))
    })
  }, [declarationScopeByEntityId, filingAssets, relevantScopes, selectedScope])

  const declarationSummary = useMemo(() => {
    const summary: Record<DeclarationScope, { count: number; totalEur: number }> = {
      '720': { count: 0, totalEur: 0 },
      '721': { count: 0, totalEur: 0 },
      equity: { count: 0, totalEur: 0 },
      other: { count: 0, totalEur: 0 },
    }
    for (const entity of scopedFilingAssets) {
      const scopes = declarationScopeByEntityId.get(entity.entity_id) ?? ['other']
      const primaryScope = getPrimaryDeclarationScope(scopes)
      summary[primaryScope].count += 1
      if (entity.entity_type === 'financial_account') {
        summary[primaryScope].totalEur += getEntityMonetaryDisplayBasisEur(entity, resolveUsdPerEur)
      }
    }
    return summary
  }, [declarationScopeByEntityId, scopedFilingAssets, resolveUsdPerEur])

  useEffect(() => {
    if (selectedScope !== 'all' && !availableScopes.includes(selectedScope)) {
      setSelectedScope('all')
    }
  }, [availableScopes, selectedScope])

  const assetColumns = useMemo<Column[]>(
    () => [
      {
        key: 'entity_type',
        label: 'Type',
        sortable: true,
        render: (_v, e) => text(humanizePropertyKey(e.entity_type)),
      },
      {
        key: 'declaration_scope',
        label: 'Declaration scope',
        sortable: false,
        render: (_v, e) => {
          const scopes = declarationScopeByEntityId.get(e.entity_id) ?? ['other']
          return text(scopes.map(scope => declarationScopeLabel(scope)).join(' / '))
        },
      },
      {
        key: 'institution',
        label: 'Institution',
        render: (_v, e) => {
          if (e.entity_type === 'financial_account') {
            const label = deriveFinancialAccountInstitution(e) ?? '—'
            return <span className="font-medium">{text(label)}</span>
          }
          return text(
            String(
              coalesceSnapshot<string>(e.snapshot, ['title', 'name', 'canonical_name']) ??
                e.canonical_name ??
                e.entity_id,
            ),
          )
        },
      },
      {
        key: 'account_name',
        label: 'Account / name',
        sortable: true,
        render: (_v, e) => {
          if (e.entity_type === 'financial_account') {
            return text(deriveFinancialAccountName(e) ?? '—')
          }
          return text(
            String(coalesceSnapshot<string>(e.snapshot, ['description', 'notes', 'detail']) ?? '—'),
          )
        },
      },
      {
        key: 'modelo_bien',
        label: 'Modelo bien',
        render: (_v, e) =>
          text(String(coalesceSnapshot<string>(e.snapshot, ['modelo_bien', 'modelo_bien_hint']) ?? '—')),
      },
      {
        key: 'country',
        label: 'Country',
        sortable: true,
        render: (_v, e) =>
          text(
            String(coalesceSnapshot<string>(e.snapshot, ['country', 'jurisdiction', 'jurisdiction_code']) ?? '—'),
          ),
      },
      {
        key: 'currency',
        label: 'Ccy',
        className: 'w-16',
        render: (v, e) =>
          e.entity_type === 'financial_account' ? text(String(v ?? '—')) : text('—'),
      },
      {
        key: 'account_value',
        label: 'Value',
        sortAccessor: (e) =>
          e.entity_type === 'financial_account' ? getEntityMonetaryDisplayBasisEur(e, resolveUsdPerEur) : 0,
        render: (_v, e) => {
          if (e.entity_type !== 'financial_account') return text('—')
          return (
            <MonetaryPair
              canonicalEur={getEntityCanonicalEur(e, resolveUsdPerEur)}
              usdPerEur={resolveUsdPerEur(e)}
              entity={e}
              pairKey={`filing-asset-${e.entity_id}`}
              layout="inline"
              showConversion={false}
            />
          )
        },
        className: 'text-right',
      },
      {
        key: 'q4_average_balance_eur',
        label: 'Q4 average',
        sortAccessor: (e) =>
          e.entity_type === 'financial_account'
            ? (snapshotField<number>(e.snapshot, 'q4_average_balance_eur') ?? Number.NEGATIVE_INFINITY)
            : Number.NEGATIVE_INFINITY,
        render: (_v, e) => {
          if (e.entity_type !== 'financial_account') return text('—')
          const q4AvgEur = snapshotField<number>(e.snapshot, 'q4_average_balance_eur')
          const status = snapshotField<string>(e.snapshot, 'q4_reconciliation_status')
          if (q4AvgEur != null) return text(formatEur(q4AvgEur))
          if (status === 'missing_q4_average') {
            const tags = normalizeFilingTags(e.snapshot)
            const equityWorkbookLine = tags.includes('equity') && !tags.includes('720')
            if (equityWorkbookLine) {
              return (
                <span
                  className="text-muted-foreground"
                  title="No trimestre promedio row for this bien in Cantidades; add one in the workbook to reconcile Q4."
                >
                  {text('—')}
                </span>
              )
            }
            return <span className="text-muted-foreground">{text('Missing')}</span>
          }
          return text('—')
        },
        className: 'text-right',
      },
      {
        key: 'q4_vs_year_end_delta_eur',
        label: 'Delta vs Q4',
        sortAccessor: (e) =>
          e.entity_type === 'financial_account'
            ? (snapshotField<number>(e.snapshot, 'q4_vs_year_end_delta_eur') ?? Number.NEGATIVE_INFINITY)
            : Number.NEGATIVE_INFINITY,
        render: (_v, e) => {
          if (e.entity_type !== 'financial_account') return text('—')
          const delta = snapshotField<number>(e.snapshot, 'q4_vs_year_end_delta_eur')
          const deltaPct = snapshotField<number>(e.snapshot, 'q4_vs_year_end_delta_pct')
          if (delta == null) return text('—')
          const amount = formatEur(delta)
          const pct = deltaPct != null ? ` (${formatPercent(deltaPct)})` : ''
          const signed = delta > 0 ? `+${amount}` : amount
          return text(`${signed}${pct}`)
        },
        className: 'text-right',
      },
      {
        key: 'last_statement_date',
        label: 'Last statement / registry',
        render: (_v, e) => {
          if (e.entity_type === 'financial_account') {
            const raw = coalesceSnapshot<string>(e.snapshot, [
              'last_statement_date',
              'statement_as_of_date',
              'statement_period_end',
              'assets_sheet_as_of_date',
            ])
            return maskOn ? freeform(String(raw ?? '')) : formatDate(raw)
          }
          return freeform(String(coalesceSnapshot<string>(e.snapshot, ['registry_id']) ?? '—'))
        },
      },
    ],
    [text, freeform, maskOn, resolveUsdPerEur, declarationScopeByEntityId],
  )

  const titleBase =
    entity == null
      ? null
      : (snapshotField<string>(entity.snapshot, 'title') ??
          entity.canonical_name ??
          snapshotField<string>(entity.snapshot, 'form_code') ??
          entity.entity_id)
  const breadcrumbLabel =
    titleBase == null ? null : maskOn ? text(String(titleBase)) : String(titleBase)
  useDetailBreadcrumbLabel(breadcrumbLabel)

  if (isLoading) return <p className="text-sm text-muted-foreground animate-pulse py-8">Loading...</p>
  if (error) return <p className="text-sm text-destructive py-8">Error: {(error as Error).message}</p>
  if (!entity) return <p className="text-sm text-muted-foreground py-8">Not found</p>

  if (resolveEntityType(entity) !== 'tax_filing') {
    return <Navigate to={entityHref(entity)} replace />
  }

  const fields = entity.snapshot
    ? Object.entries(entity.snapshot).filter(([key]) => key !== 'entity_id' && key !== 'entity_type')
    : []

  const title = titleBase!
  const taxYearString = String(snapshotField<number | string>(entity.snapshot, 'tax_year') ?? '')
  const filingYear = isFilingYear(taxYearString) ? (Number(taxYearString) as FilingYear) : null

  function formatFieldValue(key: string, value: unknown): string {
    if (value == null || value === '') return '—'
    if ((key.includes('date') || key.endsWith('_at')) && typeof value === 'string') {
      return maskOn ? freeform(value) : formatDate(value)
    }
    if (Array.isArray(value)) return `[${maskOn ? maskNumber(value.length, `arr:${key}`) : value.length} items]`
    if (typeof value === 'object') return JSON.stringify(maskOn ? deep(value) : value)
    if (typeof value === 'number') return String(maskOn ? maskNumber(value, `field:${key}`) : value)
    return maskOn ? freeform(String(value)) : String(value)
  }

  return (
    <div className="space-y-8">
      <div>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2 className="text-2xl font-semibold tracking-tight">
              {maskOn ? text(String(title)) : String(title)}
            </h2>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              <Badge variant="secondary" className="font-normal">
                {maskOn ? text(String(formCode || entity.entity_type)) : String(formCode || entity.entity_type)}
              </Badge>
              <WorkflowStatusBadge
                value={snapshotField(entity.snapshot, 'status') ?? 'unknown'}
                maskOn={maskOn}
                text={text}
              />
              {snapshotField(entity.snapshot, 'tax_year') != null && (
                <span>
                  Year {maskOn ? maskNumber(Number(snapshotField(entity.snapshot, 'tax_year')), 'filing-tax-year') : String(snapshotField(entity.snapshot, 'tax_year'))}
                </span>
              )}
            </div>
          </div>
          {filingYear && (
            <div className="pt-1">
              <DownloadModeloButton filingYear={filingYear} />
            </div>
          )}
        </div>
      </div>

      <DataCompletenessBar entityType={entity.entity_type} snapshot={entity.snapshot} />

      <section>
        <h3 className="text-lg font-medium mb-3">
          All declarable assets (
          {financialAccountsQuery.isLoading || goodsQuery.isLoading
            ? '…'
            : maskOn
              ? maskNumber(scopedFilingAssets.length, 'filing-assets-count')
              : scopedFilingAssets.length}
          )
        </h3>
        <p className="text-muted-foreground text-sm mb-3">
          One consolidated view of filing-linked assets. Use scope chips to narrow by declaration workflow, and expand
          advanced sections below for form-specific review tables.
        </p>
        <div className="mb-3 flex flex-wrap gap-2">
          <Button
            type="button"
            size="sm"
            variant={selectedScope === 'all' ? 'secondary' : 'outline'}
            onClick={() => setSelectedScope('all')}
          >
            {maskOn ? text('All') : 'All'} ({maskOn ? maskNumber(scopedFilingAssets.length, 'scope-all-count') : scopedFilingAssets.length})
          </Button>
          {availableScopes.map(scope => (
            <Button
              key={scope}
              type="button"
              size="sm"
              variant={selectedScope === scope ? 'secondary' : 'outline'}
              onClick={() => setSelectedScope(scope)}
            >
              {maskOn ? text(declarationScopeLabel(scope)) : declarationScopeLabel(scope)} (
              {maskOn
                ? maskNumber(declarationSummary[scope].count, `scope-count-${scope}`)
                : declarationSummary[scope].count}
              )
            </Button>
          ))}
        </div>
        <div className="mb-4 flex flex-wrap gap-2 text-xs text-muted-foreground">
          {availableScopes.map(scope => (
            <Badge key={scope} variant="outline" className="font-normal">
              {declarationScopeLabel(scope)}:{' '}
              {maskOn
                ? text(formatEur(declarationSummary[scope].totalEur))
                : formatEur(declarationSummary[scope].totalEur)}
            </Badge>
          ))}
        </div>
        {financialAccountsQuery.error && (
          <p className="text-sm text-destructive mb-2">
            Error loading accounts: {(financialAccountsQuery.error as Error).message}
          </p>
        )}
        {goodsQuery.error && (
          <p className="text-sm text-destructive mb-2">
            Error loading goods: {(goodsQuery.error as Error).message}
          </p>
        )}
        <EntityTable
          entities={filteredFilingAssets}
          columns={assetColumns}
          linkTo={entityHref}
          columnVisibilityStorageKey="filing-assets-v3"
          defaultHiddenColumnKeysCsv="declaration_scope,entity_type,modelo_bien,country,currency,last_statement_date,q4_vs_year_end_delta_eur"
          columnEnsureVisibleKeysCsv="institution,account_name,account_value,q4_average_balance_eur"
          defaultSortKey="account_value"
          defaultSortDir="desc"
          emptyMessage="No assets for this tax year. Tag accounts (720/721/equity), set tax_year / tax_year_context, import the Modelo workbook, or link accounts to this tax_filing."
          financialAccountDenomination
        />
      </section>

      <section className="space-y-3">
        <h3 className="text-lg font-medium">Advanced filing-specific views</h3>
        {formCode === '720' && filingYear && (
          <details className="rounded-lg border border-border bg-card">
            <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
              Detailed Modelo 720 tables
            </summary>
            <div className="border-t border-border px-4 py-4">
              <Modelo720 filingYear={filingYear} />
            </div>
          </details>
        )}
        {formCode === '721' && filingYear && (
          <details className="rounded-lg border border-border bg-card">
            <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
              Detailed Modelo 721 tables
            </summary>
            <div className="border-t border-border px-4 py-4">
              <Modelo721 filingYear={filingYear} />
            </div>
          </details>
        )}
      </section>

      <section>
        <h3 className="text-lg font-medium mb-3">Filing properties</h3>
        <Card className="overflow-hidden py-0">
          <CardContent className="p-0">
            <dl className="divide-y divide-border">
              {fields.map(([key, value]) => (
                <div key={key} className="flex px-4 py-2.5">
                  <PropertyLabel
                    literalKey={key}
                    as="dt"
                    className="w-56 shrink-0 text-sm text-muted-foreground"
                  />
                  <dd className="text-sm break-all">
                    {isWorkflowStatusSnapshotKey(key) && typeof value === 'string' ? (
                      <WorkflowStatusBadge value={value} maskOn={maskOn} text={text} />
                    ) : (
                      formatFieldValue(key, value)
                    )}
                  </dd>
                </div>
              ))}
            </dl>
          </CardContent>
        </Card>
      </section>

      {relationships.data && relationships.data.length > 0 && (
        <section>
          <h3 className="text-lg font-medium mb-3">
            Related records ({maskOn ? maskNumber(relationships.data.length, 'filing-rel-count') : relationships.data.length})
          </h3>
          <Card className="overflow-hidden py-0">
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-40">Relationship</TableHead>
                    <TableHead className="w-40">Type</TableHead>
                    <TableHead>Name</TableHead>
                    <TableHead className="w-20 text-right">Open</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {relationships.data.map(rel => {
                    const otherEntity = getRelationshipOtherEntity(rel, entity.entity_id)
                    const otherId = rel.source_entity_id === entity.entity_id ? rel.target_entity_id : rel.source_entity_id
                    const otherName = otherEntity ? entityDisplayName(otherEntity) : otherId
                    const href = otherEntity ? entityHref(otherEntity) : `/explorer?id=${encodeURIComponent(otherId)}`
                    return (
                      <TableRow key={rel.relationship_key}>
                        <TableCell>
                          <Badge variant="secondary" className="font-normal" title={rel.relationship_type}>
                            {maskOn ? text(humanizeRelationshipType(rel.relationship_type)) : humanizeRelationshipType(rel.relationship_type)}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {otherEntity?.entity_type
                            ? (maskOn ? text(entityTypeLabel(otherEntity.entity_type)) : entityTypeLabel(otherEntity.entity_type))
                            : '—'}
                        </TableCell>
                        <TableCell className="font-medium">
                          {maskOn ? text(String(otherName)) : String(otherName)}
                        </TableCell>
                        <TableCell className="text-right">
                          <Button variant="link" size="sm" className="h-auto p-0" asChild>
                            <Link to={href}>Open</Link>
                          </Button>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </section>
      )}

      {observations.isLoading && (
        <p className="text-sm text-muted-foreground animate-pulse">Loading observations…</p>
      )}
      {observations.error && (
        <p className="text-sm text-destructive">
          Error loading observations: {(observations.error as Error).message}
        </p>
      )}

      {observations.data && observations.data.length > 0 && (
        <>
          <ObservationSourcesSummary observations={observations.data} />

          <section>
            <h3 className="text-lg font-medium mb-3">
              All observations ({maskOn ? maskNumber(observations.data.length, 'filing-obsc') : observations.data.length})
            </h3>
            <ObservationTimeline observations={observations.data} />
          </section>
        </>
      )}
    </div>
  )
}
