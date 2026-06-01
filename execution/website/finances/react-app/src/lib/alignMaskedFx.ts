import { roundEurFromUsd, roundUsdFromEur } from '@/lib/aggregations'
import { formatEur, formatUsd } from '@/lib/formatters'

export { roundEurFromUsd, roundUsdFromEur } from '@/lib/aggregations'

/** Pair labels for any EUR/USD display (table rows, headers). */
export function alignedMaskedFxLabels(
  eur: number,
  usd: number,
  maskEnabled: boolean,
  maskMoneyFn: (n: number, key?: string) => number,
  pairKey: string,
  usdPerEur: number,
): { eurLabel: string; usdLabel: string } {
  if (!maskEnabled) {
    return { eurLabel: formatEur(eur), usdLabel: formatUsd(usd) }
  }
  const eurZ = Math.abs(eur) < 1e-9
  const usdZ = Math.abs(usd) < 1e-9
  if (!eurZ && usdZ) {
    const eurMasked = maskMoneyFn(eur, `${pairKey}-eur`)
    const usdMasked = roundUsdFromEur(eurMasked, usdPerEur)
    return { eurLabel: formatEur(eurMasked), usdLabel: formatUsd(usdMasked) }
  }
  if (eurZ && !usdZ) {
    const usdMasked = maskMoneyFn(usd, `${pairKey}-usd`)
    const eurMasked = roundEurFromUsd(usdMasked, usdPerEur)
    return { eurLabel: formatEur(eurMasked), usdLabel: formatUsd(usdMasked) }
  }
  const eurMasked = maskMoneyFn(eur, `${pairKey}-eur`)
  const usdMasked = roundUsdFromEur(eurMasked, usdPerEur)
  return { eurLabel: formatEur(eurMasked), usdLabel: formatUsd(usdMasked) }
}
