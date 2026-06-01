import { useState, useMemo, useEffect, useCallback, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import type { Entity } from '@/types/neotoma'
import { snapshotField } from '@/lib/formatters'
import { handleRowAuxClick, handleRowClickNavigate, handleRowKeyNavigate } from '@/lib/spaNavigation'
import { cn } from '@/lib/utils'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Input } from '@/components/ui/input'
import TableColumnToggle from '@/components/TableColumnToggle'
import WorkflowStatusBadge from '@/components/WorkflowStatusBadge'
import { useMaskMode } from '@/context/MaskModeContext'
import { useTableColumnVisibility } from '@/hooks/useTableColumnVisibility'
import { entityDisplayName } from '@/lib/humanize'
import { getAccountDenomination, getAccountDenominationSortOrder, type AccountDenominationKind } from '@/lib/accountDenomination'
import { DenominationBadge } from '@/components/DenominationBadge'
import FinancialDenominationFilter from '@/components/FinancialDenominationFilter'

export interface Column {
  key: string
  label: string
  render?: (value: unknown, entity: Entity) => ReactNode
  sortable?: boolean
  className?: string
  /** When sorting by this column, use this instead of {@link snapshotField}(snapshot, key). */
  sortAccessor?: (entity: Entity) => unknown
  /** Text accessor for filtering — returns the searchable string for this column. */
  filterAccessor?: (entity: Entity) => string
}

interface Props {
  entities: Entity[]
  columns: Column[]
  linkTo?: (entity: Entity) => string
  emptyMessage?: string
  /** When set, column visibility is persisted under `finances-table-cols:{key}` */
  columnVisibilityStorageKey?: string
  /** Comma-separated column keys hidden by default (e.g. `registry,assets,rows`) */
  defaultHiddenColumnKeysCsv?: string
  /** Comma-separated keys always visible and not hideable (e.g. `account_name` on Modelo 720) */
  columnEnsureVisibleKeysCsv?: string
  /** Initial sort column key (e.g. `account_value`); must match a column `key` */
  defaultSortKey?: string | null
  /** Initial sort direction when `defaultSortKey` is set */
  defaultSortDir?: 'asc' | 'desc'
  /** Column keys whose cell values render as human-readable workflow status badges */
  workflowStatusColumnKeys?: string[]
  /** Insert denomination badge column + filter for lists that include `financial_account` rows */
  financialAccountDenomination?: boolean
}

function withFinancialAccountDenominationColumns(columns: Column[]): Column[] {
  if (columns.some((c) => c.key === 'denomination')) return columns
  const denCol: Column = {
    key: 'denomination',
    label: 'Denomination',
    sortAccessor: (e) => getAccountDenominationSortOrder(e),
    filterAccessor: (e) => {
      if (e.entity_type !== 'financial_account') return ''
      const d = getAccountDenomination(e)
      return `${d.label} ${d.kind} ${d.detail}`.toLowerCase()
    },
    render: (_v, e) => <DenominationBadge entity={e} />,
  }
  const idx = columns.findIndex((c) => c.key === 'institution')
  if (idx >= 0) return [...columns.slice(0, idx + 1), denCol, ...columns.slice(idx + 1)]
  const idx2 = columns.findIndex((c) => c.key === 'account_name' || c.key === 'account')
  if (idx2 >= 0) return [...columns.slice(0, idx2 + 1), denCol, ...columns.slice(idx2 + 1)]
  return [denCol, ...columns]
}

function sortValueForColumn(columns: Column[], sortKey: string, entity: Entity): unknown {
  const col = columns.find(c => c.key === sortKey)
  if (col?.sortAccessor) return col.sortAccessor(entity)
  return snapshotField(entity.snapshot, sortKey) ?? ''
}

