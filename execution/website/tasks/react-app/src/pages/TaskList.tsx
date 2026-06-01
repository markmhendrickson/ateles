import { useState, useMemo, useCallback } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useEntities } from '@shared/hooks/useEntities'
import { snapshotField, formatDate, formatRelativeTime } from '@shared/lib/formatters'
import { cn } from '@shared/lib/utils'
import { Input } from '@shared/components/ui/input'
import WorkflowStatusBadge from '@shared/components/WorkflowStatusBadge'
import ClassificationBadge, { normalizeClassification, classificationLabel } from '@/components/ClassificationBadge'
import { taskCategoryKey, categoryFromSearchParam, categoryDisplayLabel } from '@/lib/taskCategory'
import type { Entity } from '@shared/types/neotoma'

type StatusFilter = 'open' | 'completed' | 'all'

const CLASSIFICATION_GROUP_ORDER = ['urgent', 'scheduled', 'nonurgent', '']

const STATUS_SORT_ORDER: Record<string, number> = {
  open: 0,
  in_progress: 1,
  pending: 2,
  blocked: 3,
  completed: 10,
  cancelled: 11,
  canceled: 11,
  closed: 12,
}

function statusSortValue(raw: string | null | undefined): number {
  if (!raw) return 5
  const key = raw.trim().toLowerCase().replace(/[\s-]+/g, '_')
  return STATUS_SORT_ORDER[key] ?? 5
}

function isCompletedStatus(raw: string | null | undefined): boolean {
  if (!raw) return false
  const key = raw.trim().toLowerCase()
  return key === 'completed' || key === 'complete' || key === 'cancelled' || key === 'canceled' || key === 'closed' || key === 'archived'
}

function taskTitle(entity: Entity): string {
  const snap = entity.snapshot
  if (snap) {
    const t = snap.title ?? snap.name
    if (typeof t === 'string' && t.trim()) return t.trim()
  }
  if (entity.canonical_name?.trim()) return entity.canonical_name.trim()
  return `Task ${entity.entity_id.slice(-8)}`
}

