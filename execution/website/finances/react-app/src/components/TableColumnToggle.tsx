import { useMemo } from 'react'
import { Columns3 } from 'lucide-react'
import type { ColumnToggleDef } from '@/hooks/useTableColumnVisibility'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

/** Keys listed first in the menu (Account is the primary row label on Modelo / filings tables). */
const COLUMN_TOGGLE_PRIORITY_KEYS = ['account_name', 'account'] as const

function sortColumnsForToggle(columns: ColumnToggleDef[]): ColumnToggleDef[] {
  const priority: ColumnToggleDef[] = []
  const seen = new Set<string>()
  for (const pk of COLUMN_TOGGLE_PRIORITY_KEYS) {
    const found = columns.find(c => c.key === pk)
    if (found) {
      priority.push(found)
      seen.add(found.key)
    }
  }
  return [...priority, ...columns.filter(c => !seen.has(c.key))]
}

/** shadcn/Radix dropdown for column visibility (replaces &lt;details&gt; checkboxes). */
export default function TableColumnToggle({
  columns,
  visible,
  onToggle,
  /** Keys that must stay visible when checked (cannot uncheck in the menu). */
  lockedVisibleKeys = [],
}: {
  columns: ColumnToggleDef[]
  visible: Record<string, boolean>
  onToggle: (key: string) => void
  lockedVisibleKeys?: readonly string[]
}) {
  const ordered = useMemo(() => sortColumnsForToggle(columns), [columns])
  const locked = useMemo(() => new Set(lockedVisibleKeys), [lockedVisibleKeys])

  return (
    <div className="mb-3">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="outline" size="sm" className="gap-2">
            <Columns3 className="h-4 w-4 shrink-0" aria-hidden />
            Columns
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-56">
          <DropdownMenuLabel>Visible columns</DropdownMenuLabel>
          <DropdownMenuSeparator />
          {ordered.map(col => {
            const checked = visible[col.key] !== false
            return (
              <DropdownMenuCheckboxItem
                key={col.key}
                checked={checked}
                disabled={locked.has(col.key) && checked}
                onCheckedChange={next => {
                  if (next !== checked) onToggle(col.key)
                }}
              >
                {col.label}
              </DropdownMenuCheckboxItem>
            )
          })}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
