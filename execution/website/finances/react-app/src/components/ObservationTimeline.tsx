import type { Observation } from '@/types/neotoma'
import { formatDate } from '@/lib/formatters'
import { useMaskMode } from '@/context/MaskModeContext'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { humanizePropertyKey } from '@/lib/propertyLabels'
import ViewSourceButton from '@/components/ViewSourceButton'

function observationSourceHint(obs: Observation): string | null {
  const d = obs.data
  if (!d || typeof d !== 'object' || Array.isArray(d)) return null
  const rec = d as Record<string, unknown>
  const fields = ['statement_pdf_path', 'source_file', 'import_source_file', 'assets_sheet_source_file']
  for (const k of fields) {
    const v = rec[k]
    if (typeof v === 'string' && v.trim()) return v.trim()
  }
  return null
}

interface Props {
  observations: Observation[]
  /** Scroll container for long lists */
  className?: string
}

export default function ObservationTimeline({ observations, className }: Props) {
  const { enabled: maskOn, text, freeform, deep } = useMaskMode()

  if (observations.length === 0) {
    return <p className="text-muted-foreground text-sm py-4">No observations</p>
  }

  const sorted = [...observations].sort(
    (a, b) =>
      new Date(b.observed_at || b.created_at).getTime() - new Date(a.observed_at || a.created_at).getTime(),
  )

  return (
    <div
      className={cn('max-h-[min(72vh,1400px)] overflow-y-auto space-y-3 pr-1', className)}
    >
      {sorted.map(obs => {
        const sourceLabel = obs.source?.trim()
          ? obs.source
          : obs.source_id
            ? 'stored source file'
            : '—'
        return (
          <Card key={obs.id}>
            <CardContent className="pt-4 pb-4">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between mb-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-muted-foreground">
                    {maskOn
                      ? freeform(String(obs.observed_at || obs.created_at || ''))
                      : formatDate(obs.observed_at || obs.created_at)}
                  </span>
                  <Badge variant="secondary" className="font-normal">
                    {maskOn ? text(sourceLabel === '—' ? 'unknown' : sourceLabel) : sourceLabel}
                  </Badge>
                  {obs.observation_kind && (
                    <Badge
                      variant="outline"
                      className="font-normal text-xs"
                      title={obs.observation_kind}
                    >
                      {maskOn
                        ? text(humanizePropertyKey(obs.observation_kind))
                        : humanizePropertyKey(obs.observation_kind)}
                    </Badge>
                  )}
                  {obs.source_id && (
                    <ViewSourceButton sourceId={obs.source_id} sourceHint={observationSourceHint(obs)} />
                  )}
                </div>
                <details className="text-[10px] text-muted-foreground">
                  <summary className="cursor-pointer hover:text-foreground transition-colors text-right">Details</summary>
                  <div className="flex flex-col items-start gap-0.5 font-mono break-all sm:text-right sm:items-end mt-1">
                    <span title="Record ID">{maskOn ? freeform(obs.id) : obs.id}</span>
                    {obs.idempotency_key && (
                      <span title="Import key">
                        {maskOn ? freeform(obs.idempotency_key) : obs.idempotency_key}
                      </span>
                    )}
                  </div>
                </details>
              </div>
              {(() => {
                const payload = obs.data
                const isPlainObject =
                  payload != null && typeof payload === 'object' && !Array.isArray(payload)
                const keys = isPlainObject ? Object.keys(payload as Record<string, unknown>) : []
                if (isPlainObject && keys.length > 0) {
                  return (
                    <pre className="text-xs overflow-x-auto whitespace-pre-wrap text-foreground/80">
                      {JSON.stringify(maskOn ? deep(payload) : payload, null, 2)}
                    </pre>
                  )
                }
                if (isPlainObject && keys.length === 0) {
                  return (
                    <p className="text-xs text-muted-foreground">
                      No data fields recorded in this update.
                    </p>
                  )
                }
                return (
                  <p className="text-xs text-muted-foreground">
                    Metadata-only update — no field values included. The current record state reflects all prior updates.
                  </p>
                )
              })()}
              {obs.provenance && Object.keys(obs.provenance).length > 0 && (
                <details className="mt-2 text-xs">
                  <summary className="cursor-pointer text-muted-foreground">Provenance</summary>
                  <pre className="mt-1 overflow-x-auto whitespace-pre-wrap text-muted-foreground">
                    {JSON.stringify(maskOn ? deep(obs.provenance) : obs.provenance, null, 2)}
                  </pre>
                </details>
              )}
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}
