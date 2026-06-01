import type { Entity } from '@shared/types/neotoma'

export interface TimelineEvent {
  id: string
  entity_id: string
  entity_type: string
  event_type?: string | null
  timestamp: string
  created_at?: string | null
  data?: Record<string, unknown> | null
  entity?: Entity
  source_field?: string | null
  source_id?: string | null
}

export interface SheetRow {
  [key: string]: string | number | undefined
}
