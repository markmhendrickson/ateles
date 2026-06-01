import ExcelJS from 'exceljs'
import type { Entity } from '@/types/neotoma'
import { coalesceSnapshot, snapshotField } from './formatters'
import {
  getEntityMonetaryDisplayBasisEur,
  getEntityRawStorageLegs,
  type UsdPerEurResolver,
} from './aggregations'
import { humanizeAccountLabel, rawFinancialAccountDisplayLabel } from './humanize'

const HEADER_FILL: ExcelJS.Fill = {
  type: 'pattern',
  pattern: 'solid',
  fgColor: { argb: 'FF2D5F8A' },
}
const HEADER_FONT: Partial<ExcelJS.Font> = {
  bold: true,
  color: { argb: 'FFFFFFFF' },
  size: 11,
}
const BORDER_THIN: Partial<ExcelJS.Borders> = {
  top: { style: 'thin' },
  left: { style: 'thin' },
  bottom: { style: 'thin' },
  right: { style: 'thin' },
}

function applyHeaderStyle(row: ExcelJS.Row) {
  row.eachCell((cell) => {
    cell.fill = HEADER_FILL
    cell.font = HEADER_FONT
    cell.border = BORDER_THIN
    cell.alignment = { vertical: 'middle', wrapText: true }
  })
  row.height = 28
}

function applyCellBorder(row: ExcelJS.Row) {
  row.eachCell((cell) => {
    cell.border = BORDER_THIN
  })
}

function entityInstitution(e: Entity): string {
  const raw = coalesceSnapshot<string>(e.snapshot, ['institution', 'canonical_name'])
  if (!raw?.trim()) return '—'
  return humanizeAccountLabel(raw)
}

function entityAccount(e: Entity): string {
  const raw = rawFinancialAccountDisplayLabel(e)
  return raw ? humanizeAccountLabel(raw) : '—'
}

function entityModeloBien(e: Entity): string {
  return String(
    coalesceSnapshot<string>(e.snapshot, ['modelo_bien', 'modelo_bien_hint']) ?? '',
  )
}

function entityCountry(e: Entity): string {
  return String(
    coalesceSnapshot<string>(e.snapshot, ['country', 'jurisdiction', 'jurisdiction_code']) ?? '',
  )
}

function entityCurrency(e: Entity): string {
  return String(snapshotField<string>(e.snapshot, 'currency') ?? '')
}

function entityRegistryId(e: Entity): string {
  return String(snapshotField<string>(e.snapshot, 'registry_id') ?? '')
}

interface ExportableAccount {
  bienNumber: number
  entity: Entity
  institution: string
  account: string
  modeloBien: string
  country: string
  currency: string
  registryId: string
  balanceOriginal: number
  balanceEur: number
  filingTag: string
}

function buildAccountRow(
  e: Entity,
  index: number,
  resolveUsdPerEur: UsdPerEurResolver,
  filingTag: string,
): ExportableAccount {
  const raw = getEntityRawStorageLegs(e)
  const ccy = entityCurrency(e).toUpperCase()
  const balanceOriginal = ccy === 'USD' ? raw.usd : raw.eur
  const balanceEur = getEntityMonetaryDisplayBasisEur(e, resolveUsdPerEur)

  return {
    bienNumber: index + 1,
    entity: e,
    institution: entityInstitution(e),
    account: entityAccount(e),
    modeloBien: entityModeloBien(e),
    country: entityCountry(e),
    currency: ccy || 'EUR',
    registryId: entityRegistryId(e),
    balanceOriginal,
    balanceEur,
    filingTag,
  }
}

function addBienesSheet(wb: ExcelJS.Workbook, rows: ExportableAccount[]) {
  const ws = wb.addWorksheet('Bienes', { properties: { tabColor: { argb: 'FF2D5F8A' } } })

  ws.columns = [
    { header: 'Bien #', key: 'bien', width: 8 },
    { header: 'Modelo', key: 'modelo', width: 10 },
    { header: 'Bien (workbook)', key: 'modeloBien', width: 22 },
    { header: 'Institution', key: 'institution', width: 24 },
    { header: 'Account', key: 'account', width: 28 },
    { header: 'Registry ID', key: 'registryId', width: 28 },
    { header: 'Country', key: 'country', width: 14 },
    { header: 'Currency', key: 'currency', width: 10 },
    { header: 'Balance (original)', key: 'balanceOrig', width: 20 },
    { header: 'Balance EUR', key: 'balanceEur', width: 18 },
  ]
  applyHeaderStyle(ws.getRow(1))

  for (const r of rows) {
    const dataRow = ws.addRow({
      bien: r.bienNumber,
      modelo: r.filingTag === '721' ? '721' : '720',
      modeloBien: r.modeloBien,
      institution: r.institution,
      account: r.account,
      registryId: r.registryId,
      country: r.country,
      currency: r.currency,
      balanceOrig: Math.round(r.balanceOriginal * 100) / 100,
      balanceEur: Math.round(r.balanceEur * 100) / 100,
    })
    applyCellBorder(dataRow)

    const eurCell = dataRow.getCell('balanceEur')
    eurCell.numFmt = '#,##0.00'
    const origCell = dataRow.getCell('balanceOrig')
    origCell.numFmt = '#,##0.00'
  }

  ws.autoFilter = { from: 'A1', to: `J${rows.length + 1}` }
}

