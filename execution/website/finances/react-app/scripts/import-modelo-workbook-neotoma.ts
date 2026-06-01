/**
 * Import Spanish Modelo 720/721 Numbers workbook (Bienes + Cantidades YYYY) into Neotoma
 * as financial_account rows, then link them to a tax_filing entity.
 *
 * Workbook shape (per sheet):
 * - Bienes: Bien, Tipo, Nombre banco, País banco, Código BIC banco, Denominación, Siglas, Dirección criptografica, …
 * - Cantidades {year}: Bien, Valor, Fecha, Unidades, Sigla, €
 *
 * Picks year-end 31 Dec amounts: Saldo rows for USD brokerage buckets; Valor rows for crypto / equity.
 * Also captures "trimestre promedio" rows for Q4-average reconciliation (primarily Modelo 720 account lines).
 *
 * Usage (from react-app):
 *   npx tsx scripts/import-modelo-workbook-neotoma.ts \
 *     --xlsx "/abs/path/2024-Modelos_720_y_721.xlsx" \
 *     --filing-id ent_<720_2024_tax_filing> \
 *     --also-link-filing-id ent_<721_2024_tax_filing> \
 *     --tax-year 2024
 *
 *   --dry-run   (default) print summary + entity count
 *   --execute   neotoma store --file + relationships create (requires `neotoma` on PATH)
 *   --also-link-filing-id   repeat to REFERS_TO the same accounts from additional tax_filing entities
 *   --relationships-only --execute   skip store; resolve entity_id by registry_id search and link only
 */
import ExcelJS from 'exceljs'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const SKIP_BIEN = new Set(
  ['PLATAFORMA CRIPTOMONEDAS:', 'PLATAFORMA CRIPTOMONEDAS'].map((s) => s.toLowerCase()),
)

const CRYPTO_SIGLAS = new Set(['BTC', 'STX', 'UNI', 'USDC', 'ETH', 'SOL'])

function normKey(s: string): string {
  return String(s ?? '')
    .trim()
    .replace(/\s+/g, ' ')
}

function isDec31Year(d: unknown, year: number): boolean {
  if (!d) return false
  const dt = d instanceof Date ? d : new Date(String(d))
  if (Number.isNaN(dt.getTime())) return false
  return dt.getUTCFullYear() === year && dt.getUTCMonth() === 11 && dt.getUTCDate() === 31
}

function cellNum(v: unknown): number | undefined {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (v && typeof v === 'object' && 'result' in v) {
    const r = (v as { result: unknown }).result
    if (typeof r === 'number' && Number.isFinite(r)) return r
  }
  return undefined
}

interface BienesRow {
  bien: string
  tipo: string
  nombreBanco: string
  domicilioBanco: string
  paisBanco: string
  bic: string
  denominacion: string
  siglas: string
  identValores: string
  direccionCrypto: string
}

interface CantRow {
  bien: string
  valorTipo: string
  fecha: unknown
  unidades: number | undefined
  sigla: string
  eur: number | undefined
}

interface MergedAsset {
  bien: string
  registryId: string
  filingTags: string[]
  institution: string
  accountName: string
  modeloBien: string
  country: string
  currency: string
  balanceOriginal: number | undefined
  balanceEur: number | undefined
  q4AverageOriginal: number | undefined
  q4AverageEur: number | undefined
  q4AverageDeltaEur: number | undefined
  q4AverageDeltaPct: number | undefined
  q4ReconciliationStatus: 'missing_q4_average' | 'aligned' | 'variance' | undefined
  taxYear: number
  sourceFile: string
}

function normalizeLabelToken(raw: string | undefined | null): string {
  if (raw == null) return ''
  return String(raw).replace(/\s+/g, ' ').trim()
}

function stripLeadingPlaceholderPrefix(raw: string | undefined | null): string {
  const t = normalizeLabelToken(raw)
  return t.replace(/^(?:[-–—]\s*)+/, '').trim()
}

function hasMeaningfulLabel(raw: string | undefined | null): boolean {
  const t = normalizeLabelToken(raw)
  return t.length > 0 && !/^[—–-]+$/.test(t)
}

