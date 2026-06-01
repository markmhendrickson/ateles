import { useMemo } from 'react'
import { useMaskMode } from '@/context/MaskModeContext'
import { formatEur, formatUsd } from '@/lib/formatters'
import { roundEurFromUsd, roundUsdFromEur } from '@/lib/alignMaskedFx'

/**
 * When mask mode is on, one leg is masked and the other derived at `usdPerEur` so pairs match.
 * When off, returns raw formatted values.
 */
export function useAlignedMaskedFx(eur: number, usd: number, pairKey: string, usdPerEur: number) {
  const { enabled, maskMoney } = useMaskMode()

  return useMemo(() => {
    if (!enabled) {
      return {
        eurLabel: formatEur(eur),
        usdLabel: formatUsd(usd),
        eurValue: eur,
        usdValue: usd,
      }
    }
    const eurZ = Math.abs(eur) < 1e-9
    const usdZ = Math.abs(usd) < 1e-9

    let eurMasked: number
    let usdMasked: number
    if (!eurZ && usdZ) {
      eurMasked = maskMoney(eur, `${pairKey}-eur`)
      usdMasked = roundUsdFromEur(eurMasked, usdPerEur)
    } else if (eurZ && !usdZ) {
      usdMasked = maskMoney(usd, `${pairKey}-usd`)
      eurMasked = roundEurFromUsd(usdMasked, usdPerEur)
    } else {
      eurMasked = maskMoney(eur, `${pairKey}-eur`)
      usdMasked = roundUsdFromEur(eurMasked, usdPerEur)
    }

    return {
      eurLabel: formatEur(eurMasked),
      usdLabel: formatUsd(usdMasked),
      eurValue: eurMasked,
      usdValue: usdMasked,
    }
  }, [enabled, eur, usd, pairKey, maskMoney, usdPerEur])
}
