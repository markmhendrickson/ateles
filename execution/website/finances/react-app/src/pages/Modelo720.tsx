import { useMemo } from 'react'
import { useEntitiesByType } from '@/hooks/useEntities'
import { useObservationHydratedFinancialAccounts } from '@/hooks/useObservationHydratedFinancialAccounts'
import { useMaskMode } from '@/context/MaskModeContext'
import { useEntityFxRates, useFilingYearEndFxRate } from '@/hooks/useEntityFxRates'
import { coalesceSnapshot, formatDate, formatEur, formatPercent, normalizeFilingTags, snapshotField } from '@/lib/formatters'
import {
  getEntityCanonicalEur,
  getEntityMonetaryDisplayBasisEur,
  totalNetWorthUsd,
} from '@/lib/aggregations'
import type { Entity } from '@/types/neotoma'
import EntityTable, { type Column } from '@/components/EntityTable'
import { AggregateMonetaryPair, MonetaryPair } from '@/components/MonetaryPair'
import { useAlignedMaskedFx } from '@/hooks/useAlignedMaskedFx'
import { type FilingYear } from '@/constants/filingYears'
import { deriveFinancialAccountInstitution, deriveFinancialAccountName } from '@/lib/humanize'
import { applyModeloWorkbookQ4Overlay } from '@/lib/modeloWorkbookQ4Overlay'
import { prepareFinancialAccountList } from '@/lib/financialAccountDedup'
import { accountMatchesFilingTaxYear, equityWorkbookMatchesFilingYear } from '@/lib/filingAssets'

