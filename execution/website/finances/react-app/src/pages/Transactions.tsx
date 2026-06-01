import { useState, useMemo } from 'react'
import { useEntities } from '@/hooks/useEntities'
import { useFxRate } from '@/context/FxRateContext'
import { useMaskMode } from '@/context/MaskModeContext'
import { MonetaryPair } from '@/components/MonetaryPair'
import { formatDate, snapshotField } from '@/lib/formatters'
import EntityTable, { type Column } from '@/components/EntityTable'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'

const PAGE_SIZE = 50

export default function Transactions() {
  const [offset, setOffset] = useState(0)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const { usdPerEur } = useFxRate()
  const { text, freeform, maskNumber, enabled: maskOn } = useMaskMode()

  const columns = useMemo<Column[]>(
    () => [
      {
        key: 'date',
        label: 'Date',
        render: v => (maskOn ? freeform(String(v ?? '')) : formatDate(v as string)),
      },
      {
        key: 'description',
        label: 'Description',
        render: (v, e) => (
          <span className="font-medium">
            {text(String(v ?? snapshotField<string>(e.snapshot, 'counterparty') ?? '—'))}
          </span>
        ),
      },
      { key: 'category', label: 'Category', render: v => text(String(v ?? '—')) },
      { key: 'provider', label: 'Provider', render: v => text(String(v ?? '—')) },
      {
        key: 'amount_eur',
        label: 'Amount (display · stored)',
        render: (v, e) => {
          const val = (v ?? snapshotField<number>(e.snapshot, 'amount')) as number | undefined
          if (val == null) return '—'
          const ccy = String(snapshotField<string>(e.snapshot, 'currency') ?? '').toUpperCase()
          const canonicalEur = ccy === 'USD' && usdPerEur > 0 ? val / usdPerEur : val
          return (
            <MonetaryPair
              canonicalEur={canonicalEur}
              usdPerEur={usdPerEur}
              entity={e}
              pairKey={`tx-${e.entity_id}`}
              detailedEur
              layout="inline"
              showConversion={false}
              primaryClassName={val < 0 ? 'text-destructive' : undefined}
            />
          )
        },
        className: 'text-right',
      },
      { key: 'currency', label: 'Ccy', className: 'w-16', render: v => text(String(v ?? '—')) },
    ],
    [text, freeform, maskOn, usdPerEur],
  )

  const { data, isLoading, error } = useEntities({
    entity_type: 'transaction',
    include_snapshots: true,
    limit: PAGE_SIZE,
    offset,
    sort_by: 'updated_at',
    sort_order: 'desc',
  })

  const filtered = useMemo(() => {
    if (!data) return []
    return data.entities.filter(entity => {
      const d = snapshotField<string>(entity.snapshot, 'date')
      if (dateFrom && d && d < dateFrom) return false
      if (dateTo && d && d > dateTo) return false
      return true
    })
  }, [data, dateFrom, dateTo])

  const totalShown = data?.total ?? 0

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Transactions</h1>
        <p className="text-muted-foreground text-sm mt-1">
          {data ? `${maskNumber(totalShown, 'tx-tot')} total` : 'Loading...'} &middot; showing{' '}
          {maskNumber(offset + 1, 'tx-a')}–{maskNumber(offset + (filtered.length || 0), 'tx-b')}
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-1.5">
          <span className="text-sm text-muted-foreground">From</span>
          <Input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} className="w-[160px]" />
        </div>
        <div className="space-y-1.5">
          <span className="text-sm text-muted-foreground">To</span>
          <Input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} className="w-[160px]" />
        </div>
        {(dateFrom || dateTo) && (
          <Button variant="link" className="h-auto px-0 pb-0" onClick={() => { setDateFrom(''); setDateTo('') }}>
            Clear filters
          </Button>
        )}
      </div>

      {isLoading && <p className="text-sm text-muted-foreground animate-pulse">Loading...</p>}
      {error && <p className="text-sm text-destructive">Error: {(error as Error).message}</p>}

      <EntityTable
        entities={filtered}
        columns={columns}
        emptyMessage="No transactions found"
        columnVisibilityStorageKey="transactions"
      />

      {data && data.total > PAGE_SIZE && (
        <div className="flex items-center justify-between pt-2">
          <Button
            variant="outline"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            Previous
          </Button>
          <span className="text-sm text-muted-foreground">
            Page {maskNumber(Math.floor(offset / PAGE_SIZE) + 1, 'pg')} of{' '}
            {maskNumber(Math.ceil(data.total / PAGE_SIZE), 'pgmax')}
          </span>
          <Button
            variant="outline"
            disabled={offset + PAGE_SIZE >= data.total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  )
}
