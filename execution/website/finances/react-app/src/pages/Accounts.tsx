import { useMemo, useState, useCallback, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { handleRowAuxClick, handleRowClickNavigate, handleRowKeyNavigate } from '@/lib/spaNavigation'
import { useEntitiesByType } from '@/hooks/useEntities'
import { useMaskMode } from '@/context/MaskModeContext'
import { useEntityFxRates } from '@/hooks/useEntityFxRates'
import { useAlignedMaskedFx } from '@/hooks/useAlignedMaskedFx'
import {
  getEntityCanonicalEur,
  getEntityMonetaryDisplayBasisEur,
  totalNetWorthEur,
  totalNetWorthUsd,
  type UsdPerEurInput,
} from '@/lib/aggregations'
import {
  compareAccountDenomination,
  getAccountDenomination,
  type AccountDenominationKind,
} from '@/lib/accountDenomination'
import { formatDate, snapshotField } from '@/lib/formatters'
import DataCompletenessBar from '@/components/DataCompletenessBar'
import { getEntityFxAsOfDate } from '@/lib/entityFxDate'
import { AggregateMonetaryPair, MonetaryPair } from '@/components/MonetaryPair'
import type { Entity, SheetRow } from '@/types/neotoma'
import { cn } from '@/lib/utils'
import { entityDisplayName } from '@/lib/humanize'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import TableColumnToggle from '@/components/TableColumnToggle'
import { useTableColumnVisibility, type ColumnToggleDef } from '@/hooks/useTableColumnVisibility'
import { prepareFinancialAccountList } from '@/lib/financialAccountDedup'
import { Input } from '@/components/ui/input'
import { DenominationBadge } from '@/components/DenominationBadge'
import FinancialDenominationFilter from '@/components/FinancialDenominationFilter'

type SortKey = 'account' | 'denomination' | 'registry' | 'assets' | 'value' | 'rows' | 'asOf'

function getRowsSummary(entity: Entity) {
  const rows = snapshotField<SheetRow[]>(entity.snapshot, 'rows')
  if (!rows || rows.length === 0) return { assets: '—', count: 0 }
  const assets = rows.map(r => String(r['Asset'] ?? r['Description'] ?? '')).filter(Boolean)
  return { assets: assets.join(', ') || '—', count: rows.length }
}

function compareAccounts(
  a: Entity,
  b: Entity,
  key: SortKey,
  dir: 'asc' | 'desc',
  fx: UsdPerEurInput,
): number {
  const mult = dir === 'asc' ? 1 : -1
  switch (key) {
    case 'account': {
      const va = entityDisplayName(a).toLowerCase()
      const vb = entityDisplayName(b).toLowerCase()
      return mult * va.localeCompare(vb)
    }
    case 'denomination': {
      const ka = getAccountDenomination(a).kind
      const kb = getAccountDenomination(b).kind
      const c = compareAccountDenomination(ka, kb)
      if (c !== 0) return mult * c
      return mult * entityDisplayName(a).toLowerCase().localeCompare(entityDisplayName(b).toLowerCase())
    }
    case 'registry': {
      const va = String(snapshotField<string>(a.snapshot, 'registry_id') || '')
      const vb = String(snapshotField<string>(b.snapshot, 'registry_id') || '')
      return mult * va.localeCompare(vb)
    }
    case 'assets': {
      const aa = getRowsSummary(a).assets
      const bb = getRowsSummary(b).assets
      return mult * aa.localeCompare(bb)
    }
    case 'value':
      return mult * (getEntityMonetaryDisplayBasisEur(a, fx) - getEntityMonetaryDisplayBasisEur(b, fx))
    case 'rows':
      return mult * (getRowsSummary(a).count - getRowsSummary(b).count)
    case 'asOf': {
      const da = getEntityFxAsOfDate(a) || ''
      const db = getEntityFxAsOfDate(b) || ''
      return mult * da.localeCompare(db)
    }
    default:
      return 0
  }
}

const NUMERIC_SORT_KEYS: SortKey[] = ['value', 'rows']

const ACCOUNTS_COLUMN_DEFS: ColumnToggleDef[] = [
  { key: 'account', label: 'Account' },
  { key: 'denomination', label: 'Denomination' },
  { key: 'registry', label: 'Registry ID' },
  { key: 'assets', label: 'Assets' },
  { key: 'value', label: 'Value (display · stored)' },
  { key: 'rows', label: 'Rows' },
  { key: 'asOf', label: 'As of' },
]

const ACCOUNTS_DEFAULT_HIDDEN_CSV = 'registry,assets,rows'

function SortableTh({
  label,
  columnKey,
  activeKey,
  dir,
  onSort,
  align = 'left',
}: {
  label: string
  columnKey: SortKey
  activeKey: SortKey
  dir: 'asc' | 'desc'
  onSort: (k: SortKey) => void
  align?: 'left' | 'right'
}) {
  const active = activeKey === columnKey
  return (
    <TableHead
      scope="col"
      className={cn(
        'h-auto py-3 font-medium text-muted-foreground cursor-pointer select-none hover:text-foreground',
        align === 'right' && 'text-right',
        align === 'left' && 'text-left',
      )}
      onClick={() => onSort(columnKey)}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        {active && <span className="text-xs tabular-nums">{dir === 'asc' ? '↑' : '↓'}</span>}
      </span>
    </TableHead>
  )
}

export default function Accounts() {
  const navigate = useNavigate()
  const { data, isLoading, error } = useEntitiesByType('financial_account')
  const { enabled: maskOn, text, freeform, maskNumber } = useMaskMode()
  const { resolveUsdPerEur, latestUsdPerEur } = useEntityFxRates(data?.entities)

  const [sortKey, setSortKey] = useState<SortKey>('value')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [denominationFilter, setDenominationFilter] = useState<AccountDenominationKind | 'all'>('all')
  const [searchQuery, setSearchQuery] = useState('')

  const handleSort = useCallback(
    (k: SortKey) => {
      if (k === sortKey) {
        setSortDir(d => (d === 'asc' ? 'desc' : 'asc'))
      } else {
        setSortKey(k)
        setSortDir(NUMERIC_SORT_KEYS.includes(k) ? 'desc' : 'asc')
      }
    },
    [sortKey],
  )

  const entities = useMemo(() => (data ? prepareFinancialAccountList(data.entities) : []), [data])

  const filteredEntities = useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    return entities.filter((entity) => {
      if (denominationFilter !== 'all') {
        if (getAccountDenomination(entity).kind !== denominationFilter) return false
      }
      if (!q) return true
      const name = entityDisplayName(entity).toLowerCase()
      const reg = String(snapshotField<string>(entity.snapshot, 'registry_id') ?? '').toLowerCase()
      const inst = String(snapshotField<string>(entity.snapshot, 'institution') ?? '').toLowerCase()
      return name.includes(q) || reg.includes(q) || inst.includes(q)
    })
  }, [entities, denominationFilter, searchQuery])

  const sortedAccounts = useMemo(() => {
    const list = [...filteredEntities]
    list.sort((a, b) => compareAccounts(a, b, sortKey, sortDir, resolveUsdPerEur))
    return list
  }, [filteredEntities, sortKey, sortDir, resolveUsdPerEur])

  const totalCount = entities.length
  const filteredCount = filteredEntities.length
  const filtersActive = denominationFilter !== 'all' || searchQuery.trim().length > 0

  const netWorthEur = useMemo(
    () => totalNetWorthEur(filteredEntities, resolveUsdPerEur),
    [filteredEntities, resolveUsdPerEur],
  )
  const netWorthUsd = useMemo(
    () => totalNetWorthUsd(filteredEntities, resolveUsdPerEur),
    [filteredEntities, resolveUsdPerEur],
  )
  const nw = useAlignedMaskedFx(netWorthEur, netWorthUsd, 'accounts-nw', latestUsdPerEur)

  const { visible, toggle } = useTableColumnVisibility(
    'accounts',
    ACCOUNTS_COLUMN_DEFS,
    ACCOUNTS_DEFAULT_HIDDEN_CSV,
    'account',
  )

  useEffect(() => {
    if (sortKey && visible[sortKey] === false) {
      setSortKey('value')
      setSortDir('desc')
    }
  }, [visible, sortKey])

  const countLabel = !data
    ? ''
    : filtersActive
      ? `${maskOn ? maskNumber(filteredCount, 'acct-filtered') : filteredCount} of ${
          maskOn ? maskNumber(totalCount, 'acct-total') : totalCount
        } accounts`
      : `${maskOn ? maskNumber(totalCount, 'acct-count') : totalCount} financial accounts`

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Accounts</h1>
        <div className="text-muted-foreground text-sm mt-1 flex flex-wrap items-end gap-x-2 gap-y-1">
          <span>{data ? countLabel : 'Loading...'}</span>
          {!isLoading && data && (
            <>
              {filtersActive && (
                <span className="text-xs opacity-80">Net worth below matches the filtered set.</span>
              )}
              <AggregateMonetaryPair eurLabel={nw.eurLabel} usdLabel={nw.usdLabel} align="left" />
            </>
          )}
        </div>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground animate-pulse">Loading accounts...</p>}
      {error && <p className="text-sm text-destructive">Error: {(error as Error).message}</p>}

      {entities.length > 0 && (
        <div className="space-y-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
            <TableColumnToggle
              columns={ACCOUNTS_COLUMN_DEFS}
              visible={visible}
              onToggle={toggle}
              lockedVisibleKeys={['account']}
            />
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
              <FinancialDenominationFilter value={denominationFilter} onChange={setDenominationFilter} />
              <Input
                type="search"
                placeholder="Search name, registry, institution…"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="h-9 sm:w-64"
                aria-label="Filter accounts by text"
              />
            </div>
          </div>
          {sortedAccounts.length === 0 ? (
            <p className="text-sm text-muted-foreground">No accounts match the current filters.</p>
          ) : (
          <div className="rounded-lg border border-border overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border bg-muted/50 hover:bg-muted/50">
                  {visible.account !== false && (
                    <SortableTh label="Account" columnKey="account" activeKey={sortKey} dir={sortDir} onSort={handleSort} />
                  )}
                  {visible.denomination !== false && (
                    <SortableTh
                      label="Denomination"
                      columnKey="denomination"
                      activeKey={sortKey}
                      dir={sortDir}
                      onSort={handleSort}
                    />
                  )}
                  {visible.registry !== false && (
                    <SortableTh label="Registry ID" columnKey="registry" activeKey={sortKey} dir={sortDir} onSort={handleSort} />
                  )}
                  {visible.assets !== false && (
                    <SortableTh label="Assets" columnKey="assets" activeKey={sortKey} dir={sortDir} onSort={handleSort} />
                  )}
                  {visible.value !== false && (
                    <SortableTh
                      label="Value (display · stored)"
                      columnKey="value"
                      activeKey={sortKey}
                      dir={sortDir}
                      onSort={handleSort}
                      align="right"
                    />
                  )}
                  {visible.rows !== false && (
                    <SortableTh label="Rows" columnKey="rows" activeKey={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                  )}
                  {visible.asOf !== false && (
                    <SortableTh label="As of" columnKey="asOf" activeKey={sortKey} dir={sortDir} onSort={handleSort} />
                  )}
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedAccounts.map(entity => {
                  const { assets, count } = getRowsSummary(entity)
                  const canonicalEur = getEntityCanonicalEur(entity, resolveUsdPerEur)
                  const asOfIso = getEntityFxAsOfDate(entity)
                  const reg = snapshotField<string>(entity.snapshot, 'registry_id')
                  const name = entityDisplayName(entity)
                  const accountHref = `/accounts/${entity.entity_id}`
                  return (
                    <TableRow
                      key={entity.entity_id}
                      className="border-b border-border last:border-0 hover:bg-muted/30 cursor-pointer"
                      onClick={e => handleRowClickNavigate(navigate, accountHref, e)}
                      onAuxClick={e => handleRowAuxClick(accountHref, e)}
                      onKeyDown={e => handleRowKeyNavigate(navigate, accountHref, e)}
                      tabIndex={0}
                      role="link"
                    >
                      {visible.account !== false && (
                        <TableCell className="py-3 font-medium">
                          <span className="flex items-center gap-2">
                            {maskOn ? text(name) : name}
                            <DataCompletenessBar entityType="financial_account" snapshot={entity.snapshot} compact />
                          </span>
                        </TableCell>
                      )}
                      {visible.denomination !== false && (
                        <TableCell className="py-3">
                          <DenominationBadge entity={entity} />
                        </TableCell>
                      )}
                      {visible.registry !== false && (
                        <TableCell className="py-3 text-muted-foreground text-xs font-mono">
                          {reg ? (maskOn ? freeform(reg) : reg) : '—'}
                        </TableCell>
                      )}
                      {visible.assets !== false && (
                        <TableCell className="py-3 max-w-[200px] truncate text-muted-foreground">
                          {maskOn ? freeform(assets) : assets}
                        </TableCell>
                      )}
                      {visible.value !== false && (
                        <TableCell className={cn('py-3 text-right', canonicalEur !== 0 && '[&_.tabular-nums]:font-medium')}>
                          {canonicalEur !== 0 ? (
                            <MonetaryPair
                              canonicalEur={canonicalEur}
                              usdPerEur={resolveUsdPerEur(entity)}
                              entity={entity}
                              pairKey={`acct-row-${entity.entity_id}`}
                              layout="inline"
                              showConversion={false}
                            />
                          ) : (
                            '—'
                          )}
                        </TableCell>
                      )}
                      {visible.rows !== false && (
                        <TableCell className="py-3 text-right text-muted-foreground">
                          {maskOn ? maskNumber(count, `rows:${entity.entity_id}`) : count}
                        </TableCell>
                      )}
                      {visible.asOf !== false && (
                        <TableCell
                          className="py-3 text-muted-foreground"
                          title={asOfIso ? undefined : 'No valuation date available for this account'}
                        >
                          {asOfIso ? (maskOn ? freeform(asOfIso) : formatDate(asOfIso)) : '—'}
                        </TableCell>
                      )}
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
          )}
        </div>
      )}
    </div>
  )
}
