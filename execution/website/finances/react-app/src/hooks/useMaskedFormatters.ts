import { useMaskMode } from '@/context/MaskModeContext'
import { formatEur, formatUsd, formatNumber, formatPercent } from '@/lib/formatters'

/** Formatters that apply mask when privacy mode is on (money / counts / percents use type-appropriate masking). */
export function useMaskedFormatters() {
  const { enabled, maskMoney, maskCount, maskPercent } = useMaskMode()

  return {
    enabled,
    eur: (value: number | null | undefined, detailed = false) =>
      formatEur(value == null ? undefined : maskMoney(value, 'fmt-eur'), detailed),
    usd: (value: number | null | undefined) =>
      formatUsd(value == null ? undefined : maskMoney(value, 'fmt-usd')),
    num: (value: number | null | undefined) =>
      formatNumber(value == null ? undefined : maskCount(value, 'fmt-num')),
    pct: (value: number | null | undefined) =>
      formatPercent(value == null ? undefined : maskPercent(value, 'fmt-pct')),
    /** Exposed for call sites that need currency-style masking without formatting. */
    maskMoney,
    maskCount,
    maskPercent,
  }
}
