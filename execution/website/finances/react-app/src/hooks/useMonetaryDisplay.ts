import { useDisplayUnit } from '@/context/DisplayUnitContext'
import { useFxRate } from '@/context/FxRateContext'
import { useMaskMode } from '@/context/MaskModeContext'
import { formatEur, formatUsd } from '@/lib/formatters'
import { getEntityFxAsOfDate } from '@/lib/entityFxDate'
import {
  applyFinancialAccountLiabilityDisplaySign,
  comparableEurFromStorageForDisplay,
  getEntityRawStorageLegs,
  isDisplayBasisEurSourced,
  isDisplayBasisUsdSourced,
  roundUsdFromEur,
} from '@/lib/aggregations'
import type { Entity } from '@/types/neotoma'

function buildPrimaryConversionTooltip(params: {
  displayUnit: 'usd' | 'eur'
  entity: Entity | null | undefined
  basisEurUnmasked: number
  usdPerEur: number
  eurDisplayCore: number
  usdDisplayCore: number
  fxDateLabel: string
}): string | null {
  const { displayUnit, entity, basisEurUnmasked, usdPerEur, eurDisplayCore, usdDisplayCore, fxDateLabel } = params
  const rate = usdPerEur.toFixed(4)
  const header = `FX as of ${fxDateLabel} (ECB via Frankfurter)`
  const rateLine = `1 EUR = ${rate} USD`

  if (displayUnit === 'usd') {
    if (entity && isDisplayBasisUsdSourced(entity)) return null
    const productUsd = basisEurUnmasked * usdPerEur
    return [
      header,
      rateLine,
      `${formatEur(basisEurUnmasked, true)} × ${rate} USD/EUR = ${productUsd.toFixed(2)} USD`,
      `Display: ${formatUsd(usdDisplayCore)} (band-rounded)`,
    ].join('\n')
  }

  if (displayUnit === 'eur' && entity && isDisplayBasisUsdSourced(entity)) {
    const raw = getEntityRawStorageLegs(entity)
    const usdStored = raw.usd
    if (!Number.isFinite(usdStored) || Math.abs(usdStored) < 1e-12) return null
    const quotientEur = usdStored / usdPerEur
    return [
      header,
      rateLine,
      `${formatUsd(usdStored)} ÷ ${rate} USD/EUR = ${quotientEur.toFixed(2)} EUR`,
      `Display: ${formatEur(eurDisplayCore, true)} (band-rounded)`,
    ].join('\n')
  }

  return null
}