function splitCompoundLabel(raw: string | undefined | null): string[] {
  const t = normalizeLabelToken(raw)
  if (!t) return []
  return t
    .split(/\s*[—–]\s*|\s+-\s+/)
    .map((part) => normalizeLabelToken(part))
    .filter((part) => hasMeaningfulLabel(part))
}

function canonicalNameFromParts(institution: string, accountName: string): string {
  const parts = [institution, accountName].map((p) => normalizeLabelToken(p)).filter((p) => hasMeaningfulLabel(p))
  return parts.join(' — ').slice(0, 500)
}

function deriveInstitutionAndAccountName(
  institutionRaw: string | undefined | null,
  accountNameRaw: string | undefined | null,
  inferredInstitutionRaw: string | undefined | null,
): { institution: string; accountName: string } {
  const institution = normalizeLabelToken(institutionRaw)
  const accountName = stripLeadingPlaceholderPrefix(accountNameRaw)
  const inferredInstitution = normalizeLabelToken(inferredInstitutionRaw)

  if (hasMeaningfulLabel(institution)) {
    return { institution, accountName: accountName || institution }
  }

  const accountParts = splitCompoundLabel(accountName)
  if (accountParts.length >= 2) {
    return {
      institution: accountParts[0],
      accountName: accountParts.slice(1).join(' — '),
    }
  }

  if (hasMeaningfulLabel(inferredInstitution)) {
    return { institution: inferredInstitution, accountName: accountName || inferredInstitution }
  }

  return { institution: '', accountName: accountName || stripLeadingPlaceholderPrefix(institutionRaw) }
}

function slugRegistryId(bien: string, taxYear: number): string {
  const base = normKey(bien)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_|_$/g, '')
    .slice(0, 80)
  return `modelo_workbook_${taxYear}_${base || 'row'}`
}

function classifyTags(
  meta: Partial<BienesRow> | undefined,
  sigla: string,
  cryptoAddr: string,
  bienLabel: string,
): string[] {
  const s = sigla.toUpperCase()
  const addr = (cryptoAddr || '').trim()
  if (addr && addr !== '-') return ['721']
  if (CRYPTO_SIGLAS.has(s)) return ['721']
  const bank = (meta?.nombreBanco || '').toLowerCase()
  const bien = (meta?.bien || bienLabel || '').toLowerCase()
  if (
    bien.includes('hiro') ||
    bien.includes('kite') ||
    bien.includes('leather') ||
    bien.includes('nassau') ||
    bien.includes('pbc') ||
    bien.includes('solutions, inc')
  ) {
    return ['equity']
  }
  if (
    bank.includes('coinbase') ||
    bien.includes('coinbase') ||
    bank.includes('kraken') ||
    bien.includes('kraken')
  ) {
    return ['721']
  }
  return ['720']
}

function isGeneratedBienesFormat(hdr: unknown[]): boolean {
  return hdr.some((h) => String(h ?? '').trim() === 'Bien #') &&
    hdr.some((h) => String(h ?? '').trim() === 'Bien (workbook)')
}

function readBienes(ws: ExcelJS.Worksheet): Map<string, BienesRow> {
  const hdr = ws.getRow(1).values as unknown[]
  const idx = (name: string) => hdr.findIndex((h) => String(h ?? '').trim() === name)

  if (isGeneratedBienesFormat(hdr)) {
    return readBienesGenerated(ws, hdr)
  }

  const I = {
    bien: idx('Bien'),
    tipo: idx('Tipo'),
    nombre: idx('Nombre banco'),
    dom: idx('Domicilio (banco)'),
    pais: idx('País banco'),
    bic: idx('Código BIC banco'),
    denom: idx('Denominación'),
    siglas: idx('Siglas'),
    ident: idx('Identificación valores'),
    crypto: idx('Dirección criptografica '),
  }
  const m = new Map<string, BienesRow>()
  for (let r = 2; r <= ws.rowCount; r++) {
    const row = ws.getRow(r)
    const v = row.values as unknown[]
    const bienRaw = I.bien >= 0 ? v[I.bien] : null
    if (bienRaw == null || String(bienRaw).trim() === '') continue
    const bien = normKey(String(bienRaw))
    if (SKIP_BIEN.has(bien.toLowerCase())) continue
    m.set(bien, {
      bien,
      tipo: I.tipo >= 0 ? String(v[I.tipo] ?? '') : '',
      nombreBanco: I.nombre >= 0 ? String(v[I.nombre] ?? '') : '',
      domicilioBanco: I.dom >= 0 ? String(v[I.dom] ?? '') : '',
      paisBanco: I.pais >= 0 ? String(v[I.pais] ?? '') : '',
      bic: I.bic >= 0 ? String(v[I.bic] ?? '') : '',
      denominacion: I.denom >= 0 ? String(v[I.denom] ?? '') : '',
      siglas: I.siglas >= 0 ? String(v[I.siglas] ?? '') : '',
      identValores: I.ident >= 0 ? String(v[I.ident] ?? '') : '',
      direccionCrypto: I.crypto >= 0 ? String(v[I.crypto] ?? '') : '',
    })
  }
  return m
}

