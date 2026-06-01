import { useMemo, useState } from 'react'
import { useEntities } from '@/hooks/useEntities'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import TimelineEventCard from '@/components/TimelineEventCard'
import { buildRecentActivityEvents } from '@/lib/recentActivity'

const RECENT_ACTIVITY_QUERY_LIMIT = 1000
const RECENT_ACTIVITY_DISPLAY_LIMIT = 200

export default function Timeline() {
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const { data, isLoading, error } = useEntities({
    sort_by: 'last_observation_at',
    sort_order: 'desc',
    include_snapshots: true,
    limit: RECENT_ACTIVITY_QUERY_LIMIT,
  })
  const events = useMemo(
    () =>
      buildRecentActivityEvents(data?.entities ?? [], {
        startDate: startDate || undefined,
        endDate: endDate || undefined,
        limit: RECENT_ACTIVITY_DISPLAY_LIMIT,
      }),
    [data?.entities, endDate, startDate],
  )

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Recent Activity</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Records ordered by when Neotoma last observed them (newest first).
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-1.5">
          <span className="text-sm text-muted-foreground">From</span>
          <Input type="date" value={startDate} onChange={e => setStartDate(e.target.value)} className="w-[160px]" />
        </div>
        <div className="space-y-1.5">
          <span className="text-sm text-muted-foreground">To</span>
          <Input type="date" value={endDate} onChange={e => setEndDate(e.target.value)} className="w-[160px]" />
        </div>
        {(startDate || endDate) && (
          <Button variant="link" className="h-auto px-0 pb-0" onClick={() => { setStartDate(''); setEndDate('') }}>
            Clear
          </Button>
        )}
      </div>

      {isLoading && <p className="text-sm text-muted-foreground animate-pulse">Loading timeline...</p>}
      {error && <p className="text-sm text-destructive">Error: {(error as Error).message}</p>}

      {events && events.length === 0 && (
        <div className="text-muted-foreground text-sm py-8 text-center space-y-2 max-w-lg mx-auto">
          <p>No recent records found.</p>
          <p className="text-xs text-muted-foreground/90">
            No records match the selected observation window.
          </p>
        </div>
      )}

      {events && events.length > 0 && (
        <div className="space-y-3">
          {events.map((event, i) => (
            <TimelineEventCard key={event.id || i} event={event} />
          ))}
        </div>
      )}
    </div>
  )
}
