import { useMemo } from 'react'
import { useEntitiesByType } from '@/hooks/useEntities'
import { useFxRate } from '@/context/FxRateContext'
import { useMaskMode } from '@/context/MaskModeContext'
import { MonetaryPair } from '@/components/MonetaryPair'
import { snapshotField } from '@/lib/formatters'
import EntityTable, { type Column } from '@/components/EntityTable'
import CategoryPieChart from '@/components/CategoryPieChart'
import { Card, CardContent } from '@/components/ui/card'

export default function RecurringExpenses() {
  const { data, isLoading, error } = useEntitiesByType('recurring_expense')
  const { usdPerEur } = useFxRate()
  const { text, maskNumber } = useMaskMode()

  const columns = useMemo<Column[]>(
    () => [
      {
        key: 'name',
        label: 'Expense',
        render: (v, e) => (
          <span className="font-medium">
            {text(String(v ?? snapshotField<string>(e.snapshot, 'provider') ?? e.canonical_name ?? '—'))}
          </span>
        ),
      },
      { key: 'expense_type', label: 'Type', render: v => text(String(v ?? '—')) },
      { key: 'category', label: 'Category', render: v => text(String(v ?? '—')) },
      { key: 'billing_frequency', label: 'Frequency', render: v => text(String(v ?? '—')) },
      {
        key: 'amount_eur',
        label: 'Amount (display · stored)',
        render: (v, e) => {
          const raw = (v ?? snapshotField<number>(e.snapshot, 'amount')) as number
          if (raw == null) return '—'
          return (
            <MonetaryPair
              canonicalEur={raw}
              usdPerEur={usdPerEur}
              entity={e}
              pairKey={`rex-amt-${e.entity_id}`}
              layout="inline"
              showConversion={false}
            />
          )
        },
        className: 'text-right',
      },
      {
        key: 'yearly_total_eur',
        label: 'Yearly (display · stored)',
        render: (v, e) => {
          const raw = v as number
          if (raw == null) return '—'
          return (
            <MonetaryPair
              canonicalEur={raw}
              usdPerEur={usdPerEur}
              entity={e}
              pairKey={`rex-yr-${e.entity_id}`}
              layout="inline"
              showConversion={false}
              primaryClassName="font-medium"
            />
          )
        },
        className: 'text-right',
      },
    ],
    [text, usdPerEur],
  )

  const totalYearly = data
    ? data.entities.reduce((sum, e) => sum + (snapshotField<number>(e.snapshot, 'yearly_total_eur') ?? 0), 0)
    : 0

  const categoryData = useMemo(() => {
    if (!data) return []
    const map = new Map<string, number>()
    for (const entity of data.entities) {
      const cat = snapshotField<string>(entity.snapshot, 'category') || 'other'
      const val = snapshotField<number>(entity.snapshot, 'yearly_total_eur') ?? 0
      map.set(cat, (map.get(cat) || 0) + val)
    }
    return Array.from(map.entries())
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value)
  }, [data])

  const frequencyData = useMemo(() => {
    if (!data) return []
    const map = new Map<string, number>()
    for (const entity of data.entities) {
      const freq = snapshotField<string>(entity.snapshot, 'billing_frequency') || 'unknown'
      const val = snapshotField<number>(entity.snapshot, 'yearly_total_eur') ?? 0
      map.set(freq, (map.get(freq) || 0) + val)
    }
    return Array.from(map.entries())
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value)
  }, [data])

  const n = data?.entities.length ?? 0

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Recurring expenses</h1>
        <div className="text-muted-foreground text-sm mt-1 flex flex-wrap items-end gap-x-2 gap-y-1">
          <span>
            {data ? `${maskNumber(n, 'rex-n')} expense${n !== 1 ? 's' : ''}` : 'Loading...'}
            {data ? ' · ' : ''}
          </span>
          {data ? (
            <>
              <MonetaryPair
              canonicalEur={totalYearly}
              usdPerEur={usdPerEur}
              pairKey="rex-tot-yr"
              align="left"
              layout="inline"
            />
              <span className="self-end pb-0.5">/yr</span>
            </>
          ) : null}
        </div>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground animate-pulse">Loading...</p>}
      {error && <p className="text-sm text-destructive">Error: {(error as Error).message}</p>}

      {categoryData.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <Card>
            <CardContent className="pt-6">
              <CategoryPieChart data={categoryData} label="By category" />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-6">
              <CategoryPieChart data={frequencyData} label="By billing frequency" />
            </CardContent>
          </Card>
        </div>
      )}

      <EntityTable
        entities={data?.entities ?? []}
        columns={columns}
        emptyMessage="No recurring expenses found"
        columnVisibilityStorageKey="recurring-expenses"
      />
    </div>
  )
}
