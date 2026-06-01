#!/usr/bin/env tsx
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'
import type { Entity } from '../src/types/neotoma'
import { buildModeloWorkbookBuffer } from '../src/lib/modeloWorkbookBuilder'
import { collectModeloScope, findModeloCoverageIssues } from '../src/lib/modeloScope'
import { getEntityFxAsOfDate } from '../src/lib/entityFxDate'
import type { UsdPerEurResolver } from '../src/lib/aggregations'

interface Args {
  taxYear: number
  apiUrl: string
  token?: string
  outPath: string
  allowIncomplete: boolean
}

async function fetchWithTimeout(url: string, init: RequestInit = {}, timeoutMs = 20000): Promise<Response> {
  const ac = new AbortController()
  const timer = setTimeout(() => ac.abort(), timeoutMs)
  try {
    return await fetch(url, { ...init, signal: ac.signal })
  } finally {
    clearTimeout(timer)
  }
}

function parseArgs(argv: string[]): Args {
  const nowYear = new Date().getFullYear() - 1
  const outDefault = path.resolve(process.cwd(), `Modelos_720_721_${nowYear}.xlsx`)
  const args: Args = {
    taxYear: nowYear,
    apiUrl: process.env.VITE_NEOTOMA_API_URL || 'http://localhost:3180',
    token: process.env.VITE_NEOTOMA_TOKEN,
    outPath: outDefault,
    allowIncomplete: false,
  }

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--tax-year' && argv[i + 1]) args.taxYear = Number(argv[++i])
    else if (a === '--api-url' && argv[i + 1]) args.apiUrl = argv[++i]
    else if (a === '--token' && argv[i + 1]) args.token = argv[++i]
    else if (a === '--out' && argv[i + 1]) args.outPath = path.resolve(argv[++i])
    else if (a === '--allow-incomplete') args.allowIncomplete = true
    else if (a === '--help' || a === '-h') {
      console.log(
        [
          'Usage: npm run export:modelo -- --tax-year 2025 [--out ./Modelos_720_721_2025.xlsx]',
          '',
          'Options:',
          '  --tax-year <year>        Tax year context (default: previous year)',
          '  --api-url <url>          Neotoma API base (default: VITE_NEOTOMA_API_URL || http://localhost:3180)',
          '  --token <token>          Optional Bearer token (default: VITE_NEOTOMA_TOKEN)',
          '  --out <path>             Output xlsx path',
          '  --allow-incomplete       Export even if Neotoma coverage checks fail',
        ].join('\n'),
      )
      process.exit(0)
    }
  }

  if (!Number.isInteger(args.taxYear) || args.taxYear < 2000 || args.taxYear > 2100) {
    throw new Error(`Invalid --tax-year: ${args.taxYear}`)
  }
  return args
}

async function fetchEntitiesPage(
  apiUrl: string,
  token: string | undefined,
  limit: number,
  offset: number,
): Promise<{ entities: Entity[]; total: number }> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`

  const res = await fetchWithTimeout(
    `${apiUrl.replace(/\/$/, '')}/entities/query`,
    {
      method: 'POST',
      headers,
      body: JSON.stringify({
        entity_type: 'financial_account',
        include_snapshots: true,
        limit,
        offset,
      }),
    },
    20000,
  )
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`entities/query failed: ${res.status} ${res.statusText} ${body}`)
  }
  const j = (await res.json()) as { entities?: Entity[]; data?: Entity[]; total?: number }
  const entities = j.entities || j.data || []
  return { entities, total: j.total ?? entities.length }
}

async function fetchAllFinancialAccounts(apiUrl: string, token?: string): Promise<Entity[]> {
  const pageSize = 1000
  let offset = 0
  let total = Number.POSITIVE_INFINITY
  const out: Entity[] = []

  while (offset < total) {
    const page = await fetchEntitiesPage(apiUrl, token, pageSize, offset)
    total = page.total
    out.push(...page.entities)
    if (page.entities.length < pageSize) break
    offset += pageSize
  }

  return out
}

async function fetchUsdPerEur(isoDate: string): Promise<number> {
  const qs = new URLSearchParams({ from: 'EUR', to: 'USD' })
  const res = await fetchWithTimeout(`https://api.frankfurter.app/${isoDate}?${qs}`, {}, 15000)
  if (!res.ok) throw new Error(`Frankfurter HTTP ${res.status}`)
  const j = (await res.json()) as { rates?: { USD?: number } }
  const usd = j.rates?.USD
  if (typeof usd !== 'number' || !Number.isFinite(usd) || usd <= 0) {
    throw new Error(`Frankfurter invalid USD rate for ${isoDate}`)
  }
  return usd
}

async function buildResolver(entities: Entity[], fallbackUsdPerEur: number): Promise<UsdPerEurResolver> {
  const uniqueDates = Array.from(
    new Set(
      entities
        .map((e) => getEntityFxAsOfDate(e))
        .filter((d): d is string => Boolean(d)),
    ),
  ).sort()
  const rates = new Map<string, number>()

  for (const d of uniqueDates) {
    rates.set(d, await fetchUsdPerEur(d))
  }

  return (entity) => {
    const d = getEntityFxAsOfDate(entity)
    if (d && rates.has(d)) return rates.get(d)!
    return fallbackUsdPerEur
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2))
  const entities = await fetchAllFinancialAccounts(args.apiUrl, args.token)
  const scope = collectModeloScope(entities, args.taxYear)

  if (scope.allRows.length === 0) {
    throw new Error(`No filing-scoped financial_account entities found for tax year ${args.taxYear}`)
  }

  const issues = findModeloCoverageIssues(scope)
  if (issues.length > 0 && !args.allowIncomplete) {
    const preview = issues
      .slice(0, 12)
      .map((i) => `- ${i.registryId || i.entityId}: ${i.issue}`)
      .join('\n')
    throw new Error(
      `Neotoma coverage check failed with ${issues.length} issue(s). ` +
        `Fix data first or rerun with --allow-incomplete.\n${preview}`,
    )
  }

  const yearEnd = `${args.taxYear}-12-31`
  const usdPerEurYearEnd = await fetchUsdPerEur(yearEnd)
  const resolveUsdPerEur = await buildResolver(scope.allRows, usdPerEurYearEnd)
  const ecbRateUsdToEur = 1 / usdPerEurYearEnd

  const workbook = await buildModeloWorkbookBuffer({
    taxYear: args.taxYear,
    accounts720: scope.accounts720,
    accounts721: scope.accounts721,
    equityRows: scope.equityRows,
    resolveUsdPerEur,
    ecbRate: ecbRateUsdToEur,
  })

  await mkdir(path.dirname(args.outPath), { recursive: true })
  await writeFile(args.outPath, Buffer.from(workbook))

  console.log(
    [
      `Wrote ${args.outPath}`,
      `Rows: 720=${scope.accounts720.length}, equity=${scope.equityRows.length}, 721=${scope.accounts721.length}`,
      `FX: ${yearEnd} USD/EUR=${usdPerEurYearEnd.toFixed(5)} (ECB via Frankfurter)`,
      `Coverage issues: ${issues.length}${issues.length > 0 ? ' (exported with --allow-incomplete)' : ''}`,
    ].join('\n'),
  )
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err))
  process.exit(1)
})

