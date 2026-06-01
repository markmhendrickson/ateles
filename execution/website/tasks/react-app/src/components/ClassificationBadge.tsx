import { Badge } from '@shared/components/ui/badge'
import { cn } from '@shared/lib/utils'

const CLASSIFICATION_CLASSES: Record<string, string> = {
  urgent: 'bg-red-100 text-red-900 dark:bg-red-900/35 dark:text-red-200',
  nonurgent: 'bg-sky-100 text-sky-900 dark:bg-sky-900/35 dark:text-sky-200',
  scheduled: 'bg-violet-100 text-violet-900 dark:bg-violet-900/35 dark:text-violet-200',
}

const CLASSIFICATION_LABELS: Record<string, string> = {
  urgent: 'Urgent',
  nonurgent: 'Non-urgent',
  scheduled: 'Scheduled',
}

const CLASSIFICATION_TOOLTIPS: Record<string, string> = {
  urgent: 'Blocking, time-sensitive, active breakage, or committed delivery',
  nonurgent: 'Important but nothing forces it to happen now',
  scheduled: 'Has a due date',
}

export function normalizeClassification(raw: string | null | undefined): string {
  if (!raw) return ''
  return raw.trim().toLowerCase()
}

export function classificationLabel(raw: string | null | undefined): string {
  const key = normalizeClassification(raw)
  return CLASSIFICATION_LABELS[key] ?? (key ? key.charAt(0).toUpperCase() + key.slice(1) : 'Unclassified')
}

export function classificationSortOrder(raw: string | null | undefined): number {
  const key = normalizeClassification(raw)
  switch (key) {
    case 'urgent': return 0
    case 'scheduled': return 1
    case 'nonurgent': return 2
    default: return 3
  }
}

interface Props {
  value: string | null | undefined
  className?: string
}

export default function ClassificationBadge({ value, className }: Props) {
  const key = normalizeClassification(value)
  if (!key) {
    return <span className="text-muted-foreground text-sm">&mdash;</span>
  }
  const label = classificationLabel(key)
  const colorClass = CLASSIFICATION_CLASSES[key] ?? 'bg-muted text-muted-foreground'
  return (
    <Badge
      variant="outline"
      className={cn('font-medium normal-case border-0', colorClass, className)}
      title={CLASSIFICATION_TOOLTIPS[key] ?? key}
    >
      {label}
    </Badge>
  )
}
