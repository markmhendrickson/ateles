import { useMemo } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import { useEntity, useEntityRelationships } from '@/hooks/useEntity'
import { useObservations } from '@/hooks/useObservations'
import { useMaskMode } from '@/context/MaskModeContext'
import { useEntityFxRates } from '@/hooks/useEntityFxRates'
import { formatDate, snapshotField } from '@/lib/formatters'
import { MonetaryPair } from '@/components/MonetaryPair'
import { getEntityCanonicalEur } from '@/lib/aggregations'
import BalanceChart from '@/components/BalanceChart'
import EntityTimelineList from '@/components/EntityTimelineList'
import ObservationTimeline from '@/components/ObservationTimeline'
import ObservationSourcesSummary from '@/components/ObservationSourcesSummary'
import type { Entity, Observation, Relationship, SheetRow } from '@/types/neotoma'
import { useDetailBreadcrumbLabel } from '@/context/BreadcrumbContext'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import PropertyLabel from '@/components/PropertyLabel'
import DataCompletenessBar from '@/components/DataCompletenessBar'
import { DenominationBadge } from '@/components/DenominationBadge'
import ViewSourceButton from '@/components/ViewSourceButton'
import WorkflowStatusBadge from '@/components/WorkflowStatusBadge'
import { humanizePropertyKey } from '@/lib/propertyLabels'
import {
  humanizeRelationshipType,
  entityTypeLabel,
  entityDisplayName,
  humanizeAccountLabel,
  humanizeSource,
  isWorkflowStatusSnapshotKey,
} from '@/lib/humanize'
import { findLatestObservationForField } from '@/lib/snapshotFieldProvenance'
import { entityHref, resolveEntityType, isAccountsRouteEntityType, normalizeEntityTypeKey } from '@/lib/entityRoutes'

const ACCOUNT_LABEL_META_KEYS = new Set([
  'account_name',
  'display_name_en',
  'display_name_es',
  'institution',
])

const STATEMENT_EVIDENCE_KEYS = new Set([
  'last_statement_date',
  'statement_as_of_date',
  'statement_period_start',
  'statement_period_end',
  'statement_source_kind',
  'statement_pdf_path',
  'ending_account_value',
  'ending_account_value_currency',
  'ending_account_value_eur',
  'ending_account_value_usd',
])

function isStatementEvidenceKey(key: string): boolean {
  return STATEMENT_EVIDENCE_KEYS.has(key) || key.startsWith('statement_') || key.startsWith('ending_account_value')
}

function getRelationshipOtherEntity(rel: Relationship, currentId: string): Entity | null {
  if (rel.source_entity_id === currentId) return rel.target_entity ?? null
  if (rel.target_entity_id === currentId) return rel.source_entity ?? null
  return null
}

function maskChainAddress(raw: string, mask: boolean): string {
  if (!mask) return raw
  if (raw.startsWith('placeholder:') || raw.startsWith('koinly_wallet:')) return raw
  if (raw.length < 18) return '••••'
  return `${raw.slice(0, 10)}…${raw.slice(-6)}`
}

