import { useMemo } from 'react'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { resolvedTaxFormName } from '@/lib/taxFormNames'

interface CompletenessField {
  key: string
  label: string
  required?: boolean
}

const ACCOUNT_FIELDS: CompletenessField[] = [
  { key: 'institution', label: 'Institution', required: true },
  { key: 'account_value', label: 'Current value' },
  { key: 'account_value_currency', label: 'Currency' },
  { key: 'account_value_eur', label: 'EUR value' },
  { key: 'account_value_as_of_date', label: 'Value date' },
  { key: 'registry_id', label: 'Registry ID' },
  { key: 'account_type', label: 'Account type' },
  { key: 'modelo_form', label: 'Modelo form' },
]

const FILING_FIELDS: CompletenessField[] = [
  { key: 'form_code', label: 'Form code', required: true },
  { key: 'tax_year', label: 'Tax year', required: true },
  { key: 'status', label: 'Status', required: true },
  { key: 'filing_deadline', label: 'Deadline' },
  { key: 'filing_authority', label: 'Authority' },
  { key: 'form_name', label: 'Form name' },
]

function getFieldsForType(entityType: string): CompletenessField[] {
  switch (entityType) {
    case 'financial_account':
      return ACCOUNT_FIELDS
    case 'tax_filing':
      return FILING_FIELDS
    default:
      return []
  }
}

function hasValue(v: unknown): boolean {
  if (v == null) return false
  if (typeof v === 'string' && v.trim() === '') return false
  if (typeof v === 'number' && isNaN(v)) return false
  return true
}

interface Props {
  entityType: string
  snapshot: Record<string, unknown> | null | undefined
  compact?: boolean
  className?: string
}

export default function DataCompletenessBar({ entityType, snapshot, compact, className }: Props) {
  const fields = getFieldsForType(entityType)

  const { filled, total, missingLabels, percentage } = useMemo(() => {
    if (fields.length === 0 || !snapshot) {
      return { filled: 0, total: fields.length, missingLabels: fields.map(f => f.label), percentage: 0 }
    }
    let filled = 0
    const missing: string[] = []
    for (const f of fields) {
      const raw = f.key === 'form_name' ? resolvedTaxFormName(snapshot) : snapshot[f.key]
      if (hasValue(raw)) {
        filled++
      } else {
        missing.push(f.label)
      }
    }
    return {
      filled,
      total: fields.length,
      missingLabels: missing,
      percentage: fields.length > 0 ? Math.round((filled / fields.length) * 100) : 0,
    }
  }, [fields, snapshot])

  if (fields.length === 0) return null

  const barColor =
    percentage === 100
      ? 'bg-emerald-500'
      : percentage >= 60
        ? 'bg-amber-500'
        : 'bg-red-400'

  const badgeVariant = percentage === 100 ? 'default' : 'secondary'

  if (compact) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge variant={badgeVariant} className={cn('font-normal text-xs cursor-help', className)}>
            {percentage}%
          </Badge>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-xs">
          <p className="font-medium mb-1">{filled}/{total} fields populated</p>
          {missingLabels.length > 0 && (
            <p className="text-xs text-muted-foreground">
              Missing: {missingLabels.join(', ')}
            </p>
          )}
        </TooltipContent>
      </Tooltip>
    )
  }

  return (
    <div className={cn('space-y-1.5', className)}>
      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground">
          Data completeness
        </span>
        <span className={cn('font-medium', percentage === 100 ? 'text-emerald-600' : 'text-muted-foreground')}>
          {filled}/{total} fields ({percentage}%)
        </span>
      </div>
      <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden">
        <div
          className={cn('h-full rounded-full transition-all duration-300', barColor)}
          style={{ width: `${percentage}%` }}
        />
      </div>
      {missingLabels.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Missing: {missingLabels.join(', ')}
        </p>
      )}
    </div>
  )
}