export function useMonetaryDisplay({
  canonicalEur,
  usdPerEur,
  entity,
  pairKey,
  detailedEur = false,
}: {
  canonicalEur: number
  usdPerEur: number
  entity?: Entity | null
  pairKey: string
  detailedEur?: boolean
}): { primary: string; secondary: string; primaryConversionTooltip: string | null } {
  const { displayUnit } = useDisplayUnit()
  const { enabled: maskOn, maskMoney } = useMaskMode()
  const { rateDate } = useFxRate()

  const basisEur = comparableEurFromStorageForDisplay(entity, canonicalEur, usdPerEur)
  const basisEurDisplay = applyFinancialAccountLiabilityDisplaySign(basisEur, entity)
  const isZero = !Number.isFinite(basisEurDisplay) || Math.abs(basisEurDisplay) < 1e-12

  const raw = entity ? getEntityRawStorageLegs(entity) : { eur: 0, usd: 0 }
  const hasExplicitZeroSnapshotBalance =
    !!entity?.snapshot &&
    (() => {
      const bv = entity.snapshot?.balance_value
      if (typeof bv === 'number' && Number.isFinite(bv) && Math.abs(bv) < 1e-12) return true
      return ['account_value', 'balance', 'balance_eur', 'balance_usd'].some((k) => {
        const v = entity.snapshot?.[k]
        return typeof v === 'number' && Number.isFinite(v) && Math.abs(v) < 1e-12
      })
    })()

  if (isZero && !hasExplicitZeroSnapshotBalance) {
    return { primary: '—', secondary: '', primaryConversionTooltip: null }
  }
  if (isZero && hasExplicitZeroSnapshotBalance) {
    const zero = maskOn ? maskMoney(0, `${pairKey}-zero`) : 0
    return {
      primary: displayUnit === 'usd' ? formatUsd(zero) : formatEur(zero, detailedEur),
      secondary: '',
      primaryConversionTooltip: null,
    }
  }
  const usdNative = Boolean(entity && isDisplayBasisUsdSourced(entity))
  const eurNative = Boolean(entity && isDisplayBasisEurSourced(entity))

  /** Stored leg matches display currency: show that leg directly (no EUR↔USD band round-trip). */
  if (usdNative && displayUnit === 'usd' && Math.abs(raw.usd) > 1e-12) {
    const signed = applyFinancialAccountLiabilityDisplaySign(raw.usd, entity)
    const leg = maskOn ? maskMoney(signed, `${pairKey}-ru`) : signed
    return {
      primary: formatUsd(leg),
      secondary: '',
      primaryConversionTooltip: null,
    }
  }
  if (eurNative && displayUnit === 'eur' && Math.abs(raw.eur) > 1e-12) {
    const signed = applyFinancialAccountLiabilityDisplaySign(raw.eur, entity)
    const leg = maskOn ? maskMoney(signed, `${pairKey}-re`) : signed
    return {
      primary: formatEur(leg, detailedEur),
      secondary: '',
      primaryConversionTooltip: null,
    }
  }

  const eurMaskedCore = maskOn ? maskMoney(basisEurDisplay, `${pairKey}-ce`) : basisEurDisplay
  const usdMaskedCore = roundUsdFromEur(eurMaskedCore, usdPerEur)

  const primary =
    displayUnit === 'usd'
      ? formatUsd(usdMaskedCore)
      : formatEur(eurMaskedCore, detailedEur)
  const rawE = raw.eur !== 0 ? (maskOn ? maskMoney(raw.eur, `${pairKey}-re`) : raw.eur) : 0
  const rawU = raw.usd !== 0 ? (maskOn ? maskMoney(raw.usd, `${pairKey}-ru`) : raw.usd) : 0

  let storagePart = ''
  if (rawE !== 0 && rawU !== 0) {
    storagePart = `${formatEur(rawE, detailedEur)} EUR · ${formatUsd(rawU)} USD`
  } else if (rawE !== 0) {
    storagePart = `${formatEur(rawE, detailedEur)} EUR`
  } else if (rawU !== 0) {
    storagePart = `${formatUsd(rawU)} USD`
  }

  /** Secondary is only Neotoma-stored legs (rows / explicit snapshot fields), never a converted cross of canonical. */
  const secondary = storagePart

  const fxIso = entity ? getEntityFxAsOfDate(entity) : null
  const fxDateLabel = fxIso ?? rateDate ?? 'latest rate'

  const primaryConversionTooltip = maskOn
    ? null
    : buildPrimaryConversionTooltip({
        displayUnit,
        entity,
        basisEurUnmasked: basisEurDisplay,
        usdPerEur,
        eurDisplayCore: eurMaskedCore,
        usdDisplayCore: usdMaskedCore,
        fxDateLabel,
      })

  return { primary, secondary, primaryConversionTooltip }
}

/** Pre-formatted aggregate (e.g. net worth) from masked-aligned EUR/USD labels. */
export function useAggregateMonetaryLabels(eurLabel: string, usdLabel: string) {
  const { displayUnit } = useDisplayUnit()
  const primary = displayUnit === 'usd' ? usdLabel : eurLabel
  const secondary = displayUnit === 'usd' ? `${eurLabel} EUR` : `${usdLabel} USD`
  return { primary, secondary }
}
