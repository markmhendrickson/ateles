import type { ElementType, HTMLAttributes } from 'react'
import { useMaskMode } from '@/context/MaskModeContext'
import { humanizePropertyKey } from '@/lib/propertyLabels'
import { cn } from '@/lib/utils'

type Props<T extends ElementType> = {
  /** Literal API / snapshot key (shown in native tooltip) */
  literalKey: string
  as?: T
  className?: string
} & Omit<HTMLAttributes<HTMLElement>, 'title' | 'children'>

/**
 * Renders a human-readable property name; hover shows the literal field key.
 */
export default function PropertyLabel<T extends ElementType = 'span'>({
  literalKey,
  as,
  className,
  ...rest
}: Props<T>) {
  const Comp = (as ?? 'span') as ElementType
  const { enabled: maskOn, text } = useMaskMode()
  const readable = humanizePropertyKey(literalKey)
  const display = maskOn ? text(readable) : readable

  return (
    <Comp className={cn(className)} {...rest} title={literalKey}>
      {display}
    </Comp>
  )
}
