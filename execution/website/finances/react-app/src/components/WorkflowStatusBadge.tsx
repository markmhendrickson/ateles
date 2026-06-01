import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { humanizeWorkflowStatus, workflowStatusBadgeClassName } from '@/lib/humanize'

interface Props {
  value: unknown
  maskOn?: boolean
  text?: (s: string) => string
  className?: string
}

export default function WorkflowStatusBadge({ value, maskOn, text, className }: Props) {
  const raw = value == null || value === '' ? null : String(value).trim()
  if (!raw) {
    return <span className="text-muted-foreground text-sm">—</span>
  }
  const label = humanizeWorkflowStatus(raw)
  const shown = maskOn && text ? text(label) : label
  return (
    <Badge
      variant="outline"
      className={cn('font-medium normal-case border-0', workflowStatusBadgeClassName(raw), className)}
      title={raw}
    >
      {shown}
    </Badge>
  )
}