function readBienesGenerated(ws: ExcelJS.Worksheet, hdr: unknown[]): Map<string, BienesRow> {
  const idx = (name: string) => hdr.findIndex((h) => String(h ?? '').trim() === name)
  const I = {
    bienWorkbook: idx('Bien (workbook)'),
    modelo: idx('Modelo'),
    institution: idx('Institution'),
    account: idx('Account'),
    country: idx('Country'),
    currency: idx('Currency'),
  }
  const m = new Map<string, BienesRow>()
  for (let r = 2; r <= ws.rowCount; r++) {
    const row = ws.getRow(r)
    const v = row.values as unknown[]
    const bienRaw = I.bienWorkbook >= 0 ? v[I.bienWorkbook] : null
    if (bienRaw == null || String(bienRaw).trim() === '') continue
    const bien = normKey(String(bienRaw))
    if (SKIP_BIEN.has(bien.toLowerCase())) continue
    const institution = I.institution >= 0 ? String(v[I.institution] ?? '') : ''
    const currency = I.currency >= 0 ? String(v[I.currency] ?? '') : ''
    m.set(bien, {
      bien,
      tipo: '',
      nombreBanco: institution,
      domicilioBanco: '',
      paisBanco: I.country >= 0 ? String(v[I.country] ?? '') : '',
      bic: '',
      denominacion: '',
      siglas: currency,
      identValores: '',
      direccionCrypto: '',
    })
  }
  return m
}

function isInTaxYear(d: unknown, year: number): boolean {
  if (!d) return false
  const dt = d instanceof Date ? d : new Date(String(d))
  if (Number.isNaN(dt.getTime())) return false
  return dt.getUTCFullYear() === year
}

function isGeneratedCantidadesFormat(ws: ExcelJS.Worksheet): boolean {
  const hdr = ws.getRow(1).values as unknown[]
  return hdr.some((h) => String(h ?? '').trim() === 'Bien (workbook)') &&
    hdr.some((h) => String(h ?? '').trim().startsWith('Balance at 31 Dec'))
}

/** Prefer Saldo @ year-end; else Valor @ year-end. Capture trimestre promedio in a dedicated map. */
function readCantidades(
  ws: ExcelJS.Worksheet,
  taxYear: number,
): { yearEndByBien: Map<string, CantRow>; q4AverageByBien: Map<string, CantRow> } {
  if (isGeneratedCantidadesFormat(ws)) {
    return readCantidadesGenerated(ws, taxYear)
  }

  const yearEndByBien = new Map<string, CantRow>()
  const q4AverageByBien = new Map<string, CantRow>()
  const score = (row: CantRow) => {
    const v = row.valorTipo.toLowerCase()
    if (v.includes('saldo')) return 3
    if (v.includes('valor')) return 2
    return 0
  }
  for (let r = 2; r <= ws.rowCount; r++) {
    const row = ws.getRow(r)
    const v = row.values as unknown[]
    const bien = normKey(String(v[1] ?? ''))
    if (!bien || SKIP_BIEN.has(bien.toLowerCase())) continue
    const valorTipo = String(v[2] ?? '').trim()
    const fecha = v[3]
    if (fecha && !isInTaxYear(fecha, taxYear)) continue
    const unidades = cellNum(v[4])
    const sigla = String(v[5] ?? '').trim()
    const eurRaw = row.getCell(6).value
    const eur = cellNum(eurRaw)

    const cand: CantRow = { bien, valorTipo, fecha, unidades, sigla, eur }
    if (valorTipo.toLowerCase().includes('trimestre promedio')) {
      const prevQ4 = q4AverageByBien.get(bien)
      if (!prevQ4 || score(cand) > score(prevQ4)) {
        q4AverageByBien.set(bien, cand)
      }
      continue
    }
    if (!isDec31Year(fecha, taxYear)) continue
    const prev = yearEndByBien.get(bien)
    if (!prev || score(cand) > score(prev)) {
      yearEndByBien.set(bien, cand)
    }
  }
  return { yearEndByBien, q4AverageByBien }
}

