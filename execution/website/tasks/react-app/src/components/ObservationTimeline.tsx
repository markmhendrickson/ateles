import type { Observation } from '@shared/types/neotoma'
import { formatDate } from '@shared/lib/formatters'
import { humanizePropertyKey } from '@shared/lib/propertyLabels'
import { Badge } from '@shared/components/ui/badge'
import { Card, CardContent } from '@shared/components/ui/card'
import { cn } from '@shared/lib/utils'

interface Props {
  observations: Observation[]
  className?: string
}

export default function ObservationTimeline({ observations, className }: Props) {
  if (observations.length === 0) {
    return <p className="text-muted-foreground text-sm py-4">No observations</p>
  }

  const sorted = [...observations].sort(
    (a, b) =>
      new Date(b.observed_at || b.created_at).getTime() - new Date(a.observed_at || a.created_at).getTime(),
  )

  return (
    <div className={cn('max-h-[min(72vh,1400px)] overflow-y-auto space-y-3 pr-1', className)}>
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
                    {formatDate(obs.observed_at || obs.created_at)}
                  </span>
                  <Badge variant="secondary" className="font-normal">
                    {sourceLabel}
                  </Badge>
                  {obs.observation_kind && (
                    <Badge variant="outline" className="font-normal text-xs" title={obs.observation_kind}>
                      {humanizePropertyKey(obs.observation_kind)}
                    </Badge>
                  )}
                </div>
                <details className="text-[10px] text-muted-foreground">
                  <summary className="cursor-pointer hover:text-foreground transition-colors text-right">IDs</summary>
                  <div className="flex flex-col items-start gap-0.5 font-mono break-all sm:text-right sm:items-end mt-1">
                    <span title="Record ID">{obs.id}</span>
                    {obs.idempotency_key && (
                      <span title="Import key">{obs.idempotency_key}</span>
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
                      {JSON.stringify(payload, null, 2)}
                    </pre>
                  )
                }
                if (isPlainObject && keys.length === 0) {
                  return <p className="text-xs text-muted-foreground">No data fields in this update.</p>
                }
                return <p className="text-xs text-muted-foreground">Metadata-only update.</p>
              })()}
              {obs.provenance && Object.keys(obs.provenance).length > 0 && (
                <details className="mt-2 text-xs">
                  <summary className="cursor-pointer text-muted-foreground">Provenance</summary>
                  <pre className="mt-1 overflow-x-auto whitespace-pre-wrap text-muted-foreground">
                    {JSON.stringify(obs.provenance, null, 2)}
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
