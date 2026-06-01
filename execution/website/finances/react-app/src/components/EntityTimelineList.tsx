import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { getTimeline } from '@/api/timeline'
import TimelineEventCard from '@/components/TimelineEventCard'
import { Button } from '@/components/ui/button'

const ENTITY_TIMELINE_LIMIT = 400

/**
 * Timeline rows whose `entity_id` matches (client-filtered after fetch).
 * Distinct from per-entity observations (see ObservationTimeline).
 */
export default function EntityTimelineList({
  entityId,
  className,
}: {
  entityId: string
  className?: string
}) {
  const { data: raw, isLoading, error } = useQuery({
    queryKey: ['timeline', 'entity', entityId],
    queryFn: () => getTimeline({ entity_id: entityId, limit: ENTITY_TIMELINE_LIMIT, order_by: 'event_timestamp' }),
    enabled: Boolean(entityId),
    staleTime: 60_000,
  })

  const events = useMemo(() => {
    if (!raw?.length) return []
    return raw
      .filter(e => e.entity_id === entityId)
      .sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime())
  }, [raw, entityId])

  return (
    <section className={className}>
      <div className="flex flex-wrap items-end justify-between gap-2 mb-3">
        <h2 className="text-lg font-medium">Event Timeline</h2>
        <Button variant="link" className="h-auto p-0 text-muted-foreground text-sm" asChild>
          <Link to="/timeline">All recent activity</Link>
        </Button>
      </div>
      <p className="text-sm text-muted-foreground mb-3">
        Date-derived events for this record, newest event first.
      </p>

      {isLoading && <p className="text-sm text-muted-foreground animate-pulse">Loading timeline…</p>}
      {error && <p className="text-sm text-destructive">Error: {(error as Error).message}</p>}

      {!isLoading && !error && events.length === 0 && (
        <p className="text-sm text-muted-foreground py-4 border border-dashed border-border rounded-lg px-4">
          No date-derived events were recorded for this record.
        </p>
      )}

      {events.length > 0 && (
        <div className="space-y-3">
          {events.map((event, i) => (
            <TimelineEventCard key={event.id || `${entityId}-${i}`} event={event} />
          ))}
        </div>
      )}
    </section>
  )
}
