import { cn } from '@/lib/utils'
import { useAggregateMonetaryLabels, useMonetaryDisplay } from '@/hooks/useMonetaryDisplay'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { Entity } from '@/types/neotoma'

export type MonetaryLayout = 'stack' | 'inline'

function PrimaryWithOptionalTooltip({
  primary,
  primaryClassName,
  primaryConversionTooltip,
}: {
  primary: string
  primaryClassName?: string
  primaryConversionTooltip: string | null
}) {
  const inner = <span className={cn('font-medium', primaryClassName)}>{primary}</span>
  if (!primaryConversionTooltip?.trim()) return inner
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="cursor-help underline decoration-dotted decoration-muted-foreground/60 underline-offset-2">
          {inner}
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="whitespace-pre-line text-xs leading-snug">
        {primaryConversionTooltip}
      </TooltipContent>
    </Tooltip>
  )
}

export function MonetaryStack({
  primary,
  secondary,
  align = 'right',
  layout = 'stack',
  className,
  primaryClassName,
  secondaryClassName,
  primaryConversionTooltip = null,
  showConversion = true,
}: {
  primary: string
  /** Neotoma original / aggregate counterpart; empty hides secondary. */
  secondary: string
  align?: 'left' | 'right'
  /** `inline`: `primary (secondary)` on one line. `stack`: secondary below, no parentheses. */
  layout?: MonetaryLayout
  className?: string
  primaryClassName?: string
  secondaryClassName?: string
  /** Multi-line explanation for ECB cross when primary is converted (hover). */
  primaryConversionTooltip?: string | null
  /** When false, hide EUR/USD breakdown; primary tooltip still shows conversion notes when set. */
  showConversion?: boolean
}) {
  const hasSecondary = showConversion && Boolean(secondary?.trim())

  if (layout === 'inline') {
    return (
      <div className={cn('tabular-nums inline-block min-w-0 max-w-full', align === 'right' && 'text-right', className)}>
        <span className="tabular-nums">
          <PrimaryWithOptionalTooltip
            primary={primary}
            primaryClassName={primaryClassName}
            primaryConversionTooltip={primaryConversionTooltip}
          />
          {hasSecondary ? (
            <span
              className={cn(
                'text-[11px] text-muted-foreground font-normal whitespace-normal',
                secondaryClassName,
              )}
            >
              {' '}
              ({secondary})
            </span>
          ) : null}
        </span>
      </div>
    )
  }

  return (
    <div className={cn('flex flex-col gap-0.5', align === 'right' && 'items-end', className)}>
      <span className="tabular-nums">
        <PrimaryWithOptionalTooltip
          primary={primary}
          primaryClassName={primaryClassName}
          primaryConversionTooltip={primaryConversionTooltip}
        />
      </span>
      {hasSecondary && (
        <span className={cn('tabular-nums text-[11px] text-muted-foreground leading-tight', secondaryClassName)}>
          {secondary}
        </span>
      )}
    </div>
  )
}

export function MonetaryPair({
  canonicalEur,
  usdPerEur,
  entity,
  pairKey,
  align = 'right',
  layout = 'stack',
  detailedEur = false,
  className,
  primaryClassName,
  showConversion = true,
}: {
  canonicalEur: number
  usdPerEur: number
  entity?: Entity | null
  pairKey: string
  align?: 'left' | 'right'
  layout?: MonetaryLayout
  detailedEur?: boolean
  className?: string
  primaryClassName?: string
  showConversion?: boolean
}) {
  const { primary, secondary, primaryConversionTooltip } = useMonetaryDisplay({
    canonicalEur,
    usdPerEur,
    entity,
    pairKey,
    detailedEur,
  })
  return (
    <MonetaryStack
      primary={primary}
      secondary={secondary}
      primaryConversionTooltip={primaryConversionTooltip}
      align={align}
      layout={layout}
      className={className}
      primaryClassName={primaryClassName}
      showConversion={showConversion}
    />
  )
}

/** Portfolio-style line using pre-built EUR/USD strings (e.g. from useAlignedMaskedFx). */
export function AggregateMonetaryPair({
  eurLabel,
  usdLabel,
  align = 'right',
  layout = 'inline',
  className,
  primaryClassName,
  showConversion = true,
}: {
  eurLabel: string
  usdLabel: string
  align?: 'left' | 'right'
  /** Totals in subtitles default to `inline` so the non-display unit is parenthesized. */
  layout?: MonetaryLayout
  className?: string
  primaryClassName?: string
  showConversion?: boolean
}) {
  const { primary, secondary } = useAggregateMonetaryLabels(eurLabel, usdLabel)
  return (
    <MonetaryStack
      primary={primary}
      secondary={secondary}
      align={align}
      layout={layout}
      className={className}
      primaryClassName={primaryClassName}
      showConversion={showConversion}
    />
  )
}