export default function EntityTable({
  entities,
  columns,
  linkTo,
  emptyMessage = 'No data',
  columnVisibilityStorageKey,
  defaultHiddenColumnKeysCsv = '',
  columnEnsureVisibleKeysCsv = '',
  defaultSortKey = null,
  defaultSortDir = 'asc',
  workflowStatusColumnKeys = [],
  financialAccountDenomination = false,
}: Props) {
  const navigate = useNavigate()
  const { enabled: maskOn, text: maskText } = useMaskMode()
  const statusBadgeKeys = useMemo(() => new Set(workflowStatusColumnKeys), [workflowStatusColumnKeys])
  const [sortKey, setSortKey] = useState<string | null>(() => defaultSortKey ?? null)
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>(() => defaultSortDir)
  const [filterText, setFilterText] = useState('')
  const [denominationKindFilter, setDenominationKindFilter] = useState<AccountDenominationKind | 'all'>('all')

  const hasFinancialAccounts = useMemo(
    () => entities.some((e) => e.entity_type === 'financial_account'),
    [entities],
  )

  const columnsResolved = useMemo(() => {
    if (!financialAccountDenomination || !hasFinancialAccounts) return columns
    return withFinancialAccountDenominationColumns(columns)
  }, [columns, financialAccountDenomination, hasFinancialAccounts])

  const colToggleDefs = useMemo(
    () => columnsResolved.map((c) => ({ key: c.key, label: c.label })),
    [columnsResolved],
  )

  const lockedVisibleKeys = useMemo(
    () =>
      columnEnsureVisibleKeysCsv
        .split(',')
        .map(s => s.trim())
        .filter(Boolean),
    [columnEnsureVisibleKeysCsv],
  )

  const { visible, toggle, enabled: columnToggleEnabled } = useTableColumnVisibility(
    columnVisibilityStorageKey,
    colToggleDefs,
    defaultHiddenColumnKeysCsv,
    columnEnsureVisibleKeysCsv,
  )

  const displayedColumns = useMemo(
    () => columnsResolved.filter((c) => visible[c.key] !== false),
    [columnsResolved, visible],
  )

  useEffect(() => {
    if (sortKey && !displayedColumns.some(c => c.key === sortKey)) {
      setSortKey(null)
    }
  }, [displayedColumns, sortKey])

  const afterDenominationFilter = useMemo(() => {
    if (!financialAccountDenomination || denominationKindFilter === 'all') return entities
    return entities.filter((entity) => {
      if (entity.entity_type !== 'financial_account') return true
      return getAccountDenomination(entity).kind === denominationKindFilter
    })
  }, [entities, financialAccountDenomination, denominationKindFilter])

  const filtered = useMemo(() => {
    if (!filterText.trim()) return afterDenominationFilter
    const q = filterText.toLowerCase()
    return afterDenominationFilter.filter((entity) => {
      for (const col of columnsResolved) {
        if (col.filterAccessor) {
          if (col.filterAccessor(entity).toLowerCase().includes(q)) return true
        } else {
          const v = snapshotField(entity.snapshot, col.key)
          if (v != null && String(v).toLowerCase().includes(q)) return true
        }
      }
      if (entity.canonical_name?.toLowerCase().includes(q)) return true
      if (entityDisplayName(entity).toLowerCase().includes(q)) return true
      return false
    })
  }, [afterDenominationFilter, filterText, columnsResolved])

  const sorted = useMemo(() => {
    if (!sortKey) return filtered
    return [...filtered].sort((a, b) => {
      const va = sortValueForColumn(columnsResolved, sortKey, a)
      const vb = sortValueForColumn(columnsResolved, sortKey, b)
      const cmp =
        typeof va === 'number' && typeof vb === 'number'
          ? va - vb
          : String(va).localeCompare(String(vb))
      return sortDir === 'asc' ? cmp : -cmp
    })
  }, [filtered, sortKey, sortDir, columnsResolved])

  const handleSort = useCallback((key: string) => {
    setSortKey(prev => {
      if (prev === key) {
        setSortDir(d => (d === 'asc' ? 'desc' : 'asc'))
        return key
      }
      setSortDir('asc')
      return key
    })
  }, [])

  if (entities.length === 0) {
    return <p className="text-muted-foreground text-sm py-8 text-center">{emptyMessage}</p>
  }

  const denominationFilterActive = financialAccountDenomination && denominationKindFilter !== 'all'
  const showFilter = entities.length > 5 || (financialAccountDenomination && hasFinancialAccounts)

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {showFilter && (
          <Input
            placeholder="Filter..."
            value={filterText}
            onChange={e => setFilterText(e.target.value)}
            className="max-w-xs h-8 text-sm"
          />
        )}
        {financialAccountDenomination && hasFinancialAccounts && (
          <FinancialDenominationFilter
            value={denominationKindFilter}
            onChange={setDenominationKindFilter}
            triggerClassName="h-8 w-[min(100vw-2rem,200px)] text-sm"
          />
        )}
        {columnToggleEnabled && (
          <TableColumnToggle
            columns={colToggleDefs}
            visible={visible}
            onToggle={toggle}
            lockedVisibleKeys={lockedVisibleKeys}
          />
        )}
      </div>
      <div className="rounded-lg border border-border overflow-hidden">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border bg-muted/50 hover:bg-muted/50">
              {displayedColumns.map(col => (
                <TableHead
                  key={col.key}
                  title={col.key}
                  className={cn(
                    'h-auto py-3 font-medium text-muted-foreground',
                    col.sortable !== false && 'cursor-pointer select-none hover:text-foreground',
                    col.className,
                  )}
                  onClick={() => col.sortable !== false && handleSort(col.key)}
                >
                  <span className="flex items-center gap-1">
                    {col.label}
                    {sortKey === col.key && <span className="text-xs">{sortDir === 'asc' ? '↑' : '↓'}</span>}
                  </span>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {sorted.map(entity => {
              const href = linkTo?.(entity)
              return (
                <TableRow
                  key={entity.entity_id}
                  className={cn(
                    'border-b border-border last:border-0',
                    linkTo && 'hover:bg-muted/30 cursor-pointer',
                  )}
                  onClick={href ? e => handleRowClickNavigate(navigate, href, e) : undefined}
                  onAuxClick={href ? e => handleRowAuxClick(href, e) : undefined}
                  onKeyDown={href ? e => handleRowKeyNavigate(navigate, href, e) : undefined}
                  tabIndex={linkTo ? 0 : undefined}
                  role={linkTo ? 'link' : undefined}
                >
                  {displayedColumns.map(col => {
                    const val = snapshotField(entity.snapshot, col.key)
                    const showStatusBadge = statusBadgeKeys.has(col.key)
                    return (
                      <TableCell key={col.key} className={cn('py-3', col.className)}>
                        {col.render ? (
                          col.render(val, entity)
                        ) : showStatusBadge ? (
                          <WorkflowStatusBadge value={val} maskOn={maskOn} text={maskText} />
                        ) : val != null ? (
                          String(val)
                        ) : (
                          '—'
                        )}
                      </TableCell>
                    )
                  })}
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>
      {showFilter && (
        <p className="text-xs text-muted-foreground">
          {filterText.trim() || denominationFilterActive
            ? `${sorted.length} of ${afterDenominationFilter.length} records${
                denominationFilterActive && afterDenominationFilter.length !== entities.length
                  ? ` (${entities.length} total)`
                  : ''
              }`
            : `${entities.length} records`}
        </p>
      )}
    </div>
  )
}
