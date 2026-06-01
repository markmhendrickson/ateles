import { describe, expect, it } from 'vitest'
import { buildRecentActivityEvents, getRecordedAt, isRecentActivityEntity } from '@/lib/recentActivity'
import type { Entity } from '@/types/neotoma'

function makeEntity(overrides: Partial<Entity>): Entity {
  return {
    entity_id: overrides.entity_id ?? 'ent-1',
    entity_type: overrides.entity_type ?? 'note',
    canonical_name: overrides.canonical_name ?? null,
    snapshot: overrides.snapshot ?? { title: 'Example' },
    ...overrides,
  }
}

describe('recentActivity helpers', () => {
  it('prefers last_observation_at when determining record time', () => {
    const entity = makeEntity({
      last_observation_at: '2026-04-20T08:11:58.851Z',
      updated_at: '2026-04-20T08:00:00.000Z',
    })

    expect(getRecordedAt(entity)).toBe('2026-04-20T08:11:58.851Z')
  })

  it('filters out internal bookkeeping entity types', () => {
    expect(isRecentActivityEntity(makeEntity({ entity_type: 'agent_message' }))).toBe(false)
    expect(isRecentActivityEntity(makeEntity({ entity_type: 'conversation' }))).toBe(false)
    expect(isRecentActivityEntity(makeEntity({ entity_type: 'email_message' }))).toBe(false)
    expect(isRecentActivityEntity(makeEntity({
      entity_type: 'email_message',
      last_observation_at: '2026-04-20T08:11:58.851Z',
    }))).toBe(true)
  })

  it('builds events sorted by last observation time and preserves embedded entities', () => {
    const older = makeEntity({
      entity_id: 'older',
      entity_type: 'note',
      last_observation_at: '2026-04-19T08:11:58.851Z',
      snapshot: { title: 'Older note' },
    })
    const newer = makeEntity({
      entity_id: 'newer',
      entity_type: 'email_message',
      last_observation_at: '2026-04-20T08:11:58.851Z',
      snapshot: { subject: 'Newer email' },
    })

    const events = buildRecentActivityEvents([older, newer])

    expect(events).toHaveLength(2)
    expect(events[0].entity_id).toBe('newer')
    expect(events[0].timestamp).toBe('2026-04-20T08:11:58.851Z')
    expect(events[0].event_type).toBe('Recorded')
    expect(events[0].entity).toBe(newer)
  })

  it('applies recorded-at date filters inclusively', () => {
    const entities = [
      makeEntity({
        entity_id: 'in-range',
        entity_type: 'note',
        last_observation_at: '2026-04-20T12:00:00.000Z',
      }),
      makeEntity({
        entity_id: 'out-of-range',
        entity_type: 'note',
        last_observation_at: '2026-04-18T12:00:00.000Z',
      }),
    ]

    const events = buildRecentActivityEvents(entities, {
      startDate: '2026-04-20',
      endDate: '2026-04-20',
    })

    expect(events.map(event => event.entity_id)).toEqual(['in-range'])
  })
})
