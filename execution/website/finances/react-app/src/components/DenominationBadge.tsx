import type { Entity } from '@/types/neotoma'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { denominationBadgeClass, getAccountDenomination } from '@/lib/accountDenomination'

export function DenominationBadge({ entity }: { entity: Entity }) {
  if (entity.entity_type !== 'financial_account') {
    return <span className="text-muted-foreground text-sm">—</span>
  }
  const d = getAccountDenomination(entity)
  return (
    <Badge variant="outline" className={cn('font-normal', denominationBadgeClass(d.kind))} title={d.detail}>
      {d.label}
    </Badge>
  )
}
