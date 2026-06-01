/**
 * Official-style titles for Spanish AEAT modelos when `form_name` is absent on `tax_filing`.
 * Keys: normalized `form_code` (digits only or common aliases).
 */
const TAX_FORM_NAME_BY_CODE: Record<string, string> = {
  '720':
    'Declaración informativa sobre bienes y derechos situados en el extranjero (Modelo 720)',
  '721':
    'Declaración informativa sobre monedas virtuales situadas en el extranjero (Modelo 721)',
}

function normalizeFormCode(raw: unknown): string {
  const s = String(raw ?? '')
    .trim()
    .toLowerCase()
    .replace(/^modelo\s*/, '')
    .replace(/[^0-9a-z]/g, '')
  if (/^720/.test(s) || s === 'm720') return '720'
  if (/^721/.test(s) || s === 'm721') return '721'
  const digits = s.replace(/\D/g, '')
  if (digits === '720' || digits === '721') return digits
  return digits || s
}

/** Resolved display / completeness value for `form_name` (stored name wins). */
export function resolvedTaxFormName(snapshot: Record<string, unknown> | null | undefined): string | undefined {
  if (!snapshot) return undefined
  const explicit = snapshot.form_name
  if (typeof explicit === 'string' && explicit.trim() !== '') return explicit.trim()
  const code = normalizeFormCode(snapshot.form_code)
  return TAX_FORM_NAME_BY_CODE[code]
}
