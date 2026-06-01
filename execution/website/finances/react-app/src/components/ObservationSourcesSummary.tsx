import { useMemo } from 'react'
import type { Observation } from '@/types/neotoma'
import { useMaskMode } from '@/context/MaskModeContext'
import { humanizeSource } from '@/lib/humanize'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import ViewSourceButton from '@/components/ViewSourceButton'

interface Props {
  observations: Observation[]
}

interface SourceRow {
  label: string
  count: number
  sourceId: string | null
  sourceHint: string | null
}

function observationSourceHint(o: Observation): string | null {
  const d = o.data
  if (!d || typeof d !== 'object' || Array.isArray(d)) return null
  const rec = d as Record<string, unknown>
  const fields = ['statement_pdf_path', 'source_file', 'import_source_file', 'assets_sheet_source_file']
  for (const k of fields) {
    const v = rec[k]
    if (typeof v === 'string' && v.trim()) return v.trim()
  }
  return null
}

export default function ObservationSourcesSummary({ observations }: Props) {
  const { enabled: maskOn, text, maskNumber } = useMaskMode()

  const rows = useMemo<SourceRow[]>(() => {
    const m = new Map<string, { count: number; sourceId: string | null; sourceHint: string | null }>()
    for (const o of observations) {
      const key = o.source?.trim() || (o.source_id ? 'stored_source_file' : '')
      const hint = observationSourceHint(o)
      const existing = m.get(key)
      if (existing) {
        existing.count++
        if (!existing.sourceId && o.source_id) existing.sourceId = o.source_id
        if (!existing.sourceHint && hint) existing.sourceHint = hint
      } else {
        m.set(key, { count: 1, sourceId: o.source_id ?? null, sourceHint: hint })
      }
    }
    return [...m.entries()]
      .map(([label, { count, sourceId, sourceHint }]) => ({ label, count, sourceId, sourceHint }))
      .sort((a, b) => b.count - a.count)
  }, [observations])

  if (rows.length === 0) return null

  const hasAnySources = rows.some(r => r.sourceId)

  return (
    <Card>
      <CardHeader className="pb-2">
        <h2 className="text-lg font-medium">Data sources</h2>
        <p className="text-sm text-muted-foreground">
          How this record's data was collected ({maskOn ? maskNumber(observations.length, 'src-tot') : observations.length} updates total).
        </p>
      </CardHeader>
      <CardContent className="pt-0">
        <div className="rounded-md border border-border overflow-hidden">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="h-10">Source</TableHead>
                <TableHead className="h-10 text-right w-28">Updates</TableHead>
                {hasAnySources && <TableHead className="h-10 w-28" />}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map(row => (
                <TableRow key={row.label || '__no_source__'}>
                  <TableCell
                    className="text-sm"
                    title={row.label || undefined}
                  >
                    {maskOn
                      ? text(humanizeSource(row.label || null))
                      : humanizeSource(row.label || null)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {maskOn ? maskNumber(row.count, `src:${row.label || 'none'}`) : row.count}
                  </TableCell>
                  {hasAnySources && (
                    <TableCell className="text-right">
                      <ViewSourceButton sourceId={row.sourceId} sourceHint={row.sourceHint} />
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  )
}