export default function Modelo720({ filingYear }: { filingYear: FilingYear }) {

  const { data, isLoading, error } = useEntitiesByType('financial_account')
  const { entities: hydratedEntities } = useObservationHydratedFinancialAccounts(data?.entities, filingYear)
  const accountPool = hydratedEntities ?? data?.entities ?? []
  const { enabled: maskOn, text, freeform, maskNumber } = useMaskMode()
  const yearEndRate = useFilingYearEndFxRate(filingYear)
  const { resolveUsdPerEur, latestUsdPerEur } = useEntityFxRates(accountPool, {
    fallbackUsdPerEur: yearEndRate,
  })

  const columns = useMemo<Column[]>(() => [
    {
      key: 'institution',
      label: 'Institution',
      render: (_v, e) => {
        const label = deriveFinancialAccountInstitution(e) ?? '—'
        return <span className="font-medium">{text(label)}</span>
      },
    },
    {
      key: 'account_name',
      label: 'Account',
      render: (_v, e) => {
        return text(deriveFinancialAccountName(e) ?? '—')
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
      render: (_v, e) =>
        text(
          String(coalesceSnapshot<string>(e.snapshot, ['country', 'jurisdiction', 'jurisdiction_code']) ?? '—'),
        ),
    },
    { key: 'currency', label: 'Ccy', className: 'w-16', render: (v) => text(String(v ?? '—')) },
    {
      key: 'account_value',
      label: 'Value',
      sortAccessor: (e) => getEntityMonetaryDisplayBasisEur(e, resolveUsdPerEur),
      render: (_v, e) => (
        <MonetaryPair
          canonicalEur={getEntityCanonicalEur(e, resolveUsdPerEur)}
          usdPerEur={resolveUsdPerEur(e)}
          entity={e}
          pairKey={`m720-${e.entity_id}`}
          layout="inline"
          showConversion={false}
        />
      ),
      className: 'text-right',
    },
    {
      key: 'q4_average_balance_eur',
      label: 'Q4 average',
      sortAccessor: (e) => snapshotField<number>(e.snapshot, 'q4_average_balance_eur') ?? Number.NEGATIVE_INFINITY,
      render: (_v, e) => {
        const q4AvgEur = snapshotField<number>(e.snapshot, 'q4_average_balance_eur')
        const status = snapshotField<string>(e.snapshot, 'q4_reconciliation_status')
        if (q4AvgEur != null) return text(formatEur(q4AvgEur))
        if (status === 'missing_q4_average') return <span className="text-muted-foreground">{text('Missing')}</span>
        return text('—')
      },
      className: 'text-right',
    },
    {
      key: 'q4_vs_year_end_delta_eur',
      label: 'Delta vs Q4',
      sortAccessor: (e) => snapshotField<number>(e.snapshot, 'q4_vs_year_end_delta_eur') ?? Number.NEGATIVE_INFINITY,
      render: (_v, e) => {
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
      label: 'Last statement',
      render: (_v, e) => {
        const raw = coalesceSnapshot<string>(e.snapshot, [
          'last_statement_date',
          'statement_as_of_date',
          'statement_period_end',
          'assets_sheet_as_of_date',
        ])
        return maskOn ? freeform(String(raw ?? '')) : formatDate(raw)
      },
    },
  ], [text, freeform, maskOn, resolveUsdPerEur])

  const filtered = useMemo(() => {
    if (!accountPool.length) return []
    const pool = accountPool
    const deduped = prepareFinancialAccountList(pool)
    return deduped
      .filter((entity) => {
        const tags = normalizeFilingTags(entity.snapshot)
        if (!tags.includes('720')) return false
        return accountMatchesFilingTaxYear(entity, filingYear)
      })
      .map((entity) => applyModeloWorkbookQ4Overlay(entity, pool, filingYear))
  }, [accountPool, filingYear])

  /**
   * Workbook private equity / options rows use `filing_tags: equity`, not `720`, so they are hidden
   * from the primary table by design. Show them here for Bienes cross-check; gestor still decides 720 scope.
   * Include prior-year anchors (tax_year_context === filingYear - 1) while prepping the next return.
   */
  const equityWorkbookRows = useMemo(() => {
    if (!accountPool.length) return []
    const deduped = prepareFinancialAccountList(accountPool)
    return deduped.filter((entity) => equityWorkbookMatchesFilingYear(entity, filingYear))
  }, [accountPool, filingYear])

  const totalEur = useMemo(
    () => filtered.reduce((sum, e) => sum + getEntityMonetaryDisplayBasisEur(e, resolveUsdPerEur), 0),
    [filtered, resolveUsdPerEur],
  )
  const totalUsd = useMemo(
    () => totalNetWorthUsd(filtered, resolveUsdPerEur),
    [filtered, resolveUsdPerEur],
  )
  const nw = useAlignedMaskedFx(totalEur, totalUsd, `m720-tot-${filingYear}`, latestUsdPerEur)
  const q4TrackedCount = useMemo(
    () => filtered.filter((e) => snapshotField<number>(e.snapshot, 'q4_average_balance_eur') != null).length,
    [filtered],
  )
  const q4VarianceCount = useMemo(
    () =>
      filtered.filter((e) => snapshotField<string>(e.snapshot, 'q4_reconciliation_status') === 'variance').length,
    [filtered],
  )

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold tracking-tight">Modelo 720</h2>
        <div className="text-muted-foreground text-sm mt-1 flex flex-wrap items-end gap-x-2 gap-y-1">
          <span>
            Tax year {filingYear}
            {' · '}
            Foreign asset declaration &middot; {maskOn ? maskNumber(filtered.length, 'm720c') : filtered.length}{' '}
            account{filtered.length !== 1 ? 's' : ''} &middot; Q4 tracked{' '}
            {maskOn ? maskNumber(q4TrackedCount, `m720q4-${filingYear}`) : q4TrackedCount}/{maskOn ? maskNumber(filtered.length, `m720all-${filingYear}`) : filtered.length}
            {' · '}
            variances {maskOn ? maskNumber(q4VarianceCount, `m720var-${filingYear}`) : q4VarianceCount} &middot; total
          </span>
          <AggregateMonetaryPair eurLabel={nw.eurLabel} usdLabel={nw.usdLabel} align="left" />
        </div>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground animate-pulse">Loading...</p>}
      {error && <p className="text-sm text-destructive">Error: {(error as Error).message}</p>}

      <EntityTable
        entities={filtered}
        columns={columns}
        linkTo={(e: Entity) => `/accounts/${e.entity_id}`}
        emptyMessage="No 720-tagged accounts found"
        columnVisibilityStorageKey="modelo-720"
        defaultHiddenColumnKeysCsv="q4_vs_year_end_delta_eur"
        columnEnsureVisibleKeysCsv="account_name,account_value,q4_average_balance_eur"
        financialAccountDenomination
      />

      <div className="space-y-2 pt-4 border-t border-border">
        <h3 className="text-base font-semibold tracking-tight">Workbook equity &amp; private lines</h3>
        <p className="text-muted-foreground text-sm">
          Neotoma tags these as <code className="text-xs bg-muted px-1 rounded">equity</code> (Bienes anchors), not{' '}
          <code className="text-xs bg-muted px-1 rounded">720</code>. Listed here for visibility next to 720 cash/brokerage
          rows; confirm with your gestor whether each line belongs on Modelo 720.
        </p>
        <EntityTable
          entities={equityWorkbookRows}
          columns={columns}
          linkTo={(e: Entity) => `/accounts/${e.entity_id}`}
          emptyMessage="No equity-tagged workbook rows for this tax year (or prior-year anchor)."
          columnVisibilityStorageKey="modelo-720-equity"
          defaultHiddenColumnKeysCsv="q4_vs_year_end_delta_eur"
          columnEnsureVisibleKeysCsv="account_name,account_value"
          financialAccountDenomination
        />
      </div>
    </div>
  )
}