function readCantidadesGenerated(
  ws: ExcelJS.Worksheet,
  taxYear: number,
): { yearEndByBien: Map<string, CantRow>; q4AverageByBien: Map<string, CantRow> } {
  const yearEndByBien = new Map<string, CantRow>()
  const q4AverageByBien = new Map<string, CantRow>()
  const hdr = ws.getRow(1).values as unknown[]
  const idx = (name: string) => hdr.findIndex((h) => String(h ?? '').trim() === name)
  const altIdx = (prefix: string) => hdr.findIndex((h) => String(h ?? '').trim().startsWith(prefix))
  const I = {
    bienWorkbook: idx('Bien (workbook)'),
    currency: idx('Currency'),
    balance: altIdx('Balance at 31 Dec'),
    valueEur: idx('Value EUR'),
  }

  for (let r = 2; r <= ws.rowCount; r++) {
    const row = ws.getRow(r)
    const v = row.values as unknown[]
    const bienRaw = I.bienWorkbook >= 0 ? v[I.bienWorkbook] : null
    if (bienRaw == null || String(bienRaw).trim() === '') continue
    const bien = normKey(String(bienRaw))
    if (SKIP_BIEN.has(bien.toLowerCase())) continue
    if (bien === 'TOTAL') continue

    const sigla = I.currency >= 0 ? String(v[I.currency] ?? '').trim() : ''
    const unidades = I.balance >= 0 ? cellNum(v[I.balance]) : undefined
    const eur = I.valueEur >= 0 ? cellNum(v[I.valueEur]) : undefined
    const dec31 = new Date(Date.UTC(taxYear, 11, 31))

    yearEndByBien.set(bien, {
      bien,
      valorTipo: 'Saldo',
      fecha: dec31,
      unidades,
      sigla,
      eur,
    })
  }

  return { yearEndByBien, q4AverageByBien }
}

/** Extra keys for Cantidades rows that only exist there (e.g. Coinbase aggregates). */
function cantidadAliasKeys(byBien: Map<string, CantRow>): void {
  if (byBien.has('Bitcoin') && !byBien.has('Bitcoin 1')) {
    const row = byBien.get('Bitcoin')!
    byBien.set('Bitcoin 1', row)
  }
}