function addCantidadesSheet(
  wb: ExcelJS.Workbook,
  rows: ExportableAccount[],
  taxYear: number,
  ecbRate: number,
) {
  const ws = wb.addWorksheet(`Cantidades ${taxYear}`, {
    properties: { tabColor: { argb: 'FF4CAF50' } },
  })

  ws.columns = [
    { header: 'Bien #', key: 'bien', width: 8 },
    { header: 'Modelo', key: 'modelo', width: 10 },
    { header: 'Bien (workbook)', key: 'modeloBien', width: 22 },
    { header: 'Institution', key: 'institution', width: 24 },
    { header: 'Currency', key: 'currency', width: 10 },
    { header: `Balance at 31 Dec ${taxYear}`, key: 'balance', width: 24 },
    { header: 'ECB rate (USD/EUR)', key: 'rate', width: 18 },
    { header: 'Value EUR', key: 'valueEur', width: 18 },
    { header: 'Notes', key: 'notes', width: 30 },
  ]
  applyHeaderStyle(ws.getRow(1))

  for (const r of rows) {
    const isUsd = r.currency === 'USD'
    const notes: string[] = []

    const status = snapshotField<string>(r.entity.snapshot, 'account_status')
    if (status === 'closed') notes.push('Closed / zero balance')

    const gestor = snapshotField<string>(r.entity.snapshot, 'gestor_treatment')
    if (gestor) notes.push(gestor)

    const declaration = snapshotField<string>(r.entity.snapshot, 'modelo_720_declaration')
    if (declaration) notes.push(declaration)

    const dataRow = ws.addRow({
      bien: r.bienNumber,
      modelo: r.filingTag === '721' ? '721' : '720',
      modeloBien: r.modeloBien,
      institution: r.institution,
      currency: r.currency,
      balance: Math.round(r.balanceOriginal * 100) / 100,
      rate: isUsd ? ecbRate : '',
      valueEur: Math.round(r.balanceEur * 100) / 100,
      notes: notes.join('; '),
    })
    applyCellBorder(dataRow)

    dataRow.getCell('balance').numFmt = '#,##0.00'
    dataRow.getCell('valueEur').numFmt = '#,##0.00'
    if (isUsd) dataRow.getCell('rate').numFmt = '0.00000'
  }

  ws.autoFilter = { from: 'A1', to: `I${rows.length + 1}` }

  const totalsRow = ws.addRow({
    bien: '',
    modelo: '',
    modeloBien: '',
    institution: 'TOTAL',
    currency: '',
    balance: '',
    rate: '',
    valueEur: rows.reduce((s, r) => s + Math.round(r.balanceEur * 100) / 100, 0),
    notes: '',
  })
  totalsRow.font = { bold: true }
  totalsRow.getCell('valueEur').numFmt = '#,##0.00'
  applyCellBorder(totalsRow)
}

function addConversionesSheet(
  wb: ExcelJS.Workbook,
  taxYear: number,
  ecbRate: number,
) {
  const ws = wb.addWorksheet(`Conversiones ${taxYear}`, {
    properties: { tabColor: { argb: 'FFFF9800' } },
  })

  ws.columns = [
    { header: 'Parameter', key: 'param', width: 32 },
    { header: 'Value', key: 'value', width: 28 },
  ]
  applyHeaderStyle(ws.getRow(1))

  const info = [
    ['Tax year', String(taxYear)],
    ['Reference date', `31 December ${taxYear}`],
    ['EUR conversion source', 'ECB via Frankfurter API'],
    ['USD → EUR rate', String(ecbRate)],
    ['Rate date', `${taxYear}-12-31`],
    ['Filing deadline (typical)', `31 March ${taxYear + 1}`],
    ['Generated', new Date().toISOString().split('T')[0]],
  ]

  for (const [param, value] of info) {
    const row = ws.addRow({ param, value })
    applyCellBorder(row)
  }
}

export interface ModeloExportOptions {
  taxYear: number
  accounts720: Entity[]
  accounts721: Entity[]
  equityRows: Entity[]
  resolveUsdPerEur: UsdPerEurResolver
  ecbRate: number
}

export async function buildModeloWorkbookBuffer(options: ModeloExportOptions): Promise<ArrayBuffer> {
  const { taxYear, accounts720, accounts721, equityRows, resolveUsdPerEur, ecbRate } = options

  const wb = new ExcelJS.Workbook()
  wb.creator = 'Finances Dashboard'
  wb.created = new Date()

  const allRows: ExportableAccount[] = []

  let idx = 0
  for (const e of accounts720) {
    allRows.push(buildAccountRow(e, idx++, resolveUsdPerEur, '720'))
  }
  for (const e of equityRows) {
    allRows.push(buildAccountRow(e, idx++, resolveUsdPerEur, 'equity'))
  }
  for (const e of accounts721) {
    allRows.push(buildAccountRow(e, idx++, resolveUsdPerEur, '721'))
  }

  addBienesSheet(wb, allRows)
  addCantidadesSheet(wb, allRows, taxYear, ecbRate)
  addConversionesSheet(wb, taxYear, ecbRate)

  return wb.xlsx.writeBuffer()
}

