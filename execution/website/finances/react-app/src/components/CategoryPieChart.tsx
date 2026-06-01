import { useMemo, useCallback } from 'react'
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer, Legend } from 'recharts'
import { formatEur, formatUsd } from '@/lib/formatters'
import { roundUsdFromEur } from '@/lib/aggregations'
import { useMaskMode } from '@/context/MaskModeContext'
import { useDisplayUnit } from '@/context/DisplayUnitContext'
import { useFxRate } from '@/context/FxRateContext'

interface PieDataItem {
  name: string
  value: number
}

interface Props {
  data: PieDataItem[]
  label?: string
}

const COLORS = [
  'hsl(220, 90%, 56%)',
  'hsl(160, 60%, 45%)',
  'hsl(30, 90%, 55%)',
  'hsl(280, 60%, 55%)',
  'hsl(350, 70%, 55%)',
  'hsl(190, 70%, 50%)',
  'hsl(50, 80%, 50%)',
  'hsl(100, 50%, 45%)',
]

export default function CategoryPieChart({ data, label }: Props) {
  const { enabled: maskOn, text, maskMoney } = useMaskMode()
  const { displayUnit } = useDisplayUnit()
  const { usdPerEur } = useFxRate()

  const formatSlice = useCallback(
    (eurValue: number) => {
      const usd = roundUsdFromEur(eurValue, usdPerEur)
      if (displayUnit === 'usd') {
        return `${formatUsd(usd)} · ${formatEur(eurValue, true)} EUR`
      }
      return `${formatEur(eurValue, true)} · ${formatUsd(usd)} USD`
    },
    [displayUnit, usdPerEur],
  )

  const displayData = useMemo(() => {
    if (!maskOn) return data
    return data.map((d, i) => ({
      name: text(d.name),
      value: maskMoney(d.value, `pie:${d.name}:${i}`),
    }))
  }, [data, maskOn, text, maskMoney])

  if (displayData.length === 0) {
    return <p className="text-muted-foreground text-sm py-4">No data</p>
  }

  return (
    <div className="h-72">
      {label && <h3 className="text-sm font-medium text-muted-foreground mb-2">{label}</h3>}
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={displayData}
            cx="50%"
            cy="50%"
            innerRadius={50}
            outerRadius={90}
            paddingAngle={2}
            dataKey="value"
            nameKey="name"
          >
            {displayData.map((_, i) => (
              <Cell key={i} fill={COLORS[i % COLORS.length]} />
            ))}
          </Pie>
          <Tooltip
            formatter={(value: number) => formatSlice(value)}
            contentStyle={{
              backgroundColor: 'hsl(var(--card))',
              border: '1px solid hsl(var(--border))',
              borderRadius: '0.5rem',
              color: 'hsl(var(--card-foreground))',
            }}
          />
          <Legend />
        </PieChart>
      </ResponsiveContainer>
    </div>
  )
}
