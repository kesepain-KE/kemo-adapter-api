import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, DatabaseZap, RefreshCw } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import {
  adminApi,
  type DailyStatistics,
  type StatisticsRanking,
} from '../adminApi'
import { Badge, Card, CardHeader, EmptyState, SectionTitle, Subtabs } from '../components/UI'
import DatePicker, { localDate } from '../components/DatePicker'

type Dimension = 'provider' | 'model' | 'gateway_key'

function integer(value: number | null): string {
  return value === null ? '—' : new Intl.NumberFormat('zh-CN').format(value)
}

function percent(value: number | null): string {
  return value === null ? '—' : `${(value * 100).toFixed(1)}%`
}

function latency(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(value >= 100 ? 0 : 1)} ms`
}

export default function Logs() {
  const { token } = useAdmin()
  const [date, setDate] = useState(localDate)
  const [dimension, setDimension] = useState<Dimension>('provider')
  const [daily, setDaily] = useState<DailyStatistics | null>(null)
  const [rankings, setRankings] = useState<Record<Dimension, StatisticsRanking | null>>({
    provider: null,
    model: null,
    gateway_key: null,
  })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [summary, providers, models, keys] = await Promise.all([
        adminApi.dailyStatistics(token, date),
        adminApi.statisticsRanking(token, date, 'provider'),
        adminApi.statisticsRanking(token, date, 'model'),
        adminApi.statisticsRanking(token, date, 'gateway_key'),
      ])
      setDaily(summary)
      setRankings({ provider: providers, model: models, gateway_key: keys })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '统计数据读取失败')
    } finally {
      setLoading(false)
    }
  }, [date, token])

  useEffect(() => { void load() }, [load])

  const activeRanking = rankings[dimension]
  const dimensionLabel = {
    provider: '厂商',
    model: '模型',
    gateway_key: '网关密钥',
  }[dimension]

  return <>
    <SectionTitle
      title="统计与日志"
      description="每日真实 Provider 调用汇总；日期边界由网关统计时区决定"
      action={<div className="stats-toolbar"><DatePicker value={date} onChange={setDate}/><button className="btn primary" onClick={() => void load()} disabled={loading}><RefreshCw size={14}/>{loading ? '读取中' : '刷新'}</button></div>}
    />

    {error && <div className="alert"><AlertTriangle size={18}/><span>{error}</span></div>}

    {daily && <>
      {!daily.storage.healthy && <div className="alert"><DatabaseZap size={18}/><span>统计存储当前异常，已丢弃 {daily.storage.dropped_events} 个事件；模型调用未受影响。</span></div>}
      <div className="metrics-grid">
        <Card className="metric"><span>调用次数</span><strong>{integer(daily.calls)}</strong><em>{daily.running} 个仍在执行 · {daily.replay_count} 次幂等重放</em></Card>
        <Card className="metric"><span>成功率</span><strong>{percent(daily.success_rate)}</strong><em>{daily.successes} 成功 · {daily.failures} 失败</em></Card>
        <Card className="metric"><span>Token 用量</span><strong>{integer(daily.tokens.total_tokens)}</strong><em>{daily.token_coverage.total_tokens ?? 0} 个调用提供总 Token</em></Card>
        <Card className="metric"><span>平均延迟</span><strong>{latency(daily.average_latency_ms)}</strong><em>只统计已到达终态的调用</em></Card>
      </div>

      <div className="two-grid stats-detail-grid">
        <Card><CardHeader title="Token 明细" description="未知计量保持为空，不按 0 处理"/><div className="info-list"><div><span>输入 Token</span><strong>{integer(daily.tokens.input_tokens)}</strong></div><div><span>输出 Token</span><strong>{integer(daily.tokens.output_tokens)}</strong></div><div><span>推理 Token</span><strong>{integer(daily.tokens.reasoning_tokens)}</strong></div><div><span>可见输出 Token</span><strong>{integer(daily.tokens.visible_output_tokens)}</strong></div></div></Card>
        <Card><CardHeader title="缓存命中" description="只使用 Provider 明确标记为精确的样本" action={<Badge tone={daily.cache_hit_rate === null ? 'muted' : 'success'}>{percent(daily.cache_hit_rate)}</Badge>}/><div className="info-list"><div><span>命中 Token</span><strong>{integer(daily.tokens.cached_input_tokens)}</strong></div><div><span>可计算样本</span><strong>{daily.cache_eligible_samples}</strong></div><div><span>取消</span><strong>{daily.cancellations}</strong></div><div><span>未完整结束</span><strong>{daily.incompletes}</strong></div></div></Card>
      </div>

      <SectionTitle title="调用排行" description="排行依据当天真实调用次数，Token 与延迟仍保留各自数据覆盖率"/>
      <Subtabs<Dimension> items={[{ id: 'provider', label: '厂商' }, { id: 'model', label: '模型' }, { id: 'gateway_key', label: '网关密钥' }]} value={dimension} onChange={setDimension}/>
      {activeRanking?.items.length ? <Card className="table-card">
        <table>
          <thead><tr><th>{dimensionLabel}</th><th>调用</th><th>成功率</th><th>总 Token</th><th>缓存命中率</th><th>平均延迟</th></tr></thead>
          <tbody>{activeRanking.items.map(item => <tr key={item.id}><td><code>{item.id}</code></td><td>{item.calls}</td><td>{percent(item.success_rate)}</td><td>{integer(item.tokens.total_tokens)}</td><td>{percent(item.cache_hit_rate)}</td><td>{latency(item.average_latency_ms)}</td></tr>)}</tbody>
        </table>
      </Card> : <EmptyState title={`暂无${dimensionLabel}调用`} description={`${date} 尚未记录该维度的真实 Provider 执行。`}/>}
    </>}

    {!daily && !loading && !error && <EmptyState title="暂无统计数据" description="当天第一次真实 Provider 调用完成后，这里会显示统计。"/>}
  </>
}