function mergeWorkbook(
  bienesByName: Map<string, BienesRow>,
  cantByBien: Map<string, CantRow>,
  q4ByBien: Map<string, CantRow>,
  taxYear: number,
  sourceFile: string,
): MergedAsset[] {
  cantidadAliasKeys(cantByBien)
  const keys = new Set<string>([...bienesByName.keys(), ...cantByBien.keys()])
  const out: MergedAsset[] = []
  for (const bien of keys) {
    if (SKIP_BIEN.has(bien.toLowerCase())) continue
    const meta = bienesByName.get(bien)
    const cant = cantByBien.get(bien)
    if (!cant) continue

    const sigla = (cant.sigla || meta?.siglas || '').trim()
    const currency = sigla || (cant.unidades != null && !sigla ? 'USD' : '')
    const filingTags = classifyTags(meta, sigla, meta?.direccionCrypto ?? '', bien)
    const institutionRaw = meta?.nombreBanco || (sigla && CRYPTO_SIGLAS.has(sigla.toUpperCase()) ? 'Crypto wallet' : '')
    const accountNameRaw = meta?.bien || bien
    const inferredInstitution =
      filingTags.includes('721') || CRYPTO_SIGLAS.has((currency || '').toUpperCase())
        ? 'Crypto wallet'
        : filingTags.includes('equity')
          ? 'Private equity'
          : ''
    const derivedLabels = deriveInstitutionAndAccountName(institutionRaw, accountNameRaw, inferredInstitution)
    const institution = derivedLabels.institution
    const accountName = derivedLabels.accountName || accountNameRaw
    const modeloBien = [meta?.tipo, meta?.denominacion].filter(Boolean).join(' · ') || bien
    const country = meta?.paisBanco || ''

    let balanceOriginal = cant.unidades
    let balanceEur = cant.eur
    if (currency === 'USD' && balanceEur == null && balanceOriginal != null) {
      balanceEur = balanceOriginal
    }
    if (!currency && balanceOriginal != null) {
      balanceEur = balanceOriginal
    }

    const q4 = q4ByBien.get(bien)
    let q4AverageOriginal = q4?.unidades
    let q4AverageEur = q4?.eur
    if (currency === 'USD' && q4AverageEur == null && q4AverageOriginal != null) {
      q4AverageEur = q4AverageOriginal
    }
    if (!currency && q4AverageOriginal != null) {
      q4AverageEur = q4AverageOriginal
    }

    const q4AverageDeltaEur =
      q4AverageEur != null && balanceEur != null ? balanceEur - q4AverageEur : undefined
    const q4AverageDeltaPct =
      q4AverageDeltaEur != null && q4AverageEur && q4AverageEur !== 0
        ? (q4AverageDeltaEur / q4AverageEur) * 100
        : undefined
    const q4ReconciliationStatus: MergedAsset['q4ReconciliationStatus'] =
      q4AverageEur == null ? 'missing_q4_average' : q4AverageDeltaEur == null ? undefined : Math.abs(q4AverageDeltaEur) <= 1 ? 'aligned' : 'variance'

    out.push({
      bien,
      registryId: slugRegistryId(bien, taxYear),
      filingTags,
      institution,
      accountName,
      modeloBien,
      country,
      currency: currency || 'USD',
      balanceOriginal,
      balanceEur,
      q4AverageOriginal,
      q4AverageEur,
      q4AverageDeltaEur,
      q4AverageDeltaPct,
      q4ReconciliationStatus,
      taxYear,
      sourceFile,
    })
  }
  return dropWorkbookParentRowsSupersededByChildren(out, taxYear)
}

/** Omit `modelo_workbook_*_<venue>` parent when `modelo_workbook_*_<venue>_<asset>` children exist — same rule as the app. */
function dropWorkbookParentRowsSupersededByChildren(assets: MergedAsset[], taxYear: number): MergedAsset[] {
  const rids = new Set(assets.map((a) => a.registryId.toLowerCase()))
  return assets.filter((a) => {
    const rid = a.registryId.toLowerCase()
    const m = rid.match(/^modelo_workbook_(\d+)_(.+)$/)
    if (!m) return true
    const year = m[1]
    const slug = m[2]
    if (year !== String(taxYear)) return true
    const childPrefix = `modelo_workbook_${year}_${slug}_`
    for (const other of rids) {
      if (other === rid) continue
      if (other.startsWith(childPrefix) && other.length > rid.length) return false
    }
    return true
  })
}

