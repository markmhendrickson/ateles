import { useMemo } from 'react'
import { useEntitiesByType } from '@/hooks/useEntities'
import { useMaskMode } from '@/context/MaskModeContext'
import { formatDate, snapshotField } from '@/lib/formatters'
import { humanizeWorkflowStatus } from '@/lib/humanize'
import EntityTable, { type Column } from '@/components/EntityTable'
import { Badge } from '@/components/ui/badge'
import { applyModeloWorkbookQ4Overlay, isModeloWorkbookImportAccount } from '@/lib/modeloWorkbookQ4Overlay'

export default function FilingsIndex() {
  const filings = useEntitiesByType('tax_filing', { sort_by: 'last_observation_at', sort_order: 'desc', limit: 100 })
  const accounts = useEntitiesByType('financial_account', { limit: 1000 })
  const { text, freeform, maskNumber, enabled: maskOn } = useMaskMode()

  const q4ByFilingId = useMemo(() => {
    const accountsRows = accounts.data?.entities ?? []
    const out = new Map<
      string,
      { total: number; tracked: number; variance: number; missing: number; status: 'aligned' | 'variance' | 'missing' | 'no_data' | 'not_applicable' }
    >()
    for (const filing of filings.data?.entities ?? []) {
      const filingId = filing.entity_id
      const formCode = String(snapshotField<string>(filing.snapshot, 'form_code') ?? '').trim()
      const taxYear = Number(snapshotField<number | string>(filing.snapshot, 'tax_year') ?? NaN)
      if (formCode !== '720' || !Number.isFinite(taxYear)) {
        out.set(filingId, { total: 0, tracked: 0, variance: 0, missing: 0, status: 'not_applicable' })
        continue
      }
      const matchedRaw = accountsRows.filter((account) => {
        const tags = snapshotField<string[]>(account.snapshot, 'filing_tags')
        if (!tags?.includes('720')) return false
        const y = snapshotField<number | string>(account.snapshot, 'tax_year_context')
        if (y == null) return false
        return Number(y) === taxYear
      })
      const matched = matchedRaw.filter((a) => !isModeloWorkbookImportAccount(a))
      const overlayed = matched.map((a) => applyModeloWorkbookQ4Overlay(a, accountsRows, taxYear))
      const total = overlayed.length
      const tracked = overlayed.filter((account) => snapshotField<number>(account.snapshot, 'q4_average_balance_eur') != null).length
      const variance = overlayed.filter((account) => snapshotField<string>(account.snapshot, 'q4_reconciliation_status') === 'variance').length
      const missing = overlayed.filter((account) => {
        const status = snapshotField<string>(account.snapshot, 'q4_reconciliation_status')
        const q4 = snapshotField<number>(account.snapshot, 'q4_average_balance_eur')
        return status === 'missing_q4_average' || q4 == null
      }).length
      const status =
        total === 0 ? 'no_data' :
        variance > 0 ? 'variance' :
        missing > 0 ? 'missing' :
        'aligned'
      out.set(filingId, { total, tracked, variance, missing, status })
    }
    return out
  }, [accounts.data?.entities, filings.data?.entities])

  const filingsByYearDesc = useMemo(() => {
    const rows = [...(filings.data?.entities ?? [])]
    rows.sort((a, b) => {
      const ay = Number(snapshotField<number | string>(a.snapshot, 'tax_year') ?? -Infinity)
      const by = Number(snapshotField<number | string>(b.snapshot, 'tax_year') ?? -Infinity)
      if (ay !== by) return by - ay

      const ad = snapshotField<string>(a.snapshot, 'filed_at') ?? a.last_observation_at ?? ''
      const bd = snapshotField<string>(b.snapshot, 'filed_at') ?? b.last_observation_at ?? ''
      return bd.localeCompare(ad)
    })
    return rows
  }, [filings.data?.entities])

  const filingColumns = useMemo<Column[]>(() => [
    {
      key: 'title',
      label: 'Filing',
      render: (_v, entity) => {
        const title = snapshotField<string>(entity.snapshot, 'title') ?? entity.canonical_name ?? entity.entity_id
        return <span className="font-medium">{maskOn ? text(String(title)) : String(title)}</span>
      },
      sortAccessor: entity => snapshotField<string>(entity.snapshot, 'title') ?? entity.canonical_name ?? entity.entity_id,
    },
    {
      key: 'form_code',
      label: 'Form',
      className: 'w-24',
      render: value => (maskOn ? text(String(value ?? '—')) : String(value ?? '—')),
    },
    {
      key: 'tax_year',
      label: 'Year',
      className: 'w-24',
      render: value => (maskOn ? maskNumber(Number(value ?? 0), 'filing-year') : String(value ?? '—')),
    },
    {
      key: 'status',
      label: 'Status',
      filterAccessor: entity => {
        const raw = snapshotField(entity.snapshot, 'status')
        const s = raw == null ? '' : String(raw)
        return `${s} ${humanizeWorkflowStatus(s)}`.toLowerCase()
      },
    },
    {
      key: 'q4_reconciliation',
      label: 'Q4 reconcile',
      sortAccessor: (entity) => {
        const s = q4ByFilingId.get(entity.entity_id)
        if (!s) return 99
        if (s.status === 'variance') return 0
        if (s.status === 'missing') return 1
        if (s.status === 'aligned') return 2
        if (s.status === 'no_data') return 3
        return 4
      },
      filterAccessor: (entity) => {
        const s = q4ByFilingId.get(entity.entity_id)
        if (!s) return ''
        return `${s.status} ${s.tracked}/${s.total} ${s.variance} ${s.missing}`.toLowerCase()
      },
      render: (_value, entity) => {
        const s = q4ByFilingId.get(entity.entity_id)
        if (!s || s.status === 'not_applicable') return <span className="text-muted-foreground text-xs">N/A</span>
        if (s.status === 'no_data') return <span className="text-muted-foreground text-xs">No 720 rows</span>

        const badgeText =
          s.status === 'aligned' ? 'Aligned' :
          s.status === 'variance' ? 'Variance' :
          'Missing'
        const badgeClass =
          s.status === 'aligned'
            ? 'border-0 bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300'
            : s.status === 'variance'
              ? 'border-0 bg-rose-100 text-rose-800 dark:bg-rose-900/30 dark:text-rose-300'
              : 'border-0 bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300'
        const counts = `${maskOn ? maskNumber(s.tracked, `q4-tracked-${entity.entity_id}`) : s.tracked}/${maskOn ? maskNumber(s.total, `q4-total-${entity.entity_id}`) : s.total}`
        const varianceCount = maskOn ? maskNumber(s.variance, `q4-var-${entity.entity_id}`) : s.variance

        return (
          <div className="flex flex-col gap-1">
            <Badge variant="outline" className={`w-fit rounded-full px-2 py-0.5 font-medium ${badgeClass}`}>
              {text(badgeText)}
            </Badge>
            <span className="text-xs text-muted-foreground">{text(`${counts} tracked · ${varianceCount} var`)}</span>
          </div>
        )
      },
    },
    {
      key: 'filing_authority',
      label: 'Authority',
      render: value => (maskOn ? text(String(value ?? '—')) : String(value ?? '—')),
    },
    {
      key: 'filed_at',
      label: 'Filed',
      render: value => (maskOn ? freeform(String(value ?? '')) : formatDate(value as string)),
      sortAccessor: entity => snapshotField<string>(entity.snapshot, 'filed_at') ?? entity.last_observation_at ?? '',
    },
  ], [freeform, maskNumber, maskOn, q4ByFilingId, text])

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-end gap-x-2 gap-y-1">
        <h2 className="text-lg font-medium">All filings</h2>
        <span className="text-sm text-muted-foreground">
          {filings.data
            ? `${maskOn ? maskNumber(filings.data.total, 'filing-count') : filings.data.total} filing${filings.data.total !== 1 ? 's' : ''}`
            : 'Loading...'}
        </span>
      </div>

      <p className="text-sm text-muted-foreground">
        Click a filing to see its full details, linked accounts, and history.
      </p>

      {filings.isLoading && <p className="text-sm text-muted-foreground animate-pulse">Loading filings...</p>}
      {accounts.isLoading && <p className="text-sm text-muted-foreground animate-pulse">Loading account reconciliation…</p>}
      {filings.error && <p className="text-sm text-destructive">Error loading filings: {(filings.error as Error).message}</p>}
      {accounts.error && <p className="text-sm text-destructive">Error loading accounts: {(accounts.error as Error).message}</p>}

      <EntityTable
        entities={filingsByYearDesc}
        columns={filingColumns}
        linkTo={entity => `/filings/${entity.entity_id}`}
        emptyMessage="No filings recorded yet."
        columnVisibilityStorageKey="tax-filings"
        workflowStatusColumnKeys={['status']}
      />
    </section>
  )
}
