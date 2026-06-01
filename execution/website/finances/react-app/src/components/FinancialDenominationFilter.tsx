import type { AccountDenominationKind } from '@/lib/accountDenomination'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

export type FinancialDenominationFilterValue = AccountDenominationKind | 'all'

interface Props {
  value: FinancialDenominationFilterValue
  onChange: (v: FinancialDenominationFilterValue) => void
  /** Shown next to control */
  label?: string
  className?: string
  triggerClassName?: string
  'aria-label'?: string
}

export default function FinancialDenominationFilter({
  value,
  onChange,
  label = 'Denomination',
  className,
  triggerClassName = 'h-9 w-[200px]',
  'aria-label': ariaLabel = 'Filter by denomination',
}: Props) {
  return (
    <div className={className ?? 'flex items-center gap-2 min-w-0'}>
      <span className="text-xs text-muted-foreground shrink-0 whitespace-nowrap">{label}</span>
      <Select value={value} onValueChange={(v) => onChange(v as FinancialDenominationFilterValue)}>
        <SelectTrigger className={triggerClassName} aria-label={ariaLabel}>
          <SelectValue placeholder="All kinds" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All kinds</SelectItem>
          <SelectItem value="crypto">Crypto</SelectItem>
          <SelectItem value="fiat_cash">Fiat (cash / bank)</SelectItem>
          <SelectItem value="investments">Securities</SelectItem>
          <SelectItem value="mixed">Mixed</SelectItem>
          <SelectItem value="other">Other</SelectItem>
        </SelectContent>
      </Select>
    </div>
  )
}
