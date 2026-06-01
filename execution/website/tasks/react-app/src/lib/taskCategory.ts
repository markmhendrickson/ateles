import { snapshotField } from '@shared/lib/formatters'
import type { Entity } from '@shared/types/neotoma'

/** URL param value for tasks with no category */
export const UNCATEGORIZED_PARAM = '_uncategorized'

/**
 * Stable category key from snapshot (lowercase slug). Empty string = uncategorized.
 */
export function taskCategoryKey(entity: Entity): string {
  const s = entity.snapshot
  if (!s) return ''

  const direct = snapshotField<string>(s, 'category')
  if (typeof direct === 'string' && direct.trim()) return normalizeKey(direct)

  const domain = snapshotField<string>(s, 'domain')
  if (typeof domain === 'string' && domain.trim()) return normalizeKey(domain)

  const project = snapshotField<string>(s, 'project')
  if (typeof project === 'string' && project.trim()) return normalizeKey(project)

  const pn = snapshotField<string | string[]>(s, 'project_names')
  if (Array.isArray(pn) && pn.length > 0) {
    const first = String(pn[0] ?? '').trim()
    if (first) return normalizeKey(first)
  }
  if (typeof pn === 'string' && pn.trim()) {
    const first = pn.split(',')[0]?.trim()
    if (first) return normalizeKey(first)
  }

  const area = snapshotField<string>(s, 'area')
  if (typeof area === 'string' && area.trim()) return normalizeKey(area)

  const tags = s.tags
  if (Array.isArray(tags) && tags.length > 0) {
    const first = String(tags[0] ?? '').trim()
    if (first) return normalizeKey(first)
  }

  return ''
}

function normalizeKey(raw: string): string {
  return raw
    .trim()
    .toLowerCase()
    .replace(/\s+/g, ' ')
}

/**
 * Human-readable label for sidebar (title case words).
 */
export function categoryDisplayLabel(key: string): string {
  if (!key) return 'Uncategorized'
  return key
    .split(' ')
    .map(w => (w.length ? w.charAt(0).toUpperCase() + w.slice(1) : w))
    .join(' ')
}

export function categoryToSearchParam(key: string): string {
  if (!key) return UNCATEGORIZED_PARAM
  return encodeURIComponent(key)
}

export function categoryFromSearchParam(param: string | null): string | null {
  if (param == null || param === '') return null
  if (param === UNCATEGORIZED_PARAM) return ''
  try {
    return decodeURIComponent(param)
  } catch {
    return param
  }
}
