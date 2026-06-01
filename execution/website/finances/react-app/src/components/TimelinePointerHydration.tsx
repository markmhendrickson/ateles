import { Link } from 'react-router-dom'
import { useEntity } from '@/hooks/useEntity'
import { snapshotPathValue } from '@/lib/formatters'
import { useMaskMode } from '@/context/MaskModeContext'
import { entityDisplayName } from '@/lib/humanize'
import { entityHref } from '@/lib/entityRoutes'
import type { TimelineEvent } from '@/types/neotoma'

function pascalToSnakeCase(s: string): string {
  const t = s.trim()
  if (!t) return ''
  return t
    .replace(/([A-Z])/g, '_$1')
    .toLowerCase()
    .replace(/^_/, '')
}

interface Props {
  event: TimelineEvent
}

export default function TimelinePointerHydration({ event }: Props) {
  const entityId = event.entity_id?.trim() || undefined
  const embeddedEntity = event.entity
  const { data: fetchedEntity, isLoading, error } = useEntity(entityId, !embeddedEntity)
  const entity = embeddedEntity ?? fetchedEntity
  const { enabled: maskOn, deep, text } = useMaskMode()

  const explicitField = event.source_field?.trim() || null
  const fromEventType = event.event_type?.trim() ? pascalToSnakeCase(event.event_type) : ''
  const inferredField = explicitField || (fromEventType || null)

  const snap =
    entity?.snapshot && typeof entity.snapshot === 'object' && !Array.isArray(entity.snapshot)
      ? (entity.snapshot as Record<string, unknown>)
      : null

  const resolved = inferredField && snap ? snapshotPathValue(snap, inferredField) : undefined
  const resolvedFound = resolved !== undefined

  if (!entityId) return null

  return (
    <div className="rounded-md border border-border bg-muted/30 px-3 py-2 space-y-2 text-xs">
      {isLoading && (
        <p className="text-muted-foreground animate-pulse">Loading details...</p>
      )}
      {error && (
        <p className="text-destructive">Could not load record: {(error as Error).message}</p>
      )}

      {entity && (
        <>
          <p className="text-muted-foreground">
            <span className="font-medium text-foreground/80">
              {maskOn ? text(entityDisplayName(entity)) : entityDisplayName(entity)}
            </span>
            <Link to={entityHref(entity)} className="ml-2 text-primary underline-offset-4 hover:underline">
              View record
            </Link>
          </p>
          {inferredField && resolvedFound && (
            <pre className="text-xs overflow-x-auto whitespace-pre-wrap text-foreground/90 bg-background/80 rounded border border-border p-2">
              {JSON.stringify(maskOn ? deep(resolved) : resolved, null, 2)}
            </pre>
          )}
          {inferredField && !resolvedFound && snap && (
            <p className="text-amber-800 dark:text-amber-400">
              This field is not currently available on the record.
            </p>
          )}
        </>
      )}
    </div>
  )
}