export default function TaskList() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('open')
  const [searchText, setSearchText] = useState('')
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const categoryFilter = categoryFromSearchParam(searchParams.get('category'))

  const tasks = useEntities(
    { entity_type: 'task', include_snapshots: true, limit: 2000 },
    true,
  )

  const filteredTasks = useMemo(() => {
    let list = tasks.data?.entities ?? []

    if (statusFilter === 'open') {
      list = list.filter(e => !isCompletedStatus(snapshotField<string>(e.snapshot, 'status')))
    } else if (statusFilter === 'completed') {
      list = list.filter(e => isCompletedStatus(snapshotField<string>(e.snapshot, 'status')))
    }

    const q = searchText.trim().toLowerCase()
    if (q) {
      list = list.filter(e => {
        const title = taskTitle(e).toLowerCase()
        const notes = String(snapshotField(e.snapshot, 'notes') ?? '').toLowerCase()
        const project = String(snapshotField(e.snapshot, 'project_names') ?? snapshotField(e.snapshot, 'domain') ?? '').toLowerCase()
        const cat = String(snapshotField(e.snapshot, 'category') ?? '').toLowerCase()
        return title.includes(q) || notes.includes(q) || project.includes(q) || cat.includes(q)
      })
    }

    if (categoryFilter !== null) {
      list = list.filter(e => taskCategoryKey(e) === categoryFilter)
    }

    return list
  }, [tasks.data?.entities, statusFilter, searchText, categoryFilter])

  const grouped = useMemo(() => {
    const groups = new Map<string, Entity[]>()
    for (const key of CLASSIFICATION_GROUP_ORDER) {
      groups.set(key, [])
    }

    for (const entity of filteredTasks) {
      const c = normalizeClassification(snapshotField<string>(entity.snapshot, 'classification'))
      const key = CLASSIFICATION_GROUP_ORDER.includes(c) ? c : ''
      const arr = groups.get(key) ?? []
      arr.push(entity)
      groups.set(key, arr)
    }

    for (const [, arr] of groups) {
      arr.sort((a, b) => {
        const sa = statusSortValue(snapshotField<string>(a.snapshot, 'status'))
        const sb = statusSortValue(snapshotField<string>(b.snapshot, 'status'))
        if (sa !== sb) return sa - sb
        const da = snapshotField<string>(a.snapshot, 'due_date') ?? ''
        const db = snapshotField<string>(b.snapshot, 'due_date') ?? ''
        if (da && db) return da.localeCompare(db)
        if (da) return -1
        if (db) return 1
        const ua = a.updated_at ?? a.last_observation_at ?? ''
        const ub = b.updated_at ?? b.last_observation_at ?? ''
        return ub.localeCompare(ua)
      })
    }

    return groups
  }, [filteredTasks])

  const totalShown = filteredTasks.length
  const totalAll = tasks.data?.total ?? 0

  const onRowClick = useCallback((entity: Entity) => {
    navigate(`/tasks/${entity.entity_id}`)
  }, [navigate])

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          {categoryFilter === null ? 'Tasks' : categoryDisplayLabel(categoryFilter)}
        </h1>
        <p className="text-muted-foreground text-sm mt-1">
          {tasks.isLoading
            ? 'Loading...'
            : `${totalShown} task${totalShown === 1 ? '' : 's'} shown${totalShown !== totalAll ? ` of ${totalAll} total` : ''}`}
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="flex rounded-md border border-border overflow-hidden">
          {(['open', 'completed', 'all'] as StatusFilter[]).map(f => (
            <button
              key={f}
              type="button"
              className={cn(
                'px-3 py-1.5 text-sm transition-colors',
                statusFilter === f
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-background text-muted-foreground hover:bg-muted',
                f !== 'open' && 'border-l border-border',
              )}
              onClick={() => setStatusFilter(f)}
            >
              {f === 'open' ? 'Open' : f === 'completed' ? 'Completed' : 'All'}
            </button>
          ))}
        </div>
        <Input
          type="search"
          placeholder="Search tasks..."
          value={searchText}
          onChange={e => setSearchText(e.target.value)}
          className="h-9 max-w-xs text-sm"
        />
      </div>

      {tasks.isLoading && (
        <p className="text-sm text-muted-foreground animate-pulse">Loading tasks...</p>
      )}
      {tasks.error && (
        <p className="text-sm text-destructive">Error: {(tasks.error as Error).message}</p>
      )}

      {tasks.data && filteredTasks.length === 0 && (
        <p className="text-sm text-muted-foreground py-8 text-center">No tasks match the current filters.</p>
      )}

      {CLASSIFICATION_GROUP_ORDER.map(key => {
        const items = grouped.get(key) ?? []
        if (items.length === 0) return null
        return (
          <section key={key || '__unclassified__'} className="space-y-2">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold text-foreground/80 uppercase tracking-wider">
                {classificationLabel(key || null)}
              </h2>
              <span className="text-xs text-muted-foreground">({items.length})</span>
            </div>
            <div className="rounded-lg border border-border overflow-hidden divide-y divide-border">
              {items.map(entity => (
                <TaskRow key={entity.entity_id} entity={entity} onClick={onRowClick} />
              ))}
            </div>
          </section>
        )
      })}
    </div>
  )
}

function TaskRow({ entity, onClick }: { entity: Entity; onClick: (e: Entity) => void }) {
  const title = taskTitle(entity)
  const status = snapshotField<string>(entity.snapshot, 'status')
  const classification = snapshotField<string>(entity.snapshot, 'classification')
  const dueDate = snapshotField<string>(entity.snapshot, 'due_date')
  const notes = snapshotField<string>(entity.snapshot, 'notes')
  const category = snapshotField<string>(entity.snapshot, 'category')
  const project = snapshotField<string>(entity.snapshot, 'project_names') ?? snapshotField<string>(entity.snapshot, 'domain')
  const updated = entity.updated_at ?? entity.last_observation_at

  return (
    <div
      className="flex flex-col gap-1.5 p-3 hover:bg-muted/30 cursor-pointer transition-colors sm:flex-row sm:items-start sm:gap-4"
      onClick={() => onClick(entity)}
      role="link"
      tabIndex={0}
      onKeyDown={e => e.key === 'Enter' && onClick(entity)}
    >
      <div className="flex-1 min-w-0 space-y-1">
        <div className="flex items-start gap-2">
          <span className="text-sm font-medium leading-snug">{title}</span>
        </div>
        {notes && (
          <p className="text-xs text-muted-foreground line-clamp-1">{notes}</p>
        )}
        {(category || project) && (
          <p className="text-xs text-muted-foreground">{category || project}</p>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2 shrink-0">
        <WorkflowStatusBadge value={status} />
        <ClassificationBadge value={classification} />
        {dueDate && (
          <span className="text-xs text-muted-foreground whitespace-nowrap" title={dueDate}>
            Due {formatDate(dueDate)}
          </span>
        )}
        {!dueDate && updated && (
          <span className="text-xs text-muted-foreground whitespace-nowrap" title={updated}>
            {formatRelativeTime(updated)}
          </span>
        )}
      </div>
    </div>
  )
}