export default function AccountDetail() {
  const { id } = useParams<{ id: string }>()
  const { data: entity, isLoading, error } = useEntity(id)
  const observations = useObservations(id)
  const relationships = useEntityRelationships(id, { expandEntities: true })
  const { enabled: maskOn, text, freeform, deep, maskNumber } = useMaskMode()
  const { resolveUsdPerEur } = useEntityFxRates(entity ? [entity] : undefined)

  const titleBase = entity ? entityDisplayName(entity) : null
  const breadcrumbLabel = titleBase == null ? null : maskOn ? text(titleBase) : titleBase
  useDetailBreadcrumbLabel(breadcrumbLabel)

  const linkedCryptoWalletAddresses = useMemo(() => {
    if (!relationships.data || !id) return []
    return relationships.data
      .filter(rel => {
        if ((rel.relationship_type || '').toUpperCase() !== 'PART_OF') return false
        if (rel.target_entity_id !== id) return false
        const src = rel.source_entity
        return Boolean(src && normalizeEntityTypeKey(src.entity_type) === 'crypto_wallet_address')
      })
      .map(rel => {
        const addr = rel.source_entity as Entity
        return { rel, addr }
      })
  }, [relationships.data, id])

  if (isLoading) return <p className="text-sm text-muted-foreground animate-pulse py-8">Loading...</p>
  if (error) return <p className="text-sm text-destructive py-8">Error: {(error as Error).message}</p>
  if (!entity) return <p className="text-sm text-muted-foreground py-8">Not found</p>

  const resolvedType = resolveEntityType(entity)
  if (!isAccountsRouteEntityType(resolvedType)) {
    return <Navigate to={entityHref(entity)} replace />
  }

  const snap = entity.snapshot
  const rows = snapshotField<SheetRow[]>(snap, 'rows') ?? []
  const metaFields = snap
    ? Object.entries(snap).filter(([k]) => k !== 'rows' && k !== 'entity_id' && k !== 'entity_type')
    : []
  const accountFields =
    entity.entity_type === 'financial_account'
      ? metaFields.filter(([key]) => !isStatementEvidenceKey(key))
      : metaFields
  const statementEvidenceFields =
    entity.entity_type === 'financial_account'
      ? metaFields.filter(([key]) => isStatementEvidenceKey(key))
      : []
  const relatedStatements = (relationships.data ?? [])
    .map(rel => getRelationshipOtherEntity(rel, entity.entity_id))
    .filter((other): other is Entity => Boolean(other && other.entity_type === 'account_statement'))
    .sort((a, b) => {
      const da = String(snapshotField<string>(a.snapshot, 'statement_as_of_date') ?? snapshotField<string>(a.snapshot, 'statement_period_end') ?? '')
      const db = String(snapshotField<string>(b.snapshot, 'statement_as_of_date') ?? snapshotField<string>(b.snapshot, 'statement_period_end') ?? '')
      return db.localeCompare(da)
    })

  const title = entityDisplayName(entity)
  const displayTitle = maskOn ? text(title) : title

  const rowKeys =
    rows.length > 0
      ? Object.keys(rows[0]).filter(k => rows[0][k] && String(rows[0][k]).trim()).slice(0, 10)
      : []

  function formatMetaDd(key: string, val: unknown): string {
    if (key.includes('date') && typeof val === 'string') {
      return maskOn ? freeform(val) : formatDate(val)
    }
    if (Array.isArray(val)) return `[${maskOn ? maskNumber(val.length, `arr:${key}`) : val.length} items]`
    if (typeof val === 'object' && val !== null) {
      return JSON.stringify(maskOn ? deep(val) : val)
    }
    if (typeof val === 'number') {
      return String(maskOn ? maskNumber(val, `meta:${key}`) : val)
    }
    const s = String(val ?? '—')
    if (!maskOn && typeof val === 'string' && ACCOUNT_LABEL_META_KEYS.has(key)) {
      return humanizeAccountLabel(val)
    }
    return maskOn ? freeform(s) : s
  }

  function observationSourceHint(obs: Observation | null): string | null {
    if (!obs?.data || typeof obs.data !== 'object' || Array.isArray(obs.data)) return null
    const rec = obs.data as Record<string, unknown>
    const fields = ['statement_pdf_path', 'source_file', 'import_source_file', 'assets_sheet_source_file']
    for (const k of fields) {
      const v = rec[k]
      if (typeof v === 'string' && v.trim()) return v.trim()
    }
    return null
  }

  function renderMetaSection(sectionTitle: string, fields: [string, unknown][], obsForProv: Observation[] | undefined) {
    if (fields.length === 0) return null
    return (
      <section>
        <h2 className="text-lg font-medium mb-3">{sectionTitle}</h2>
        <p className="text-sm text-muted-foreground mb-3">
          Hover a value for where it last appeared in a stored update (when traceable), or why it may not show up in
          payloads below.
        </p>
        <Card className="overflow-hidden py-0">
          <CardContent className="p-0">
            <dl className="divide-y divide-border">
              {fields.map(([key, val]) => {
                const prov = findLatestObservationForField(obsForProv, key)
                const when = prov ? (prov.observed_at || prov.created_at) : ''
                const sourceLabel = prov
                  ? prov.source?.trim()
                    ? humanizeSource(prov.source)
                    : prov.source_id
                      ? 'Stored source file'
                      : 'Unspecified'
                  : 'Unspecified'
                const isStatusField = isWorkflowStatusSnapshotKey(key) && typeof val === 'string'
                const ddText = isStatusField ? '' : formatMetaDd(key, val)
                return (
                  <div key={key} className="flex px-4 py-2.5 gap-2">
                    <PropertyLabel
                      literalKey={key}
                      as="dt"
                      className="w-56 text-sm text-muted-foreground shrink-0"
                    />
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <dd
                          className={
                            isStatusField
                              ? 'text-sm break-all min-w-0'
                              : 'text-sm break-all min-w-0 cursor-help underline decoration-dotted decoration-muted-foreground/50 underline-offset-2'
                          }
                        >
                          {isStatusField ? (
                            <WorkflowStatusBadge value={val} maskOn={maskOn} text={text} />
                          ) : (
                            ddText
                          )}
                        </dd>
                      </TooltipTrigger>
                      <TooltipContent side="left" align="start" className="max-w-xs space-y-2 text-xs">
                        {prov ? (
                          <>
                            <p className="font-medium text-foreground">Last update that included this field</p>
                            <p>
                              {when
                                ? maskOn
                                  ? freeform(when)
                                  : formatDate(when)
                                : '—'}
                            </p>
                            <p>Source: {maskOn ? text(sourceLabel) : sourceLabel}</p>
                            {prov.observation_kind ? (
                              <p>
                                Update kind:{' '}
                                {maskOn
                                  ? text(humanizePropertyKey(prov.observation_kind))
                                  : humanizePropertyKey(prov.observation_kind)}
                              </p>
                            ) : null}
                            {prov.source_id && (
                              <ViewSourceButton
                                sourceId={prov.source_id}
                                sourceHint={observationSourceHint(prov)}
                                inline
                                label="View source file"
                              />
                            )}
                          </>
                        ) : (
                          <>
                            <p className="font-medium text-foreground">Source not found in loaded updates</p>
                            <p className="text-muted-foreground">
                              None of the observation payloads below list this field by name. The merged record can still
                              show it from server-side materialization, corrections, or updates not matching this key
                              in <span className="font-mono">data</span>.
                            </p>
                          </>
                        )}
                        <p className="text-muted-foreground border-t border-border pt-2 font-mono text-[11px]">
                          {key}
                        </p>
                        <a
                          href="#data-updates"
                          className="text-primary underline underline-offset-2 hover:no-underline"
                        >
                          Open data updates
                        </a>
                      </TooltipContent>
                    </Tooltip>
                  </div>
                )
              })}
            </dl>
          </CardContent>
        </Card>
      </section>
    )
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{displayTitle}</h1>
        <div className="text-muted-foreground text-sm mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
          <span title={entity.entity_type}>
            {maskOn ? text(humanizePropertyKey(entity.entity_type)) : humanizePropertyKey(entity.entity_type)}
          </span>
          {entity.entity_type === 'financial_account' && <DenominationBadge entity={entity} />}
          <MonetaryPair
            canonicalEur={getEntityCanonicalEur(entity, resolveUsdPerEur)}
            usdPerEur={resolveUsdPerEur(entity)}
            entity={entity}
            pairKey={id ? `acct-hdr-${id}` : 'acct-hdr'}
            align="left"
            layout="inline"
          />
        </div>
      </div>

      <DataCompletenessBar entityType={entity.entity_type} snapshot={snap} />

      {entity.entity_type === 'financial_account' && linkedCryptoWalletAddresses.length > 0 && (
        <section className="space-y-4">
          <div>
            <h2 className="text-lg font-medium mb-1">Wallet and custody addresses</h2>
            <p className="text-sm text-muted-foreground">
              On-chain, custodial (Koinly), or reporting-anchor records linked with{' '}
              <span className="font-mono text-xs">PART_OF</span> to this account.
            </p>
          </div>
          <div className="space-y-2">
            {linkedCryptoWalletAddresses.map(({ rel, addr }) => {
              const s = addr.snapshot ?? {}
              const chain = String(snapshotField<string>(s, 'chain_network') ?? '—')
              const purpose = String(snapshotField<string>(s, 'purpose') ?? '—')
              const rawAddr = String(snapshotField<string>(s, 'address') ?? '')
              const shown = maskChainAddress(rawAddr, maskOn)
              const title = entityDisplayName(addr)
              return (
                <Card key={rel.relationship_key ?? addr.entity_id}>
                  <CardContent className="flex flex-col gap-2 py-4 sm:flex-row sm:items-start sm:justify-between">
                    <div className="space-y-1 min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant="secondary" className="font-normal">
                          {maskOn ? text(chain) : chain}
                        </Badge>
                        <span className="text-xs text-muted-foreground">
                          {maskOn ? text(purpose) : purpose}
                        </span>
                      </div>
                      <div className="text-sm font-medium truncate">{maskOn ? text(title) : title}</div>
                      <div className="font-mono text-xs text-muted-foreground break-all">{maskOn ? freeform(shown) : shown}</div>
                    </div>
                    <Button variant="link" size="sm" className="h-auto p-0 shrink-0 self-start" asChild>
                      <Link to={entityHref(addr)}>Open record</Link>
                    </Button>
                  </CardContent>
                </Card>
              )
            })}
          </div>
        </section>
      )}

      {entity.entity_type === 'financial_account'
        ? renderMetaSection('Account properties', accountFields, observations.data)
        : renderMetaSection('Properties', metaFields, observations.data)}

      {entity.entity_type === 'financial_account' && (statementEvidenceFields.length > 0 || relatedStatements.length > 0) && (
        <section className="space-y-4">
          <div>
            <h2 className="text-lg font-medium mb-1">Statements</h2>
            <p className="text-sm text-muted-foreground">
              Account statements and supporting documentation for balance and activity records.
            </p>
          </div>

          {relatedStatements.length > 0 && (
            <div className="space-y-2">
              {relatedStatements.map(statement => {
                const statementTitle = entityDisplayName(statement)
                const asOf =
                  snapshotField<string>(statement.snapshot, 'statement_as_of_date') ??
                  snapshotField<string>(statement.snapshot, 'statement_period_end')
                const sourceKind = snapshotField<string>(statement.snapshot, 'statement_source_kind')
                return (
                  <Card key={statement.entity_id}>
                    <CardContent className="flex items-center justify-between gap-4 py-4">
                      <div className="space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant="secondary" className="font-normal">
                            Account statement
                          </Badge>
                          {asOf ? (
                            <span className="text-xs text-muted-foreground">
                              {maskOn ? freeform(asOf) : formatDate(asOf)}
                            </span>
                          ) : null}
                        </div>
                        <div className="text-sm font-medium">{maskOn ? text(statementTitle) : statementTitle}</div>
                        {sourceKind ? (
                          <div className="text-xs text-muted-foreground">
                            {maskOn ? text(sourceKind) : humanizePropertyKey(sourceKind)}
                          </div>
                        ) : null}
                      </div>
                      <Button variant="link" size="sm" className="h-auto p-0" asChild>
                        <Link to={entityHref(statement)}>Open</Link>
                      </Button>
                    </CardContent>
                  </Card>
                )
              })}
            </div>
          )}

          {renderMetaSection('Legacy statement fields on this account', statementEvidenceFields, observations.data)}
        </section>
      )}

      {rows.length > 0 && (
        <section>
          <h2 className="text-lg font-medium mb-3">
            Asset rows ({maskOn ? maskNumber(rows.length, 'rowsc') : rows.length})
          </h2>
          <div className="rounded-lg border border-border overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border bg-muted/50 hover:bg-muted/50">
                  {rowKeys.map(k => (
                    <TableHead key={k} className="h-auto py-2 whitespace-nowrap" title={k}>
                      {maskOn ? text(humanizePropertyKey(k)) : humanizePropertyKey(k)}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row, i) => (
                  <TableRow key={i} className="border-b border-border last:border-0">
                    {rowKeys.map(k => {
                      const raw = String(row[k] ?? '').trim() || '—'
                      const cell = maskOn ? freeform(raw) : raw
                      return (
                        <TableCell key={k} className="py-2 whitespace-nowrap max-w-[220px] truncate" title={cell}>
                          {cell}
                        </TableCell>
                      )
                    })}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </section>
      )}

      {observations.isLoading && (
        <p className="text-sm text-muted-foreground animate-pulse">Loading observations…</p>
      )}
      {observations.error && (
        <p className="text-sm text-destructive">
          Error loading observations: {(observations.error as Error).message}
        </p>
      )}

      {observations.data && observations.data.length > 1 && (
        <section>
          <h2 className="text-lg font-medium mb-3">Observation history</h2>
          <Card>
            <CardContent className="pt-6">
              <BalanceChart observations={observations.data} />
            </CardContent>
          </Card>
        </section>
      )}

      {relationships.data && relationships.data.length > 0 && (
        <section>
          <h2 className="text-lg font-medium mb-3">Related records</h2>
          <div className="space-y-2">
            {relationships.data.map(rel => {
              const other = getRelationshipOtherEntity(rel, entity.entity_id)
              const otherId = rel.source_entity_id === id ? rel.target_entity_id : rel.source_entity_id
              const otherName = other ? entityDisplayName(other) : otherId
              const otherType = other?.entity_type
              const href = other ? entityHref(other) : `/explorer?id=${encodeURIComponent(otherId)}`
              return (
                <Card key={rel.relationship_key}>
                  <CardContent className="flex items-center justify-between py-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant="secondary" className="font-normal" title={rel.relationship_type}>
                        {maskOn ? text(humanizeRelationshipType(rel.relationship_type)) : humanizeRelationshipType(rel.relationship_type)}
                      </Badge>
                      {otherType && (
                        <span className="text-xs text-muted-foreground">
                          {maskOn ? text(entityTypeLabel(otherType)) : entityTypeLabel(otherType)}
                        </span>
                      )}
                      <span className="text-sm font-medium">
                        {maskOn ? text(otherName) : otherName}
                      </span>
                    </div>
                    <Button variant="link" size="sm" className="h-auto p-0" asChild>
                      <Link to={href}>View</Link>
                    </Button>
                  </CardContent>
                </Card>
              )
            })}
          </div>
        </section>
      )}

      {observations.data && observations.data.length > 0 && (
        <ObservationSourcesSummary observations={observations.data} />
      )}

      {observations.data && (
        <section id="data-updates">
          <h2 className="text-lg font-medium mb-3">
            Data updates ({maskOn ? maskNumber(observations.data.length, 'obsc') : observations.data.length})
          </h2>
          <p className="text-sm text-muted-foreground mb-3">
            Newest first. Each entry shows source, id, idempotency key (if any), and payload.
          </p>
          <ObservationTimeline observations={observations.data} />
        </section>
      )}

      <EntityTimelineList entityId={entity.entity_id} />

      <details className="group">
        <summary className="text-sm text-muted-foreground cursor-pointer hover:text-foreground transition-colors">
          Technical details
        </summary>
        <div className="mt-3">
          <Card className="overflow-hidden py-0">
            <CardContent className="p-0">
              <pre className="text-xs font-mono px-4 py-3 overflow-x-auto max-h-[min(70vh,32rem)] overflow-y-auto bg-muted/30 border-t border-border">
                {JSON.stringify(maskOn ? deep(entity) : entity, null, 2)}
              </pre>
            </CardContent>
          </Card>
        </div>
      </details>
    </div>
  )
}
