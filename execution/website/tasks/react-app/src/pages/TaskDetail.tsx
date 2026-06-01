import { useParams, Link } from 'react-router-dom'
import { useEntity, useEntityRelationships } from '@shared/hooks/useEntity'
import { useObservations } from '@shared/hooks/useObservations'
import { snapshotField, formatDate } from '@shared/lib/formatters'
import { humanizePropertyKey } from '@shared/lib/propertyLabels'
import { entityDisplayName, humanizeRelationshipType, entityTypeLabel } from '@shared/lib/humanize'
import { Card, CardContent, CardHeader } from '@shared/components/ui/card'
import WorkflowStatusBadge from '@shared/components/WorkflowStatusBadge'
import ClassificationBadge from '@/components/ClassificationBadge'
import ObservationTimeline from '@/components/ObservationTimeline'
import type { Entity } from '@shared/types/neotoma'
import { ArrowLeft } from 'lucide-react'

const PROMOTED_KEYS = ['title', 'status', 'classification', 'due_date', 'completed_date', 'category', 'notes', 'domain', 'project_names', 'assignee_name']
const HIDDEN_KEYS = new Set(['entity_id', 'entity_type', 'rows'])

function isDateLike(key: string, value: unknown): boolean {
  if (typeof value !== 'string') return false
  if (key.endsWith('_at') || key.endsWith('_date') || key === 'created_at' || key === 'updated_at') return true
  return false
}

function renderValue(key: string, value: unknown): React.ReactNode {
  if (value == null) return <span className="text-muted-foreground">—</span>
  if (key === 'status') return <WorkflowStatusBadge value={value} />
  if (key === 'classification') return <ClassificationBadge value={String(value)} />
  if (isDateLike(key, value)) return <span>{formatDate(String(value))}</span>
  if (typeof value === 'boolean') return <span>{value ? 'Yes' : 'No'}</span>
  if (typeof value === 'object') {
    return (
      <pre className="text-xs overflow-x-auto whitespace-pre-wrap text-foreground/80">
        {JSON.stringify(value, null, 2)}
      </pre>
    )
  }
  return <span className="break-words">{String(value)}</span>
}

export default function TaskDetail() {
  const { id } = useParams<{ id: string }>()
  const entity = useEntity(id)
  const observations = useObservations(id)
  const relationships = useEntityRelationships(id, { expandEntities: true })

  if (entity.isLoading) {
    return <p className="text-sm text-muted-foreground animate-pulse py-8">Loading task...</p>
  }
  if (entity.error) {
    return <p className="text-sm text-destructive py-8">Error: {(entity.error as Error).message}</p>
  }
  if (!entity.data) {
    return <p className="text-sm text-muted-foreground py-8">Task not found.</p>
  }

  const task = entity.data
  const snap = task.snapshot ?? {}
  const title = snapshotField<string>(snap, 'title') ?? entityDisplayName(task)

  const promotedEntries = PROMOTED_KEYS
    .filter(k => k in snap && snap[k] != null)
    .map(k => [k, snap[k]] as [string, unknown])

  const remainingEntries = Object.entries(snap)
    .filter(([k]) => !PROMOTED_KEYS.includes(k) && !HIDDEN_KEYS.has(k))
    .sort(([a], [b]) => a.localeCompare(b))

  const allEntries = [...promotedEntries, ...remainingEntries]

  return (
    <div className="space-y-6">
      <div>
        <Link to="/" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors mb-3">
          <ArrowLeft size={14} />
          Back to tasks
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        <div className="flex flex-wrap items-center gap-2 mt-2">
          <WorkflowStatusBadge value={snapshotField(snap, 'status')} />
          <ClassificationBadge value={snapshotField<string>(snap, 'classification')} />
          {task.entity_id && (
            <span className="text-xs text-muted-foreground font-mono" title="Entity ID">
              {task.entity_id}
            </span>
          )}
        </div>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <h2 className="text-lg font-medium">Properties</h2>
        </CardHeader>
        <CardContent>
          {allEntries.length === 0 ? (
            <p className="text-sm text-muted-foreground">No properties recorded.</p>
          ) : (
            <dl className="grid grid-cols-1 sm:grid-cols-[minmax(140px,auto)_1fr] gap-x-4 gap-y-3">
              {allEntries.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="text-sm font-medium text-muted-foreground" title={key}>
                    {humanizePropertyKey(key)}
                  </dt>
                  <dd className="text-sm">{renderValue(key, value)}</dd>
                </div>
              ))}
            </dl>
          )}
        </CardContent>
      </Card>

      {relationships.data && relationships.data.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <h2 className="text-lg font-medium">
              Relationships ({relationships.data.length})
            </h2>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {relationships.data.map(rel => {
                const isSource = rel.source_entity_id === id
                const related = isSource ? rel.target_entity : rel.source_entity
                const direction = isSource ? '' : '(incoming) '
                return (
                  <div key={rel.relationship_key} className="flex items-center gap-2 text-sm">
                    <span className="text-muted-foreground">
                      {direction}{humanizeRelationshipType(rel.relationship_type)}
                    </span>
                    {related ? (
                      <Link
                        to={`/tasks/${related.entity_id}`}
                        className="text-primary hover:underline underline-offset-2"
                      >
                        {entityDisplayName(related)}
                      </Link>
                    ) : (
                      <span className="font-mono text-xs text-muted-foreground">
                        {isSource ? rel.target_entity_id : rel.source_entity_id}
                      </span>
                    )}
                    {related && (
                      <span className="text-xs text-muted-foreground">
                        ({entityTypeLabel(related.entity_type)})
                      </span>
                    )}
                  </div>
                )
              })}
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-2">
          <h2 className="text-lg font-medium">Raw snapshot</h2>
        </CardHeader>
        <CardContent>
          <pre className="text-xs overflow-x-auto whitespace-pre-wrap text-foreground/80 max-h-96 overflow-y-auto">
            {JSON.stringify(snap, null, 2)}
          </pre>
        </CardContent>
      </Card>

      {task.provenance && Object.keys(task.provenance).length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <h2 className="text-lg font-medium">Provenance</h2>
          </CardHeader>
          <CardContent>
            <pre className="text-xs overflow-x-auto whitespace-pre-wrap text-foreground/80 max-h-96 overflow-y-auto">
              {JSON.stringify(task.provenance, null, 2)}
            </pre>
          </CardContent>
        </Card>
      )}

      {observations.isLoading && (
        <p className="text-sm text-muted-foreground animate-pulse">Loading observations...</p>
      )}
      {observations.error && (
        <p className="text-sm text-destructive">
          Error loading observations: {(observations.error as Error).message}
        </p>
      )}
      {observations.data && observations.data.length > 0 && (
        <div>
          <h2 className="text-lg font-medium mb-3">
            Observations ({observations.data.length})
          </h2>
          <ObservationTimeline observations={observations.data} />
        </div>
      )}
    </div>
  )
}
