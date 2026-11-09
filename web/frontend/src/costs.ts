import { getLanguage, t } from './i18n'

interface DisplayCost {
  amount: number | null
  currency: string
  approximate: boolean
  unconverted_currencies: string[]
}

export function formatCost(cost: DisplayCost, digits = 4) {
  if (cost.unconverted_currencies.length) {
    return `${t('汇率未配置')} (${cost.unconverted_currencies.join(', ')})`
  }
  if (cost.amount == null) return '—'
  const amount = cost.amount.toLocaleString(getLanguage(), {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  })
  return `${cost.approximate ? '≈ ' : ''}${amount} ${cost.currency}`
}
