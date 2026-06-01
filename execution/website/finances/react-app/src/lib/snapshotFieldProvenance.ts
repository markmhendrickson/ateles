import type { Observation } from '@/types/neotoma'

function observationTime(o: Observation): number {
  return new Date(o.observed_at || o.created_at || 0).getTime()
}

/**
 * Most recent observation whose `data` object has this top-level key (same key as snapshot materialization).
 */
export function findLatestObservationForField(
  observations: Observation[] | undefined,
  fieldKey: string,
): Observation | null {
  if (!observations?.length || !fieldKey) return null
  const sorted = [...observations].sort((a, b) => observationTime(b) - observationTime(a))
  for (const obs of sorted) {
    const d = obs.data
    if (d != null && typeof d === 'object' && !Array.isArray(d) && Object.prototype.hasOwnProperty.call(d, fieldKey)) {
      return obs
    }
  }
  return null
}
