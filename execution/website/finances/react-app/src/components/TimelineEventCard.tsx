import { formatDate } from '@/lib/formatters'
import { useMaskMode } from '@/context/MaskModeContext'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import TimelinePointerHydration from '@/components/TimelinePointerHydration'
import type { TimelineEvent } from '@/types/neotoma'

export default function TimelineEventCard({ event }: { event: TimelineEvent }) {
  const { enabled: maskOn, taxonomyLabel, freeform, deep } = useMaskMode()

  return (
    <Card>
      <CardContent className="pt-6">
        <div className="flex items-center justify-between mb-2 gap-2">
          <div className="flex items-center gap-2 flex-wrap min-w-0">
            <Badge variant="secondary" title={event.entity_type || undefined}>
              {taxonomyLabel(event.entity_type || '')}
            </Badge>
            {event.event_type && (
              <span className="text-xs text-muted-foreground truncate" title={event.event_type}>
                {taxonomyLabel(event.event_type)}
              </span>
            )}
          </div>
          <span className="text-xs text-muted-foreground shrink-0">
            {maskOn ? freeform(String(event.timestamp)) : formatDate(event.timestamp)}
          </span>
        </div>
        {(() => {
          const payload = event.data
          const isPlainObject = payload != null && typeof payload === 'object' && !Array.isArray(payload)
          const keys = isPlainObject ? Object.keys(payload as Record<string, unknown>) : []
          if (isPlainObject && keys.length > 0) {
            return (
              <pre className="text-xs text-foreground/80 overflow-x-auto whitespace-pre-wrap">
                {JSON.stringify(maskOn ? deep(payload) : payload, null, 2)}
              </pre>
            )
          }
          return (
            <div className="space-y-2">
              {payload === null ? (
                <p className="text-xs text-muted-foreground">
                  Summary event — detailed values are resolved from the current record when available.
                </p>
              ) : isPlainObject && keys.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  No additional data attached to this event.
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Resolving details from the linked record.
                </p>
              )}
              <TimelinePointerHydration event={event} />
            </div>
          )
        })()}
      </CardContent>
    </Card>
  )
}
