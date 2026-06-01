import { Badge } from '@shared/components/ui/badge'
import { cn } from '@shared/lib/utils'

const PRIORITY_CLASSES: Record<string, string> = {
  critical: 'bg-red-100 text-red-900 dark:bg-red-900/35 dark:text-red-200',
  high: 'bg-orange-100 text-orange-900 dark:bg-orange-900/35 dark:text-orange-200',
  medium: 'bg-yellow-100 text-yellow-900 dark:bg-yellow-900/35 dark:text-yellow-200',
  low: 'bg-sky-100 text-sky-900 dark:bg-sky-900/35 dark:text-sky-200',
}

const PRIORITY_LABELS: Record<string, string> = {
  critical: 'Critical',
  high: 'High',
  medium: 'Medium',
  low: 'Low',
}

export function normalizePriority(raw: string | null | undefined): string {
  if (!raw) return ''
  return raw.trim().toLowerCase()
}

export function priorityLabel(raw: string | null | undefined): string {
  const key = normalizePriority(raw)
  return PRIORITY_LABELS[key] ?? (key ? key.charAt(0).toUpperCase() + key.slice(1) : 'Unprioritized')
}

export function prioritySortOrder(raw: string | null | undefined): number {
  const key = normalizePriority(raw)
  switch (key) {
    case 'critical': return 0
    case 'high': return 1
    case 'medium': return 2
    case 'low': return 3
    default: return 4
  }
}

interface Props {
  value: string | null | undefined
  className?: string
}

export default function PriorityBadge({ value, className }: Props) {
  const key = normalizePriority(value)
  if (!key) {
    return <span className="text-muted-foreground text-sm">—</span>
  }
  const label = priorityLabel(key)
  const colorClass = PRIORITY_CLASSES[key] ?? 'bg-muted text-muted-foreground'
  return (
    <Badge
      variant="outline"
      className={cn('font-medium normal-case border-0', colorClass, className)}
      title={key}
    >
      {label}
    </Badge>
  )
}
