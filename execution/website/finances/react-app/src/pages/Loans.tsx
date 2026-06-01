import { useMemo } from 'react'
import { useEntitiesByType } from '@/hooks/useEntities'
import { useFxRate } from '@/context/FxRateContext'
import { useMaskMode } from '@/context/MaskModeContext'
import { MonetaryPair } from '@/components/MonetaryPair'
import { formatDate, formatPercent, snapshotField } from '@/lib/formatters'
import type { Entity } from '@/types/neotoma'
import EntityTable, { type Column } from '@/components/EntityTable'

export default function Loans() {
  const { data, isLoading, error } = useEntitiesByType('loan')
  const { usdPerEur } = useFxRate()
  const { text, freeform, maskNumber, maskPercent, enabled: maskOn } = useMaskMode()

  const columns = useMemo<Column[]>(() => [
    {
      key: 'lender',
      label: 'Lender',
      render: (v) => <span className="font-medium">{text(String(v ?? '—'))}</span>,
    },
    { key: 'loan_type', label: 'Type', render: (v) => text(String(v ?? '—')) },
    {
      key: 'apr',
      label: 'APR',
      render: (v) =>
        formatPercent(v == null ? undefined : maskOn ? maskPercent(v as number, 'apr') : (v as number)),
      className: 'text-right',
    },
    {
      key: 'outstanding_principal_eur',
      label: 'Outstanding (display · stored)',
      render: (v, e) => {
        const raw = (v ?? snapshotField<number>(e.snapshot, 'outstanding_principal')) as number
        if (raw == null) return '—'
        return (
          <MonetaryPair
            canonicalEur={raw}
            usdPerEur={usdPerEur}
            entity={e}
            pairKey={`loan-out-${e.entity_id}`}
            layout="inline"
            showConversion={false}
          />
        )
      },
      className: 'text-right',
    },
    {
      key: 'monthly_payment_eur',
      label: 'Monthly (display · stored)',
      render: (v, e) => {
        const raw = (v ?? snapshotField<number>(e.snapshot, 'monthly_payment')) as number
        if (raw == null) return '—'
        return (
          <MonetaryPair
            canonicalEur={raw}
            usdPerEur={usdPerEur}
            entity={e}
            pairKey={`loan-mo-${e.entity_id}`}
            layout="inline"
            showConversion={false}
          />
        )
      },
      className: 'text-right',
    },
    {
      key: 'maturity_date',
      label: 'Maturity',
      render: (v) => (maskOn ? freeform(String(v ?? '')) : formatDate(v as string)),
    },
    { key: 'secured_property', label: 'Property', render: (v) => text(String(v ?? '—')) },
  ], [text, freeform, maskOn, maskPercent, usdPerEur])

  const totalOutstanding = data
    ? data.entities.reduce((sum, e) => {
        return sum + (snapshotField<number>(e.snapshot, 'outstanding_principal_eur') ??
          snapshotField<number>(e.snapshot, 'outstanding_principal') ?? 0)
      }, 0)
    : 0

  const n = data?.entities.length ?? 0

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Loans</h1>
        <div className="text-muted-foreground text-sm mt-1 flex flex-wrap items-end gap-x-2 gap-y-1">
          <span>
            {data ? `${maskNumber(n, 'loans-n')} loan${n !== 1 ? 's' : ''}` : 'Loading...'} &middot; outstanding
          </span>
          {data ? (
            <MonetaryPair
              canonicalEur={totalOutstanding}
              usdPerEur={usdPerEur}
              pairKey="loans-tot-out"
              layout="inline"
            />
          ) : null}
        </div>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground animate-pulse">Loading...</p>}
      {error && <p className="text-sm text-destructive">Error: {(error as Error).message}</p>}

      <EntityTable
        entities={data?.entities ?? []}
        columns={columns}
        linkTo={(e: Entity) => `/accounts/${e.entity_id}`}
        emptyMessage="No loans found"
        columnVisibilityStorageKey="loans"
      />
    </div>
  )
}
