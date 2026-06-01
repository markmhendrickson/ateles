import { get } from './client'
import type { TimelineEvent } from '@/types/neotoma'

interface TimelineParams {
  start_date?: string
  end_date?: string
  limit?: number
  offset?: number
  entity_id?: string
  order_by?: 'event_timestamp' | 'created_at'
}

type RawTimelineEvent = Partial<TimelineEvent> & {
  event_timestamp?: string
  created_at?: string | null
}

function normalizeTimelineEvent(raw: RawTimelineEvent): TimelineEvent {
  return {
    id: raw.id ?? '',
    entity_id: raw.entity_id ?? '',
    entity_type: raw.entity_type ?? '',
    event_type: raw.event_type ?? null,
    timestamp: raw.timestamp ?? raw.event_timestamp ?? '',
    created_at: raw.created_at ?? null,
    data: raw.data ?? null,
    entity: raw.entity,
    source_field: raw.source_field ?? null,
    source_id: raw.source_id ?? null,
  }
}

export async function getTimeline(params?: TimelineParams): Promise<TimelineEvent[]> {
  const searchParams = new URLSearchParams()
  if (params?.start_date) searchParams.set('start_date', params.start_date)
  if (params?.end_date) searchParams.set('end_date', params.end_date)
  if (params?.limit) searchParams.set('limit', String(params.limit))
  if (params?.offset) searchParams.set('offset', String(params.offset))
  if (params?.entity_id) searchParams.set('entity_id', params.entity_id)
  if (params?.order_by) searchParams.set('order_by', params.order_by)

  const qs = searchParams.toString()
  const path = `/timeline${qs ? `?${qs}` : ''}`
  const res = await get<{ events: RawTimelineEvent[]; total?: number }>(path)
  return (res.events ?? []).map(normalizeTimelineEvent)
}
