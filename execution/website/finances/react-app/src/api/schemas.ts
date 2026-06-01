import { get } from './client'
import type { Schema } from '@/types/neotoma'

export async function listSchemas(): Promise<Schema[]> {
  const res = await get<{ schemas: Schema[]; total?: number }>('/schemas')
  return res.schemas ?? []
}

export async function getSchema(entityType: string): Promise<Schema> {
  return get<Schema>(`/schemas/${entityType}`)
}
