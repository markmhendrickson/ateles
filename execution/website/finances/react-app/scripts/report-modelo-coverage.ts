#!/usr/bin/env tsx
import { existsSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'
import type { Entity } from '../src/types/neotoma'
import { normalizeFilingTags, snapshotField } from '../src/lib/formatters'
import { collectModeloScope, findModeloCoverageIssues } from '../src/lib/modeloScope'
import { getEntityRawStorageLegs } from '../src/lib/aggregations'

interface Args {
  taxYear: number
  apiUrl: string
  token?: string
  checklistPath?: string
  json: boolean
  repair: boolean
}

interface ChecklistRegistrySets {
  ids720: Set<string>
  ids721: Set<string>
}

function parseArgs(argv: string[]): Args {
  const nowYear = new Date().getFullYear() - 1
  const args: Args = {
    taxYear: nowYear,
    apiUrl: process.env.VITE_NEOTOMA_API_URL || 'http://localhost:3180',
    token: process.env.VITE_NEOTOMA_TOKEN,
    checklistPath: undefined,
    json: false,
    repair: false,
  }

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--tax-year' && argv[i + 1]) args.taxYear = Number(argv[++i])
    else if (a === '--api-url' && argv[i + 1]) args.apiUrl = argv[++i]
    else if (a === '--token' && argv[i + 1]) args.token = argv[++i]
    else if (a === '--checklist' && argv[i + 1]) args.checklistPath = path.resolve(argv[++i])
    else if (a === '--json') args.json = true
    else if (a === '--repair') args.repair = true
    else if (a === '--help' || a === '-h') {
      console.log(
        [
          'Usage: npm run report:modelo-coverage -- --tax-year 2025 [--json] [--repair]',
          '',
          'Options:',
          '  --tax-year <year>     Tax year context (default: previous year)',
          '  --api-url <url>       Neotoma API base (default: VITE_NEOTOMA_API_URL || http://localhost:3180)',
          '  --token <token>       Optional Bearer token (default: VITE_NEOTOMA_TOKEN)',
          '  --checklist <path>    Optional override checklist path',
          '  --json                Emit machine-readable JSON output',
          '  --repair              Idempotently store corrected observations for entities with missing contract fields',
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

function findRepoRoot(startDir: string): string | null {
  let d = path.resolve(startDir)
  for (let i = 0; i < 14; i++) {
    const gitPath = path.join(d, '.git')
    if (existsSync(gitPath)) return d
    const parent = path.dirname(d)
    if (parent === d) break
    d = parent
  }
  return null
}

async function fetchEntitiesPage(
  apiUrl: string,
  token: string | undefined,
  limit: number,
  offset: number,
): Promise<{ entities: Entity[]; total: number }> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`

  const res = await fetch(`${apiUrl.replace(/\/$/, '')}/entities/query`, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      entity_type: 'financial_account',
      include_snapshots: true,
      limit,
      offset,
    }),
  })
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

function extractSection(md: string, startMarker: string, endMarker: string): string {
  const start = md.indexOf(startMarker)
  if (start < 0) return ''
  const after = md.slice(start + startMarker.length)
  const end = after.indexOf(endMarker)
  return end >= 0 ? after.slice(0, end) : after
}

function extractRegistryIdsFromSection(section: string): Set<string> {
  const ids = new Set<string>()
  const lineRe = /^\|\s*\[[xX ]\]\s*\|\s*`([^`]+)`\s*\|/gm
  for (const m of section.matchAll(lineRe)) {
    ids.add(String(m[1]).trim())
  }
  return ids
}

async function parseChecklistRegistryIds(explicitPath?: string): Promise<ChecklistRegistrySets> {
  let checklistPath = explicitPath
  if (!checklistPath) {
    const repoRoot = findRepoRoot(process.cwd())
    if (!repoRoot) throw new Error('Cannot determine repo root to load Modelo checklist.')
    checklistPath = path.join(repoRoot, 'docs/private/finances/modelo_2025_prep_checklist.md')
  }

  const md = await readFile(checklistPath, 'utf8')
  const sectionA = extractSection(md, '## A — Modelo **720**', '## B — Modelo **721**')
  const sectionB = extractSection(md, '## B — Modelo **721**', '## C — Workbook, gestor, crosswalk')

  return {
    ids720: extractRegistryIdsFromSection(sectionA),
    ids721: (() => {
      const ids = extractRegistryIdsFromSection(sectionB)
      // Optional checklist note outside the table.
      if (sectionB.includes('`custody_kraken`')) ids.add('custody_kraken')
      return ids
    })(),
  }
}

function registryId(entity: Entity): string {
  return String(snapshotField<string>(entity.snapshot, 'registry_id') ?? '')
}

function toSortedArray(ids: Iterable<string>): string[] {
  return Array.from(new Set(ids)).filter(Boolean).sort()
}

async function main() {
  const args = parseArgs(process.argv.slice(2))
  const entities = await fetchAllFinancialAccounts(args.apiUrl, args.token)
  const checklist = await parseChecklistRegistryIds(args.checklistPath)
  const scope = collectModeloScope(entities, args.taxYear)
  const coverageIssues = findModeloCoverageIssues(scope)
  const uniqueCoverageIssues = Array.from(
    new Map(
      coverageIssues.map((i) => [`${i.registryId || i.entityId}:${i.issue}`, i] as const),
    ).values(),
  )
  const byRegistry = new Map<string, Entity>()
  for (const e of entities) {
    const id = registryId(e)
    if (id && !byRegistry.has(id)) byRegistry.set(id, e)
  }

  const inScopeIds = new Set(scope.allRows.map(registryId).filter(Boolean))
  const missingChecklist720 = toSortedArray([...checklist.ids720].filter((id) => !byRegistry.has(id)))
  const missingChecklist721 = toSortedArray([...checklist.ids721].filter((id) => !byRegistry.has(id)))
  const presentButNotScoped = toSortedArray(
    [...byRegistry.keys()].filter((id) => (checklist.ids720.has(id) || checklist.ids721.has(id)) && !inScopeIds.has(id)),
  )
  const scopeTaggedNotInChecklist = toSortedArray(
    [...inScopeIds].filter((id) => !checklist.ids720.has(id) && !checklist.ids721.has(id)),
  )
  const expectedCustody = [...checklist.ids721].filter((id) => id.startsWith('custody_'))
  const missingCustody = expectedCustody.filter((id) => !byRegistry.has(id)).sort()

  const parityIssues: Array<{ registryId: string; entityId: string; field: string; issue: string }> = []
  for (const e of scope.allRows) {
    const rid = registryId(e)
    const tags = normalizeFilingTags(e.snapshot)
    const raw = getEntityRawStorageLegs(e)

    if (!snapshotField(e.snapshot, 'tax_year_context') && !snapshotField(e.snapshot, 'tax_year')) {
      parityIssues.push({ registryId: rid, entityId: e.entity_id, field: 'tax_year_context', issue: 'missing both tax_year_context and tax_year' })
    }
    if (tags.length === 0) {
      parityIssues.push({ registryId: rid, entityId: e.entity_id, field: 'filing_tags', issue: 'no filing_tags' })
    }
    if (!snapshotField(e.snapshot, 'balance_date') && !snapshotField(e.snapshot, 'last_statement_date')) {
      parityIssues.push({ registryId: rid, entityId: e.entity_id, field: 'balance_date', issue: 'no balance_date or last_statement_date (FX rate will use fallback)' })
    }
    const hasBalance = Math.abs(raw.eur) > 1e-9 || Math.abs(raw.usd) > 1e-9
    const closed = String(snapshotField<string>(e.snapshot, 'account_status') ?? '').toLowerCase() === 'closed'
    if (!hasBalance && !closed) {
      parityIssues.push({ registryId: rid, entityId: e.entity_id, field: 'balance', issue: 'zero EUR and USD balance on active account' })
    }
  }

  const report = {
    tax_year: args.taxYear,
    checklist: {
      expected_720: checklist.ids720.size,
      expected_721: checklist.ids721.size,
      expected_custody_rows: expectedCustody.length,
    },
    neotoma: {
      financial_account_entities: entities.length,
      modelo_scope_rows: scope.allRows.length,
      scope_720_rows: scope.accounts720.length,
      scope_721_rows: scope.accounts721.length,
      scope_equity_rows: scope.equityRows.length,
    },
    gaps: {
      missing_checklist_720_in_neotoma: missingChecklist720,
      missing_checklist_721_in_neotoma: missingChecklist721,
      missing_custody_rows_in_neotoma: missingCustody,
      checklist_rows_present_but_not_in_current_scope: presentButNotScoped,
      scope_rows_not_listed_in_checklist: scopeTaggedNotInChecklist,
      scope_data_quality_issues: uniqueCoverageIssues,
    },
    parity: {
      field_issues: parityIssues,
      critical_count: parityIssues.filter(i => i.field === 'filing_tags' || i.field === 'balance').length,
    },
  }

  if (args.json) {
    console.log(JSON.stringify(report, null, 2))
    await maybeRunRepair(args, scope)
    return
  }

  console.log(`Modelo coverage report — tax year ${args.taxYear}`)
  console.log('')
  console.log(`Checklist expected: 720=${report.checklist.expected_720}, 721=${report.checklist.expected_721}, custody=${report.checklist.expected_custody_rows}`)
  console.log(`Neotoma scope: 720=${report.neotoma.scope_720_rows}, 721=${report.neotoma.scope_721_rows}, equity=${report.neotoma.scope_equity_rows}, total=${report.neotoma.modelo_scope_rows}`)
  console.log('')
  console.log(`Missing checklist 720 in Neotoma: ${missingChecklist720.length}`)
  for (const id of missingChecklist720.slice(0, 20)) console.log(`  - ${id}`)
  if (missingChecklist720.length > 20) console.log(`  ...and ${missingChecklist720.length - 20} more`)
  console.log('')
  console.log(`Missing checklist 721 in Neotoma: ${missingChecklist721.length}`)
  for (const id of missingChecklist721.slice(0, 20)) console.log(`  - ${id}`)
  if (missingChecklist721.length > 20) console.log(`  ...and ${missingChecklist721.length - 20} more`)
  console.log('')
  console.log(`Missing custody_* rows in Neotoma: ${missingCustody.length}`)
  for (const id of missingCustody.slice(0, 20)) console.log(`  - ${id}`)
  if (missingCustody.length > 20) console.log(`  ...and ${missingCustody.length - 20} more`)
  console.log('')
  console.log(`Coverage issues in scoped rows: ${uniqueCoverageIssues.length}`)
  for (const issue of uniqueCoverageIssues.slice(0, 25)) {
    console.log(`  - ${issue.registryId || issue.entityId}: ${issue.issue}`)
  }
  if (uniqueCoverageIssues.length > 25) console.log(`  ...and ${uniqueCoverageIssues.length - 25} more`)
  console.log('')
  console.log(`Parity field issues: ${parityIssues.length} (critical: ${report.parity.critical_count})`)
  for (const issue of parityIssues.slice(0, 25)) {
    console.log(`  - ${issue.registryId || issue.entityId}: [${issue.field}] ${issue.issue}`)
  }
  if (parityIssues.length > 25) console.log(`  ...and ${parityIssues.length - 25} more`)
  if (report.parity.critical_count > 0) {
    console.log('')
    console.log(`⚠ ${report.parity.critical_count} critical parity issue(s) require attention before sign-off.`)
  }

  await maybeRunRepair(args, scope)
}

interface RepairPatch {
  entity_id: string
  registry_id: string
  patches: Record<string, unknown>
}

function buildRepairPatches(scope: ReturnType<typeof collectModeloScope>, taxYear: number): RepairPatch[] {
  const patches: RepairPatch[] = []

  for (const e of scope.allRows) {
    const snap = e.snapshot ?? {}
    const rid = String(snapshotField<string>(snap, 'registry_id') ?? '')
    const patch: Record<string, unknown> = {}

    if (!snapshotField(snap, 'tax_year_context') && snapshotField(snap, 'tax_year') != null) {
      patch.tax_year_context = Number(snapshotField(snap, 'tax_year'))
    }

    if (!snapshotField(snap, 'balance_date')) {
      const dateKeys = ['last_statement_date', 'statement_as_of_date', 'statement_period_end', 'assets_sheet_as_of_date'] as const
      for (const k of dateKeys) {
        const v = snap[k]
        if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}/.test(v.trim())) {
          patch.balance_date = v.trim().slice(0, 10)
          break
        }
      }
      if (!patch.balance_date && snapshotField(snap, 'tax_year_context') != null) {
        patch.balance_date = `${snapshotField(snap, 'tax_year_context')}-12-31`
      }
    }

    const tags = normalizeFilingTags(snap)
    if (tags.length > 0 && !Array.isArray(snap.filing_tags)) {
      patch.filing_tags = tags
    }

    if (Object.keys(patch).length > 0) {
      patches.push({ entity_id: e.entity_id, registry_id: rid, patches: patch })
    }
  }

  return patches
}

async function storeRepairObservations(
  patches: RepairPatch[],
  apiUrl: string,
  token: string | undefined,
  taxYear: number,
): Promise<number> {
  if (patches.length === 0) return 0

  const entities = patches.map((p) => ({
    entity_type: 'financial_account',
    registry_id: p.registry_id,
    ...p.patches,
  }))

  const batchSize = 50
  let stored = 0
  for (let i = 0; i < entities.length; i += batchSize) {
    const batch = entities.slice(i, i + batchSize)
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (token) headers.Authorization = `Bearer ${token}`

    const res = await fetch(`${apiUrl.replace(/\/$/, '')}/entities/store`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        entities: batch,
        idempotency_key: `modelo-audit-repair-${taxYear}-batch-${i}`,
      }),
    })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      console.error(`Repair store failed for batch at offset ${i}: ${res.status} ${body}`)
      continue
    }
    stored += batch.length
  }
  return stored
}

async function maybeRunRepair(
  args: Args,
  scope: ReturnType<typeof collectModeloScope>,
) {
  if (!args.repair) return

  const patches = buildRepairPatches(scope, args.taxYear)
  if (patches.length === 0) {
    console.log('\nNo repair patches needed — all scoped entities pass contract checks.')
    return
  }

  console.log(`\nRepair: ${patches.length} entities need field corrections.`)
  for (const p of patches.slice(0, 10)) {
    console.log(`  - ${p.registry_id || p.entity_id}: ${Object.keys(p.patches).join(', ')}`)
  }
  if (patches.length > 10) console.log(`  ...and ${patches.length - 10} more`)

  const stored = await storeRepairObservations(patches, args.apiUrl, args.token, args.taxYear)
  console.log(`Repair complete: stored ${stored}/${patches.length} corrected observations.`)
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err))
  process.exit(1)
})

