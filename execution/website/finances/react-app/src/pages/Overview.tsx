import { useMemo, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getStats } from '@/api/stats'
import { useEntitiesByType } from '@/hooks/useEntities'
import { useMaskedFormatters } from '@/hooks/useMaskedFormatters'
import { useMaskMode } from '@/context/MaskModeContext'
import { useEntityFxRates } from '@/hooks/useEntityFxRates'
import { useAlignedMaskedFx } from '@/hooks/useAlignedMaskedFx'
import { bucketNetWorthUsd, groupByStrategyBucket, totalNetWorthEur, totalNetWorthUsd } from '@/lib/aggregations'
import {
  denominationBadgeClass,
  getAccountDenomination,
  type AccountDenominationKind,
} from '@/lib/accountDenomination'
import { prepareFinancialAccountList } from '@/lib/financialAccountDedup'
import { humanizePropertyKey } from '@/lib/propertyLabels'
import StrategyBucketCard from '@/components/StrategyBucketCard'
import { AggregateMonetaryPair } from '@/components/MonetaryPair'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

export default function Overview() {
  const stats = useQuery({ queryKey: ['stats'], queryFn: getStats, staleTime: 60_000 })
  const accounts = useEntitiesByType('financial_account')
  const mf = useMaskedFormatters()
  const { enabled: maskOn, text, maskNumber } = useMaskMode()
  const { resolveUsdPerEur, latestUsdPerEur } = useEntityFxRates(accounts.data?.entities)

  const dedupedAccounts = useMemo(
    () => (accounts.data ? prepareFinancialAccountList(accounts.data.entities) : []),
    [accounts.data],
  )

  const denominationCounts = useMemo(() => {
    const out: Record<AccountDenominationKind, number> = {
      crypto: 0,
      fiat_cash: 0,
      investments: 0,
      mixed: 0,
      other: 0,
    }
    for (const e of dedupedAccounts) {
      out[getAccountDenomination(e).kind] += 1
    }
    return out
  }, [dedupedAccounts])

  const displayAccounts = accounts.data ? prepareFinancialAccountList(accounts.data.entities) : []
  const buckets = displayAccounts.length > 0 ? groupByStrategyBucket(displayAccounts, resolveUsdPerEur) : []
  const netWorthEur = displayAccounts.length > 0 ? totalNetWorthEur(displayAccounts, resolveUsdPerEur) : 0
  const netWorthUsd = displayAccounts.length > 0 ? totalNetWorthUsd(displayAccounts, resolveUsdPerEur) : 0
  const nw = useAlignedMaskedFx(netWorthEur, netWorthUsd, 'overview-nw', latestUsdPerEur)

  const typeCounts = stats.data?.entities_by_type || {}
  const entityTypeCounts = useMemo(() => {
    const rows = Object.entries(typeCounts)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 15)
      .map(([literalType, count]) => ({
        literalType,
        typeLabel: humanizePropertyKey(literalType),
        count,
      }))
    if (!maskOn) return rows
    return rows.map(r => ({
      ...r,
      typeLabel: text(r.typeLabel),
      count: maskNumber(r.count, `etype:${r.literalType}`),
    }))
  }, [typeCounts, maskOn, text, maskNumber])

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Financial overview</h1>
        <p className="text-muted-foreground text-sm mt-1">Current balances and portfolio summary</p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        <SummaryCard label="Net worth" loading={accounts.isLoading}>
          {!accounts.isLoading && (
            <AggregateMonetaryPair
              eurLabel={nw.eurLabel}
              usdLabel={nw.usdLabel}
              align="left"
              primaryClassName="text-2xl font-semibold tracking-tight"
            />
          )}
        </SummaryCard>
        <SummaryCard label="Accounts tracked" value={mf.num(stats.data?.total_entities)} loading={stats.isLoading} />
        <SummaryCard
          label="Data points"
          value={mf.num(stats.data?.total_observations)}
          loading={stats.isLoading}
        />
      </div>

      {buckets.length > 0 && (
        <section>
          <h2 className="text-lg font-medium mb-4">By strategy bucket</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {buckets.map(b => (
              <StrategyBucketCard
                key={b.bucket}
                bucket={b}
                totalNetWorthEur={netWorthEur}
                bucketTotalUsd={bucketNetWorthUsd(b, resolveUsdPerEur)}
              />
            ))}
          </div>
        </section>
      )}

      {dedupedAccounts.length > 0 && (
        <section>
          <h2 className="text-lg font-medium mb-3">By denomination</h2>
          <p className="text-sm text-muted-foreground mb-3">
            Account counts by inferred crypto, fiat cash, securities, mixed, or other (deduped by registry).
          </p>
          <div className="flex flex-wrap gap-2">
            {(
              [
                ['crypto', 'Crypto'],
                ['fiat_cash', 'Fiat'],
                ['investments', 'Securities'],
                ['mixed', 'Mixed'],
                ['other', 'Other'],
              ] as const
            ).map(([kind, label]) => (
              <Badge
                key={kind}
                variant="outline"
                className={cn('gap-1.5 font-normal px-2.5 py-1', denominationBadgeClass(kind))}
              >
                <span>{maskOn ? text(label) : label}</span>
                <span className="tabular-nums">
                  {maskOn ? maskNumber(denominationCounts[kind], `ov-denom-${kind}`) : denominationCounts[kind]}
                </span>
              </Badge>
            ))}
          </div>
        </section>
      )}

      {entityTypeCounts.length > 0 && (
        <section>
          <h2 className="text-lg font-medium mb-4">Records by type</h2>
          <Card>
            <CardContent className="pt-6 h-80">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={entityTypeCounts} layout="vertical" margin={{ left: 120, right: 16, top: 8, bottom: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-border" horizontal={false} />
                  <XAxis type="number" className="text-xs fill-muted-foreground" />
                  <YAxis
                    type="category"
                    dataKey="typeLabel"
                    className="text-xs fill-muted-foreground"
                    width={110}
                  />
                  <Tooltip
                    labelFormatter={(label) => String(label ?? '')}
                    contentStyle={{
                      backgroundColor: 'hsl(var(--card))',
                      border: '1px solid hsl(var(--border))',
                      borderRadius: '0.5rem',
                      color: 'hsl(var(--card-foreground))',
                    }}
                  />
                  <Bar dataKey="count" fill="hsl(var(--primary))" radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>
        </section>
      )}

      {(stats.isLoading || accounts.isLoading) && (
        <p className="text-sm text-muted-foreground animate-pulse">Loading...</p>
      )}
      {stats.error && (
        <p className="text-sm text-destructive">Error loading stats: {(stats.error as Error).message}</p>
      )}
    </div>
  )
}

function SummaryCard({
  label,
  value,
  loading,
  children,
}: {
  label: string
  value?: string
  loading: boolean
  children?: ReactNode
}) {
  return (
    <Card>
      <CardContent className="pt-6">
        <p className="text-xs text-muted-foreground mb-1">{label}</p>
        {loading ? (
          <Skeleton className="h-8 w-24" />
        ) : children ? (
          children
        ) : (
          <p className="text-2xl font-semibold tracking-tight">{value ?? '—'}</p>
        )}
      </CardContent>
    </Card>
  )
}
