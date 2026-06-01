import type { Entity, TimelineEvent } from '@/types/neotoma'

const RECENT_ACTIVITY_HIDDEN_TYPES = new Set(['agent_message', 'conversation'])

function parseTime(value: string | undefined | null): number {
  if (!value) return Number.NaN
  return Date.parse(value)
}

export function getRecordedAt(entity: Entity): string | undefined {
  const candidates = [entity.last_observation_at, entity.updated_at, entity.created_at]
  return candidates.find((value): value is string => typeof value === 'string' && value.trim().length > 0)
}

export function isRecentActivityEntity(entity: Entity): boolean {
  return !RECENT_ACTIVITY_HIDDEN_TYPES.has(entity.entity_type) && Boolean(getRecordedAt(entity))
}

function matchesRecordedDateRange(entity: Entity, startDate?: string, endDate?: string): boolean {
  const recordedAt = getRecordedAt(entity)
  const recordedMs = parseTime(recordedAt)
  if (!Number.isFinite(recordedMs)) return false

  if (startDate) {
    const startMs = parseTime(`${startDate}T00:00:00`)
    if (Number.isFinite(startMs) && recordedMs < startMs) return false
  }

  if (endDate) {
    const endMs = parseTime(`${endDate}T23:59:59.999`)
    if (Number.isFinite(endMs) && recordedMs > endMs) return false
  }

  return true
}

export function buildRecentActivityEvents(
  entities: Entity[],
  opts?: { startDate?: string; endDate?: string; limit?: number },
): TimelineEvent[] {
  const startDate = opts?.startDate
  const endDate = opts?.endDate
  const limit = opts?.limit ?? 200

  return entities
    .filter(isRecentActivityEntity)
    .filter(entity => matchesRecordedDateRange(entity, startDate, endDate))
    .sort((a, b) => parseTime(getRecordedAt(b)) - parseTime(getRecordedAt(a)))
    .slice(0, limit)
    .map(entity => {
      const timestamp = getRecordedAt(entity) ?? new Date(0).toISOString()
      return {
        id: `recent-${entity.entity_id}-${timestamp}`,
        entity_id: entity.entity_id,
        entity_type: entity.entity_type,
        event_type: 'Recorded',
        timestamp,
        data: null,
        entity,
      }
    })
}
