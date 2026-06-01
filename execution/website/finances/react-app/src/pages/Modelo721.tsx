import { useMemo } from 'react'
import { useEntitiesByType } from '@/hooks/useEntities'
import { useObservationHydratedFinancialAccounts } from '@/hooks/useObservationHydratedFinancialAccounts'
import { useMaskMode } from '@/context/MaskModeContext'
import { useEntityFxRates, useFilingYearEndFxRate } from '@/hooks/useEntityFxRates'
import {
  bucketNetWorthUsd,
  getEntityCanonicalEur,
  getEntityMonetaryDisplayBasisEur,
  groupByStrategyBucket,
  totalNetWorthUsd,
} from '@/lib/aggregations'
import { normalizeFilingTags, snapshotField } from '@/lib/formatters'
import { deriveFinancialAccountInstitution, deriveFinancialAccountName } from '@/lib/humanize'
import type { Entity } from '@/types/neotoma'
import EntityTable, { type Column } from '@/components/EntityTable'
import StrategyBucketCard from '@/components/StrategyBucketCard'
import { AggregateMonetaryPair, MonetaryPair } from '@/components/MonetaryPair'
import { useAlignedMaskedFx } from '@/hooks/useAlignedMaskedFx'
import { type FilingYear } from '@/constants/filingYears'
import { prepareFinancialAccountList } from '@/lib/financialAccountDedup'
import { accountMatchesFilingTaxYear } from '@/lib/filingAssets'

export default function Modelo721({ filingYear }: { filingYear: FilingYear }) {

  const { data, isLoading, error } = useEntitiesByType('financial_account')
  const { entities: hydratedEntities } = useObservationHydratedFinancialAccounts(data?.entities, filingYear)
  const accountPool = hydratedEntities ?? data?.entities ?? []
  const { text, maskNumber } = useMaskMode()

  const cryptoAccounts = useMemo(() => {
    if (!accountPool.length) return []
    const prepared = prepareFinancialAccountList(accountPool)
    return prepared.filter((entity) => {
      const tags = normalizeFilingTags(entity.snapshot)
      const type = snapshotField<string>(entity.snapshot, 'account_type')
      const is721 =
        tags.includes('721') || (type != null && type.toLowerCase().includes('custod'))
      if (!is721) return false
      return accountMatchesFilingTaxYear(entity, filingYear)
    })
  }, [accountPool, filingYear])

  const yearEndRate = useFilingYearEndFxRate(filingYear)
  const { resolveUsdPerEur, latestUsdPerEur } = useEntityFxRates(cryptoAccounts, {
    fallbackUsdPerEur: yearEndRate,
  })

  const columns = useMemo<Column[]>(() => [
    {
      key: 'institution',
      label: 'Institution / Custody',
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
    { key: 'account_type', label: 'Type', render: (v) => text(String(v ?? '—')) },
    { key: 'strategy_bucket', label: 'Strategy', render: (v) => text(String(v ?? '—')) },
    { key: 'currency', label: 'Ccy', className: 'w-16', render: (v) => text(String(v ?? '—')) },
    {
      key: 'balance_eur',
      label: 'Balance (display · stored)',
      sortAccessor: (e) => getEntityMonetaryDisplayBasisEur(e, resolveUsdPerEur),
      render: (_v, e) => (
        <MonetaryPair
          canonicalEur={getEntityCanonicalEur(e, resolveUsdPerEur)}
          usdPerEur={resolveUsdPerEur(e)}
          entity={e}
          pairKey={`m721-${e.entity_id}`}
          layout="inline"
          showConversion={false}
        />
      ),
      className: 'text-right',
    },
  ], [text, resolveUsdPerEur])

  const buckets = useMemo(
    () => groupByStrategyBucket(cryptoAccounts, resolveUsdPerEur),
    [cryptoAccounts, resolveUsdPerEur],
  )
  const totalEur = useMemo(
    () => cryptoAccounts.reduce((sum, e) => sum + getEntityMonetaryDisplayBasisEur(e, resolveUsdPerEur), 0),
    [cryptoAccounts, resolveUsdPerEur],
  )
  const totalUsd = useMemo(
    () => totalNetWorthUsd(cryptoAccounts, resolveUsdPerEur),
    [cryptoAccounts, resolveUsdPerEur],
  )
  const nw = useAlignedMaskedFx(totalEur, totalUsd, `m721-tot-${filingYear}`, latestUsdPerEur)

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-lg font-semibold tracking-tight">Modelo 721 / Crypto</h2>
        <div className="text-muted-foreground text-sm mt-1 flex flex-wrap items-end gap-x-2 gap-y-1">
          <span>
            Tax year {filingYear}
            {' · '}
            Crypto custody accounts &middot; {maskNumber(cryptoAccounts.length, 'm721n')} account
            {cryptoAccounts.length !== 1 ? 's' : ''} &middot; total
          </span>
          <AggregateMonetaryPair eurLabel={nw.eurLabel} usdLabel={nw.usdLabel} align="left" />
        </div>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground animate-pulse">Loading...</p>}
      {error && <p className="text-sm text-destructive">Error: {(error as Error).message}</p>}

      {buckets.length > 0 && (
        <section>
          <h2 className="text-lg font-medium mb-3">By strategy bucket</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {buckets.map(b => (
              <StrategyBucketCard
                key={b.bucket}
                bucket={b}
                totalNetWorthEur={totalEur}
                bucketTotalUsd={bucketNetWorthUsd(b, resolveUsdPerEur)}
              />
            ))}
          </div>
        </section>
      )}

      <EntityTable
        entities={cryptoAccounts}
        columns={columns}
        linkTo={(e: Entity) => `/accounts/${e.entity_id}`}
        emptyMessage="No crypto custody accounts found"
        columnVisibilityStorageKey="modelo-721"
        financialAccountDenomination
      />
    </div>
  )
}
