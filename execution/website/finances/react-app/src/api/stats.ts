import { get } from './client'
import type { StatsResponse } from '@/types/neotoma'

export async function getStats(): Promise<StatsResponse> {
  return get<StatsResponse>('/stats')
}