function toNeotomaEntity(a: MergedAsset): Record<string, unknown> {
  const canonicalName = canonicalNameFromParts(a.institution, a.accountName)
  const ent: Record<string, unknown> = {
    entity_type: 'financial_account',
    registry_id: a.registryId,
    canonical_name: canonicalName || a.accountName.slice(0, 500),
    institution: a.institution,
    account_name: a.accountName,
    modelo_bien: a.modeloBien,
    country: a.country,
    jurisdiction: a.country,
    currency: a.currency,
    filing_tags: a.filingTags,
    tax_year_context: a.taxYear,
    tax_year: a.taxYear,
    modelo_workbook_source_file: a.sourceFile,
    modelo_workbook_bien_label: a.bien,
    observation_kind: 'modelo_workbook_import',
  }
  if (a.balanceOriginal != null) {
    if (a.currency === 'USD') {
      ent.ending_account_value_usd = a.balanceOriginal
      ent.account_value = a.balanceOriginal
      ent.account_value_currency = 'USD'
    } else if (!CRYPTO_SIGLAS.has(a.currency.toUpperCase())) {
      ent.account_value = a.balanceOriginal
      ent.account_value_currency = a.currency
    } else {
      ent.account_value = a.balanceOriginal
      ent.account_value_currency = a.currency
    }
  }
  if (a.balanceOriginal != null) {
    ent.balance_value = a.balanceOriginal
    ent.balance_currency = a.currency
    ent.balance_date = `${a.taxYear}-12-31`
  }
  if (a.institution) {
    ent.institution_name = a.institution
  }
  const isCrypto = a.filingTags.includes('721') || CRYPTO_SIGLAS.has(a.currency.toUpperCase())
  const isEquity = a.filingTags.includes('equity')
  ent.denomination_category = isCrypto ? 'crypto' : isEquity ? 'investments' : 'fiat_cash'
  ent.display_sign = 1
  if (a.balanceEur != null) {
    ent.balance_eur = a.balanceEur
    ent.ending_account_value_eur = a.balanceEur
  }
  if (a.q4AverageOriginal != null) {
    ent.q4_average_balance_original = a.q4AverageOriginal
    ent.q4_average_balance_currency = a.currency
  }
  if (a.q4AverageEur != null) {
    ent.q4_average_balance_eur = a.q4AverageEur
  }
  if (a.q4AverageDeltaEur != null) {
    ent.q4_vs_year_end_delta_eur = a.q4AverageDeltaEur
  }
  if (a.q4AverageDeltaPct != null) {
    ent.q4_vs_year_end_delta_pct = a.q4AverageDeltaPct
  }
  if (a.q4ReconciliationStatus) {
    ent.q4_reconciliation_status = a.q4ReconciliationStatus
  }
  return ent
}

function parseArgs(argv: string[]) {
  let xlsx = ''
  let filingId = ''
  const alsoLinkFilingIds: string[] = []
  let taxYear = 2024
  let execute = false
  let relationshipsOnly = false
  let idempotencyKeySuffix = ''
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--xlsx') xlsx = argv[++i] ?? ''
    else if (a === '--filing-id') filingId = argv[++i] ?? ''
    else if (a === '--also-link-filing-id') {
      const v = argv[++i]
      if (v) alsoLinkFilingIds.push(v)
    } else if (a === '--tax-year') taxYear = Number(argv[++i] ?? '2024')
    else if (a === '--execute') execute = true
    else if (a === '--dry-run') execute = false
    else if (a === '--relationships-only') relationshipsOnly = true
    else if (a === '--idempotency-key-suffix') idempotencyKeySuffix = normKey(argv[++i] ?? '')
  }
  return { xlsx, filingId, alsoLinkFilingIds, taxYear, execute, relationshipsOnly, idempotencyKeySuffix }
}

function searchEntityIdByRegistry(registryId: string): string | null {
  const r = spawnSync(
    'neotoma',
    ['entities', 'search', '--entity-type', 'financial_account', '--identifier', registryId],
    { encoding: 'utf8' },
  )
  if (r.status !== 0) return null
  try {
    const data = JSON.parse(r.stdout || '{}') as {
      entities?: Array<{ entity_id?: string }>
      data?: Array<{ entity_id?: string }>
    }
    const list = data.entities ?? data.data ?? []
    const hit = list.find((e) => e.entity_id)
    return hit?.entity_id ?? null
  } catch {
    return null
  }
}

function linkFilingsToAccountIds(filingIds: string[], accountIds: string[]): void {
  for (const sourceFilingId of filingIds) {
    for (const targetId of accountIds) {
      const rel = spawnSync(
        'neotoma',
        [
          'relationships',
          'create',
          '--source-entity-id',
          sourceFilingId,
          '--target-entity-id',
          targetId,
          '--relationship-type',
          'REFERS_TO',
        ],
        { encoding: 'utf8' },
      )
      if (rel.status !== 0) {
        console.warn('relationship failed', sourceFilingId, '->', targetId, rel.stderr || rel.stdout)
      }
    }
    console.log(`Linked ${accountIds.length} accounts from tax_filing ${sourceFilingId}.`)
  }
}

