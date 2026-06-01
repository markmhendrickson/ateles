import { normalizeEntityTypeKey } from '@shared/api/entities'

export { normalizeEntityTypeKey }

export function resolveEntityType(entity: {
  entity_id?: string
  entity_type?: string | null
  snapshot?: Record<string, unknown> | null
}): string {
  return normalizeEntityTypeKey(entity.entity_type ?? undefined)
}

/**
 * Canonical in-app path for an entity. Use this for all Link targets so navigation matches record shape.
 */
export function entityHref(entity: {
  entity_id: string
  entity_type?: string | null
  snapshot?: Record<string, unknown> | null
}): string {
  const id = entity.entity_id
  if (!id) return '/explorer'
  const t = resolveEntityType(entity)
  switch (t) {
    case 'financial_account':
    case 'account_statement':
      return `/accounts/${id}`
    case 'tax_filing':
      return `/filings/${id}`
    default:
      if (!t) return `/explorer?id=${encodeURIComponent(id)}`
      return `/explorer?type=${encodeURIComponent(t)}&id=${encodeURIComponent(id)}`
  }
}

/** Types the `/accounts/:id` screen is designed to render. */
export function isAccountsRouteEntityType(t: string): boolean {
  return t === 'financial_account' || t === 'account_statement'
}
