import { useState, useMemo, useEffect, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { listSchemas } from '@/api/schemas'
import { getStats } from '@/api/stats'
import { getEntity } from '@/api/entities'
import { useEntities } from '@/hooks/useEntities'
import { formatDate } from '@/lib/formatters'
import { entityTypeLabel, entityDisplayName } from '@/lib/humanize'
import EntityTimelineList from '@/components/EntityTimelineList'
import ObservationTimeline from '@/components/ObservationTimeline'
import ObservationSourcesSummary from '@/components/ObservationSourcesSummary'
import { useObservations } from '@/hooks/useObservations'
import { useMaskMode } from '@/context/MaskModeContext'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { getAccountDenomination, type AccountDenominationKind } from '@/lib/accountDenomination'
import FinancialDenominationFilter from '@/components/FinancialDenominationFilter'
import { DenominationBadge } from '@/components/DenominationBadge'
import { humanizePropertyKey } from '@/lib/propertyLabels'
import type { Entity } from '@/types/neotoma'
import { prepareFinancialAccountList } from '@/lib/financialAccountDedup'

export default function EntityExplorer() {
  const [searchParams, setSearchParams] = useSearchParams()
  const typeFromUrl = searchParams.get('type') ?? ''
  const idFromUrl = searchParams.get('id') ?? ''

  const [selectedType, setSelectedType] = useState(typeFromUrl)
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(idFromUrl || null)
  const [accountDenomFilter, setAccountDenomFilter] = useState<AccountDenominationKind | 'all'>('all')
  const [accountSearchQuery, setAccountSearchQuery] = useState('')
  const { text, freeform, deep, maskNumber, enabled: maskOn } = useMaskMode()

  useEffect(() => {
    if (typeFromUrl) setSelectedType(typeFromUrl)
    if (idFromUrl) setSelectedEntityId(idFromUrl)
  }, [typeFromUrl, idFromUrl])

  useEffect(() => {
    setAccountDenomFilter('all')
    setAccountSearchQuery('')
  }, [selectedType])

  const schemas = useQuery({ queryKey: ['schemas'], queryFn: listSchemas, staleTime: 120_000 })
  const stats = useQuery({ queryKey: ['stats'], queryFn: getStats, staleTime: 120_000 })
  const entities = useEntities(
    { entity_type: selectedType, include_snapshots: true, limit: 100 },
    !!selectedType,
  )

  const entityFromList = useMemo(
    () => entities.data?.entities.find(e => e.entity_id === selectedEntityId),
    [entities.data?.entities, selectedEntityId],
  )
  const entityById = useQuery({
    queryKey: ['entity', selectedEntityId],
    queryFn: () => getEntity(selectedEntityId!),
    enabled: !!selectedEntityId && entityFromList == null,
    staleTime: 60_000,
  })

  const detailEntity: Entity | undefined = entityFromList ?? entityById.data

  const observations = useObservations(selectedEntityId ?? undefined)

  useEffect(() => {
    if (!idFromUrl || typeFromUrl) return
    const t = detailEntity?.entity_type
    if (!t) return
    setSearchParams(
      prev => {
        const next = new URLSearchParams(prev)
        next.set('id', idFromUrl)
        next.set('type', t)
        return next
      },
      { replace: true },
    )
  }, [idFromUrl, typeFromUrl, detailEntity?.entity_type, setSearchParams])

  const typeOptions = useMemo(() => {
    const schemaTypes = schemas.data?.map(s => s.entity_type) ?? []
    const statsTypes = Object.keys(stats.data?.entities_by_type ?? {})
    const all = new Set([...schemaTypes, ...statsTypes])
    return Array.from(all).sort()
  }, [schemas.data, stats.data])

  const listEntities = useMemo(() => {
    const raw = entities.data?.entities ?? []
    if (selectedType !== 'financial_account') return raw
    let list = prepareFinancialAccountList(raw)
    if (accountDenomFilter !== 'all') {
      list = list.filter((e) => getAccountDenomination(e).kind === accountDenomFilter)
    }
    const q = accountSearchQuery.trim().toLowerCase()
    if (q) {
      list = list.filter(
        (e) =>
          entityDisplayName(e).toLowerCase().includes(q) ||
          String(e.entity_id).toLowerCase().includes(q),
      )
    }
    return list
  }, [entities.data?.entities, selectedType, accountDenomFilter, accountSearchQuery])

  const onSelectType = useCallback((v: string) => {
    setSelectedType(v)
    setSelectedEntityId(null)
  }, [])

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Entity explorer</h1>
        <p className="text-muted-foreground text-sm mt-1">Browse any entity type</p>
      </div>

      <div className="flex flex-wrap items-center gap-4">
        <Select value={selectedType || undefined} onValueChange={onSelectType}>
          <SelectTrigger className="w-[min(100%,280px)] min-w-[200px]">
            <SelectValue placeholder="Select record type..." />
          </SelectTrigger>
          <SelectContent>
            {typeOptions.map(t => (
              <SelectItem key={t} value={t} title={t}>
                {maskOn ? text(entityTypeLabel(t)) : entityTypeLabel(t)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {(schemas.isLoading || stats.isLoading) && (
          <span className="text-sm text-muted-foreground animate-pulse">Loading types...</span>
        )}
        {entities.data && (
          <span className="text-sm text-muted-foreground">
            {selectedType === 'financial_account' && listEntities.length !== (entities.data.entities?.length ?? 0)
              ? `${maskNumber(listEntities.length, 'ex-shown')} of ${maskNumber(entities.data.entities.length, 'ex-tot')}`
              : maskNumber(entities.data.total, 'ex-tot')}{' '}
            records
          </span>
        )}
      </div>

      {selectedType === 'financial_account' && entities.data && entities.data.entities.length > 0 && (
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
          <FinancialDenominationFilter value={accountDenomFilter} onChange={setAccountDenomFilter} />
          <Input
            type="search"
            placeholder="Search name or id…"
            value={accountSearchQuery}
            onChange={(e) => setAccountSearchQuery(e.target.value)}
            className="h-9 max-w-xs text-sm"
            aria-label="Filter financial accounts"
          />
        </div>
      )}

      {entities.isLoading && <p className="text-sm text-muted-foreground animate-pulse">Loading entities...</p>}
      {entities.error && <p className="text-sm text-destructive">Error: {(entities.error as Error).message}</p>}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {entities.data && entities.data.entities.length > 0 && (
          <div className="space-y-2 max-h-[70vh] overflow-y-auto">
            {listEntities.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4">No records match the current filters.</p>
            ) : (
              listEntities.map(entity => (
              <Button
                key={entity.entity_id}
                variant="outline"
                className={cn(
                  'h-auto w-full justify-start whitespace-normal border p-3 text-left font-normal',
                  selectedEntityId === entity.entity_id && 'border-primary bg-primary/5',
                )}
                onClick={() => setSelectedEntityId(entity.entity_id)}
              >
                <div className="flex w-full flex-col items-stretch gap-1">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-medium truncate">{text(entityDisplayName(entity))}</div>
                      {entity.entity_type === 'financial_account' && (
                        <div className="mt-1.5">
                          <DenominationBadge entity={entity} />
                        </div>
                      )}
                    </div>
                    <span className="text-xs text-muted-foreground shrink-0">
                      {maskOn
                        ? freeform(String(entity.updated_at || entity.last_observation_at || ''))
                        : formatDate(entity.updated_at || entity.last_observation_at)}
                    </span>
                  </div>
                  {entity.snapshot && (
                    <p className="text-xs text-muted-foreground truncate text-left">
                      {Object.entries(entity.snapshot)
                        .filter(([k]) => !['entity_id', 'entity_type', 'rows'].includes(k))
                        .slice(0, 3)
                        .map(([k, v], idx, arr) => (
                          <span key={k}>
                            <span title={k}>
                              {maskOn ? text(humanizePropertyKey(k)) : humanizePropertyKey(k)}
                            </span>
                            : {freeform(String(v).substring(0, 30))}
                            {idx < arr.length - 1 ? ' · ' : ''}
                          </span>
                        ))}
                    </p>
                  )}
                </div>
              </Button>
            ))
            )}
          </div>
        )}

        {selectedEntityId && (
          <div className="space-y-4">
            {entityById.isLoading && !detailEntity && (
              <p className="text-sm text-muted-foreground animate-pulse">Loading entity…</p>
            )}
            {entityById.error && !detailEntity && (
              <p className="text-sm text-destructive">Error: {(entityById.error as Error).message}</p>
            )}
            {detailEntity && (
              <>
                <Card>
                  <CardHeader className="pb-2">
                    <h3 className="text-sm font-medium">Snapshot</h3>
                  </CardHeader>
                  <CardContent>
                    <pre className="text-xs overflow-x-auto whitespace-pre-wrap text-foreground/80 max-h-96 overflow-y-auto">
                      {JSON.stringify(maskOn ? deep(detailEntity.snapshot) : detailEntity.snapshot, null, 2)}
                    </pre>
                  </CardContent>
                </Card>
                {observations.isLoading && (
                  <p className="text-sm text-muted-foreground animate-pulse">Loading observations…</p>
                )}
                {observations.error && (
                  <p className="text-sm text-destructive">
                    Error loading observations: {(observations.error as Error).message}
                  </p>
                )}
                {observations.data && observations.data.length > 0 && (
                  <ObservationSourcesSummary observations={observations.data} />
                )}
                {observations.data && (
                  <div>
                    <h3 className="text-sm font-medium mb-2">
                      All observations ({maskNumber(observations.data.length, 'exo')})
                    </h3>
                    <ObservationTimeline observations={observations.data} />
                  </div>
                )}
                <EntityTimelineList entityId={selectedEntityId} />
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