async function main() {
  const { xlsx, filingId, alsoLinkFilingIds, taxYear, execute, relationshipsOnly, idempotencyKeySuffix } = parseArgs(process.argv)
  if (!xlsx || !fs.existsSync(xlsx)) {
    console.error(
      'Usage: --xlsx /path/to/workbook.xlsx --filing-id ent_... [--also-link-filing-id ent_...] [--tax-year 2024] [--execute]',
    )
    process.exit(2)
  }
  if (!filingId) {
    console.error('Missing --filing-id (tax_filing entity_id)')
    process.exit(2)
  }
  const wb = new ExcelJS.Workbook()
  await wb.xlsx.readFile(xlsx)
  const sheetBienes = wb.getWorksheet('Bienes')
  const sheetCant = wb.getWorksheet(`Cantidades ${taxYear}`)
  if (!sheetBienes || !sheetCant) {
    console.error('Missing worksheet "Bienes" or', `"Cantidades ${taxYear}"`)
    process.exit(1)
  }

  const bienesByName = readBienes(sheetBienes)
  const { yearEndByBien: cantByBien, q4AverageByBien } = readCantidades(sheetCant, taxYear)
  const sourceFile = path.basename(xlsx)
  const merged = mergeWorkbook(bienesByName, cantByBien, q4AverageByBien, taxYear, sourceFile)

  console.log(`Merged ${merged.length} asset rows from ${sourceFile} (tax year ${taxYear}).`)
  const byTag = merged.reduce<Record<string, number>>((acc, m) => {
    const k = m.filingTags.join('+')
    acc[k] = (acc[k] ?? 0) + 1
    return acc
  }, {})
  console.log('By filing_tags:', byTag)
  const q4Tracked = merged.filter((m) => m.q4AverageOriginal != null || m.q4AverageEur != null).length
  const q4Variance = merged.filter((m) => m.q4ReconciliationStatus === 'variance').length
  console.log(`Q4 averages tracked: ${q4Tracked}/${merged.length}; variances: ${q4Variance}`)

  const entities = merged.map(toNeotomaEntity)

  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'modelo-import-'))
  const jsonPath = path.join(outDir, 'store.json')
  /** `neotoma store --file` expects a JSON array of entities. */
  fs.writeFileSync(jsonPath, JSON.stringify(entities, null, 2), 'utf8')
  console.log('Wrote', jsonPath)

  if (!execute) {
    console.log('\nDry run only. Re-run with --execute to call `neotoma store` and link relationships.')
    return
  }

  const filingIdsForLinks = [...new Set([filingId, ...alsoLinkFilingIds].filter(Boolean))]

  if (relationshipsOnly) {
    const accountIds: string[] = []
    for (const m of merged) {
      const eid = searchEntityIdByRegistry(m.registryId)
      if (eid) accountIds.push(eid)
      else console.warn('No entity for registry_id', m.registryId)
    }
    console.log(`Resolved ${accountIds.length}/${merged.length} financial_account entities by registry_id.`)
    linkFilingsToAccountIds(filingIdsForLinks, accountIds)
    return
  }

  const baseIdempotencyKey = `modelo-workbook-${taxYear}-${normKey(path.basename(xlsx)).slice(0, 40)}`
  const idempotencyKey = idempotencyKeySuffix ? `${baseIdempotencyKey}-${idempotencyKeySuffix}` : baseIdempotencyKey
  const store = spawnSync(
    'neotoma',
    [
      'store',
      '--file',
      jsonPath,
      '--api-only',
      '--file-path',
      path.resolve(xlsx),
      '--idempotency-key',
      idempotencyKey,
    ],
    { encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] },
  )
  if (store.status !== 0) {
    console.error(store.stderr || store.stdout)
    process.exit(store.status ?? 1)
  }
  const out = (store.stdout || '').trim()
  console.log(out.slice(0, 4000))

  let resp: { entities?: Array<{ entity_id?: string }>; structured?: { entities?: Array<{ entity_id?: string }> } }
  try {
    resp = JSON.parse(out || '{}')
  } catch {
    console.warn('Could not parse store stdout; skip auto relationships. Create REFERS_TO from filing manually.')
    return
  }
  const list =
    resp.entities ??
    resp.structured?.entities ??
    (Array.isArray(resp) ? (resp as Array<{ entity_id?: string }>) : [])
  const ids = list.map((e) => e.entity_id).filter(Boolean) as string[]
  linkFilingsToAccountIds(filingIdsForLinks, ids)
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
