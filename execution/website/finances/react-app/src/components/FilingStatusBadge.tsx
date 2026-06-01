import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'

interface Props {
  tags: string[] | undefined
}

const TAG_COLORS: Record<string, string> = {
  '720': 'border-0 bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300',
  '721': 'border-0 bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
  equity: 'border-0 bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  domestic_es: 'border-0 bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  domestic_us: 'border-0 bg-indigo-100 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300',
}

export default function FilingStatusBadge({ tags }: Props) {
  if (!tags || tags.length === 0) {
    return <span className="text-muted-foreground text-xs">none</span>
  }

  return (
    <div className="flex flex-wrap gap-1">
      {tags.map(tag => (
        <Badge
          key={tag}
          variant="outline"
          className={cn('rounded-full px-2 py-0.5 font-medium normal-case', TAG_COLORS[tag] ?? 'bg-muted text-muted-foreground border-transparent')}
        >
          {tag}
        </Badge>
      ))}
    </div>
  )
}
