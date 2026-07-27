import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { Activity, CircleAlert, Clock3, ShieldCheck, TimerReset } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import { adminApi, type DailyStatistics } from '../adminApi'
import { Badge, Card, CardHeader } from '../components/UI'

function integer(value: number | null | undefined): string {
  return value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value)
}

function percent(value: number | null | undefined): string {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

function runtimeDuration(startedAtMs: number, nowMs: number): string {
  if (!Number.isFinite(startedAtMs)) return '未知'
  const totalSeconds = Math.max(0, Math.floor((nowMs - startedAtMs) / 1000))
  const days = Math.floor(totalSeconds / 86_400)
  const hours = Math.floor((totalSeconds % 86_400) / 3_600)
  const minutes = Math.floor((totalSeconds % 3_600) / 60)
  const seconds = totalSeconds % 60
  const clock = [hours, minutes, seconds].map(value => String(value).padStart(2, '0')).join(':')
  return days > 0 ? `${days} 天 ${clock}` : clock
}

function RuntimeDuration({ startedAt }: { startedAt: Date }) {
  const startedAtMs = startedAt.getTime()
  const [nowMs, setNowMs] = useState(Date.now())

  useEffect(() => {
    setNowMs(Date.now())
    const timer = window.setInterval(() => setNowMs(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [startedAtMs])

  return <>{runtimeDuration(startedAtMs, nowMs)}</>
}

type TrendMode = 'day' | 'week' | 'month'

interface TrendPoint {
  label: string
  calls: number
  tokens: number | null
  inputTokens: number | null
  outputTokens: number | null
  cachedTokens: number | null
  cacheHitRate: number | null
  successRate: number | null
}

function localDate(value: Date): string {
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function TrendChart({ points, mode }: { points: TrendPoint[]; mode: TrendMode }) {
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null)
  const [pointerPosition, setPointerPosition] = useState<{ x: number; y: number; width: number; height: number } | null>(null)
  const [tooltipSize, setTooltipSize] = useState({ width: 270, height: 190 })
  const tooltipRef = useRef<HTMLDivElement | null>(null)
  const width = 1000
  const height = 250
  const plot = { left: 54, right: 946, top: 24, bottom: 202 }
  const callMax = Math.max(1, ...points.map(point => point.calls))
  const tokenValues = points.flatMap(point => point.tokens == null ? [] : [point.tokens])
  const tokenMax = Math.max(1, ...tokenValues)
  const x = (index: number) => points.length <= 1 ? plot.left : plot.left + (index / (points.length - 1)) * (plot.right - plot.left)
  const y = (value: number, maximum: number) => plot.bottom - (value / maximum) * (plot.bottom - plot.top)
  const callLine = points.map((point, index) => `${x(index)},${y(point.calls, callMax)}`).join(' ')
  // 未计量仍在气泡中显示为未知；绘图使用零基线连接，避免时间序列断裂。
  const tokenLine = points.map((point, index) => `${x(index)},${y(point.tokens ?? 0, tokenMax)}`).join(' ')
  const labelEvery = mode === 'day' ? 4 : mode === 'week' ? 1 : 5
  const activePoint = hoveredIndex == null ? null : points[hoveredIndex]
  const activeX = hoveredIndex == null ? null : x(hoveredIndex)

  useLayoutEffect(() => {
    if (!activePoint || !tooltipRef.current) return
    const bounds = tooltipRef.current.getBoundingClientRect()
    setTooltipSize(current => current.width === bounds.width && current.height === bounds.height ? current : { width: bounds.width, height: bounds.height })
  }, [activePoint])

  const updatePointerPosition = (event: ReactPointerEvent<SVGSVGElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect()
    setPointerPosition({ x: event.clientX - bounds.left, y: event.clientY - bounds.top, width: bounds.width, height: bounds.height })
  }

  const tooltipPosition = useMemo(() => {
    if (!pointerPosition) return null
    const gap = 18
    const edge = 8
    const { x: pointerX, y: pointerY, width: areaWidth, height: areaHeight } = pointerPosition
    const { width: tooltipWidth, height: tooltipHeight } = tooltipSize
    const clamp = (value: number, minimum: number, maximum: number) => maximum < minimum ? minimum : Math.min(Math.max(value, minimum), maximum)
    const verticalCenter = clamp(pointerY - tooltipHeight / 2, edge, areaHeight - tooltipHeight - edge)
    const horizontalCenter = clamp(pointerX - tooltipWidth / 2, edge, areaWidth - tooltipWidth - edge)
    const candidates = [
      { left: pointerX + gap, top: verticalCenter },
      { left: pointerX - gap - tooltipWidth, top: verticalCenter },
      { left: horizontalCenter, top: pointerY + gap },
      { left: horizontalCenter, top: pointerY - gap - tooltipHeight },
    ]
    const overflow = ({ left, top }: { left: number; top: number }) =>
      Math.max(0, edge - left) + Math.max(0, left + tooltipWidth - areaWidth + edge) +
      Math.max(0, edge - top) + Math.max(0, top + tooltipHeight - areaHeight + edge)
    return candidates.reduce((best, candidate) => overflow(candidate) < overflow(best) ? candidate : best)
  }, [pointerPosition, tooltipSize])

  return <div className="trend-chart-wrap">
    <svg className="trend-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="网关真实调用次数与 Token 使用量折线图" onPointerMove={updatePointerPosition} onPointerLeave={() => { setHoveredIndex(null); setPointerPosition(null) }}>
      <defs><linearGradient id="calls-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#4f67f6" stopOpacity=".18"/><stop offset="1" stopColor="#4f67f6" stopOpacity="0"/></linearGradient></defs>
      {[0, .25, .5, .75, 1].map(value => <line key={value} className="trend-grid-line" x1={plot.left} x2={plot.right} y1={plot.top + value * (plot.bottom - plot.top)} y2={plot.top + value * (plot.bottom - plot.top)}/>)}
      {points.length > 1 && <polygon
        className="trend-calls-area"
        points={`${plot.left},${plot.bottom} ${callLine} ${plot.right},${plot.bottom}`}
      />}
      <polyline className="trend-line trend-line-calls" points={callLine}/>
      <polyline className="trend-line trend-line-tokens" points={tokenLine}/>
      {activeX != null && <line className="trend-hover-guide" x1={activeX} x2={activeX} y1={plot.top} y2={plot.bottom}/>}
      {points.map((point, index) => <g key={`${point.label}-${index}`} className="trend-point">
        <circle className="trend-dot-calls" cx={x(index)} cy={y(point.calls, callMax)} r="3"><title>{point.label} · {point.calls} 次调用</title></circle>
        {point.tokens != null && <circle className="trend-dot-tokens" cx={x(index)} cy={y(point.tokens, tokenMax)} r="3"><title>{point.label} · {point.tokens} Tokens</title></circle>}
        {(index % labelEvery === 0 || index === points.length - 1) && <text className="trend-axis-label" x={x(index)} y="226" textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'}>{point.label}</text>}
      </g>)}
      <text className="trend-scale-label" x={plot.left} y="14">调用峰值 {integer(callMax)}</text>
      <text className="trend-scale-label" x={plot.right} y="14" textAnchor="end">Token 峰值 {tokenValues.length ? integer(tokenMax) : '无可靠计量'}</text>
      {points.map((point, index) => {
        const from = index === 0 ? plot.left : (x(index - 1) + x(index)) / 2
        const to = index === points.length - 1 ? plot.right : (x(index) + x(index + 1)) / 2
        return <rect key={`hit-${point.label}-${index}`} className="trend-hit-zone" x={from} y={plot.top} width={Math.max(1, to - from)} height={plot.bottom - plot.top} onPointerEnter={() => setHoveredIndex(index)}/>
      })}
    </svg>
    {activePoint && activeX != null && tooltipPosition && <div ref={tooltipRef} className="trend-tooltip" style={{ left: tooltipPosition.left, top: tooltipPosition.top }}>
      <header><strong>{activePoint.label}</strong><span>调用 <b>{integer(activePoint.calls)}</b> 次</span></header>
      <div>
        <span><small>Token 使用量</small><b>{integer(activePoint.tokens)}</b></span>
        <span><small>输入 Token</small><b>{integer(activePoint.inputTokens)}</b></span>
        <span><small>输出 Token</small><b>{integer(activePoint.outputTokens)}</b></span>
        <span><small>缓存 Token</small><b>{integer(activePoint.cachedTokens)}</b></span>
        <span><small>Token 缓存率</small><b>{percent(activePoint.cacheHitRate)}</b></span>
        <span><small>调用成功率</small><b>{percent(activePoint.successRate)}</b></span>
      </div>
      {activePoint.tokens == null && <p>该时段存在调用，但没有可靠 Token 计量样本。</p>}
    </div>}
  </div>
}

export default function Dashboard() {
  const { token, data, restart, isOwner } = useAdmin()
  const [statistics, setStatistics] = useState<DailyStatistics | null>(null)
  const [statisticsError, setStatisticsError] = useState('')
  const [trendMode, setTrendMode] = useState<TrendMode>('day')
  const [trend, setTrend] = useState<TrendPoint[]>([])
  const [trendLoading, setTrendLoading] = useState(true)
  const [trendError, setTrendError] = useState('')
  const runtime = restart?.gateway ?? data.runtime
  const startedAt = new Date(runtime.started_at)

  const loadStatistics = useCallback(async () => {
    try {
      setStatistics(await adminApi.dailyStatistics(token))
      setStatisticsError('')
    } catch (reason) {
      setStatistics(null)
      setStatisticsError(reason instanceof Error ? reason.message : '今日统计读取失败')
    }
  }, [token])

  useEffect(() => {
    void loadStatistics()
    const timer = window.setInterval(() => { void loadStatistics() }, 15_000)
    return () => window.clearInterval(timer)
  }, [loadStatistics])

  const loadTrend = useCallback(async () => {
    setTrendLoading(true)
    try {
      if (trendMode === 'day') {
        const hourly = await adminApi.hourlyStatistics(token)
        setTrend(hourly.items.map(item => ({
          label: item.label,
          calls: item.calls,
          tokens: item.total_tokens,
          inputTokens: item.input_tokens,
          outputTokens: item.output_tokens,
          cachedTokens: item.cached_input_tokens,
          cacheHitRate: item.cache_hit_rate,
          successRate: item.success_rate,
        })))
      } else {
        const days = trendMode === 'week' ? 7 : 30
        const end = new Date()
        const start = new Date(end)
        start.setDate(end.getDate() - days + 1)
        const series = await adminApi.statisticsSeries(token, localDate(start), localDate(end))
        setTrend(series.items.map(item => ({
          label: item.date.slice(5),
          calls: item.calls,
          tokens: item.calls === 0 ? 0 : item.tokens.total_tokens,
          inputTokens: item.calls === 0 ? 0 : item.tokens.input_tokens,
          outputTokens: item.calls === 0 ? 0 : item.tokens.output_tokens,
          cachedTokens: item.calls === 0 ? 0 : item.tokens.cached_input_tokens,
          cacheHitRate: item.cache_hit_rate,
          successRate: item.success_rate,
        })))
      }
      setTrendError('')
    } catch (reason) {
      setTrend([])
      setTrendError(reason instanceof Error ? reason.message : '调用趋势读取失败')
    } finally {
      setTrendLoading(false)
    }
  }, [token, trendMode])

  useEffect(() => {
    void loadTrend()
    const timer = window.setInterval(() => { void loadTrend() }, 15_000)
    return () => window.clearInterval(timer)
  }, [loadTrend])

  const trendTotals = useMemo(() => ({
    calls: trend.reduce((sum, point) => sum + point.calls, 0),
    tokens: trend.reduce<number | null>((sum, point) => point.tokens == null ? sum : (sum ?? 0) + point.tokens, null),
  }), [trend])

  return <>
    <div className="metrics-grid">
      <Card className="metric"><span>调用次数</span><strong>{integer(statistics?.calls)}</strong><em>{statistics ? `${statistics.running} 个执行中 · ${statistics.replay_count} 次幂等重放` : '等待真实统计数据'}</em></Card>
      <Card className="metric"><span>Token 使用量</span><strong>{integer(statistics?.tokens.total_tokens)}</strong><em>{statistics ? `${integer(statistics.tokens.input_tokens)} 输入 · ${integer(statistics.tokens.output_tokens)} 输出` : '未知计量不会按 0 处理'}</em></Card>
      <Card className="metric"><span>Token 缓存率</span><strong>{percent(statistics?.cache_hit_rate)}</strong><em>{statistics ? `${statistics.cache_eligible_samples} 个精确计量样本` : '仅计算精确 Token 样本'}</em></Card>
      <Card className="metric"><span>调用成功率</span><strong>{percent(statistics?.success_rate)}</strong><em>{statistics ? `${statistics.successes} 成功 · ${statistics.failures} 失败` : '等待真实统计数据'}</em></Card>
    </div>
    {statisticsError && <div className="alert"><CircleAlert size={18}/><span>{statisticsError}；仪表盘未显示推测值。</span></div>}
    {data.live_config_error && <div className="alert"><CircleAlert size={18}/><span>运行时配置加载失败，网关正在使用最后一个有效版本：{data.live_config_error}</span></div>}
    <Card className="dashboard-trend-card"><CardHeader title="调用趋势" description="调用次数与 Provider 归一化 Token 的真实统计" action={<div className="trend-modes"><button className={trendMode === 'day' ? 'active' : ''} onClick={() => setTrendMode('day')}>24 小时</button><button className={trendMode === 'week' ? 'active' : ''} onClick={() => setTrendMode('week')}>7 天</button><button className={trendMode === 'month' ? 'active' : ''} onClick={() => setTrendMode('month')}>30 天</button></div>}/><div className="trend-legend"><span><i className="calls"/>调用次数 <b>{integer(trendTotals.calls)}</b></span><span><i className="tokens"/>Token <b>{integer(trendTotals.tokens)}</b></span></div>{trendLoading ? <div className="trend-state">正在读取真实趋势…</div> : trendError ? <div className="trend-state trend-state-error">{trendError}；未生成推测曲线。</div> : <TrendChart points={trend} mode={trendMode}/>}</Card>
    <Card><CardHeader title="进程状态" action={<Badge tone={runtime.phase === 'running' ? 'success' : 'warning'}>{runtime.phase}</Badge>}/><div className="info-list"><div><span><Activity size={15}/> 活动执行</span><strong>{runtime.active_executions}</strong></div><div><span><Clock3 size={15}/> 启动时间</span><strong>{Number.isNaN(startedAt.getTime()) ? '未知' : startedAt.toLocaleString()}</strong></div><div><span><TimerReset size={15}/> 运行时长</span><strong><RuntimeDuration startedAt={startedAt}/></strong></div><div><span><ShieldCheck size={15}/> 管理权限</span><strong>{isOwner ? 'owner' : 'admin:web'}</strong></div></div></Card>
  </>
}
