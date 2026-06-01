import { useMemo, useCallback } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import type { Observation } from '@/types/neotoma'
import { formatEur, formatUsd, formatDateShort } from '@/lib/formatters'
import { roundUsdFromEur } from '@/lib/aggregations'
import { useMaskMode } from '@/context/MaskModeContext'
import { useDisplayUnit } from '@/context/DisplayUnitContext'
import { useFxRate } from '@/context/FxRateContext'

interface Props {
  observations: Observation[]
  valueField?: string
  label?: string
}

export default function BalanceChart({ observations, valueField = 'balance_eur', label = 'Balance' }: Props) {
  const { enabled: maskOn, maskMoney } = useMaskMode()
  const { displayUnit } = useDisplayUnit()
  const { usdPerEur } = useFxRate()

  const data = useMemo(() => {
    const raw = observations
      .map((obs, i) => {
        const payload = obs.data
        const value = payload
          ? ((payload[valueField] ?? payload.balance ?? payload.balance_eur) as number | undefined)
          : undefined
        return {
          date: obs.observed_at || obs.created_at,
          value,
          _i: i,
        }
      })
      .filter(d => d.value != null)
      .sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime())
    if (!maskOn) return raw.map(({ date, value }) => ({ date, value }))
    return raw.map(({ date, value, _i }) => ({
      date,
      value: maskMoney(value as number, `chart:${valueField}:${_i}`),
    }))
  }, [observations, valueField, maskOn, maskMoney])

  if (data.length === 0) {
    return <p className="text-muted-foreground text-sm py-4">No balance history available</p>
  }

  const tickFmt = useCallback(
    (v: number) => (displayUnit === 'usd' ? formatUsd(roundUsdFromEur(v, usdPerEur)) : formatEur(v)),
    [displayUnit, usdPerEur],
  )

  const tooltipFmt = useCallback(
    (eurVal: number) => {
      const usd = roundUsdFromEur(eurVal, usdPerEur)
      if (displayUnit === 'usd') {
        return [`${formatUsd(usd)} · ${formatEur(eurVal, true)} EUR`, label]
      }
      return [`${formatEur(eurVal, true)} · ${formatUsd(usd)} USD`, label]
    },
    [displayUnit, usdPerEur, label],
  )

  return (
    <div className="h-64">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 16, left: 16, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
          <XAxis
            dataKey="date"
            tickFormatter={formatDateShort}
            className="text-xs fill-muted-foreground"
          />
          <YAxis
            tickFormatter={(v) => tickFmt(v)}
            className="text-xs fill-muted-foreground"
            width={80}
          />
          <Tooltip
            formatter={(value: number) => tooltipFmt(value)}
            labelFormatter={(l) => formatDateShort(l as string)}
            contentStyle={{
              backgroundColor: 'hsl(var(--card))',
              border: '1px solid hsl(var(--border))',
              borderRadius: '0.5rem',
              color: 'hsl(var(--card-foreground))',
            }}
          />
          <Line
            type="monotone"
            dataKey="value"
            stroke="hsl(var(--primary))"
            strokeWidth={2}
            dot={data.length < 30}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
