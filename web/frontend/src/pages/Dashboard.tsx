import { cloneElement, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { ActivityCalendar } from 'react-activity-calendar'
import { api, useAction, useConsole } from '../console'
import type { Entity } from '../console'
import { PageHeader } from '../components/UI'
import { Empty, ErrorBar } from '../components/Form'
import { getLanguage, t } from '../i18n'

interface ActivityBucket {
  date: string; future: boolean; calls: number; success_rate: number | null
  cache_hit_rate: number | null; input_tokens: number | null; output_tokens: number | null
  cached_tokens: number | null; first_token_ms: number | null; hour?: number
}
interface Activity {
  timezone: string; today: string; start: string; end: string
  days?: ActivityBucket[]; hours?: ActivityBucket[]
}

const dateValue = (value: string) => new Date(`${value}T12:00:00Z`)
const number = (value: number | null) => value == null ? '—' : value.toLocaleString(getLanguage())
const rate = (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(1)}%`

function bucketTooltip(bucket: ActivityBucket, target: Element) {
  const rect = target.getBoundingClientRect()
  const width = Math.min(288, window.innerWidth - 32)
  const above = rect.bottom + 300 > window.innerHeight
  return { bucket, target, left: Math.max(16, Math.min(rect.left + rect.width / 2 - width / 2, window.innerWidth - width - 16)),
    top: above ? rect.top - 10 : rect.bottom + 10, above }
}

function RequestActivity() {
  const { data } = useConsole()
  const [granularity, setGranularity] = useState<'day' | 'hour'>('day')
  const [anchor, setAnchor] = useState('')
  const [activity, setActivity] = useState<Activity | null>(null)
  const [error, setError] = useState('')
  const [tooltip, setTooltip] = useState<ReturnType<typeof bucketTooltip> | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setError(''); setActivity(null); setTooltip(null)
    api<Activity>(`/diagnostics/activity?period=${granularity === 'day' ? '26w' : 'week'}&granularity=${granularity}${anchor ? `&date=${anchor}` : ''}`, { signal: controller.signal })
      .then(setActivity).catch(cause => { if (!controller.signal.aborted) setError(cause.message) })
    return () => controller.abort()
  }, [granularity, anchor, data])
  useEffect(() => {
    const close = () => setTooltip(null)
    const scroll = () => setTooltip(current => current && bucketTooltip(current.bucket, current.target))
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') close() }
    window.addEventListener('resize', close)
    window.addEventListener('scroll', scroll, true)
    window.addEventListener('keydown', escape)
    return () => {
      window.removeEventListener('resize', close)
      window.removeEventListener('scroll', scroll, true)
      window.removeEventListener('keydown', escape)
    }
  }, [])
  const locale = getLanguage()
  const formatDate = (date: string, options: Intl.DateTimeFormatOptions) =>
    dateValue(date).toLocaleDateString(locale, { ...options, timeZone: 'UTC' })
  const weekdays = Array.from({ length: 7 }, (_, index) => new Date(Date.UTC(2026, 10, 1 + index))
    .toLocaleDateString(locale, { weekday: 'short', timeZone: 'UTC' }))
  const months = Array.from({ length: 12 }, (_, index) => new Date(Date.UTC(2026, index, 1))
    .toLocaleDateString(locale, { month: 'short', timeZone: 'UTC' }))
  const buckets = activity?.[granularity === 'day' ? 'days' : 'hours'] ?? []
  const byDate = new Map(buckets.map(bucket => [bucket.date, bucket]))
  const maximum = Math.max(1, ...buckets.map(bucket => bucket.calls))
  const level = (bucket: ActivityBucket) => bucket.calls ? Math.max(1, Math.ceil(bucket.calls / maximum * 4)) : 0
  const current = activity && activity.start <= activity.today && activity.end >= activity.today
  const label = activity ? `${formatDate(activity.start, { year: 'numeric', month: 'short', day: 'numeric' })} – ${formatDate(activity.end, { year: 'numeric', month: 'short', day: 'numeric' })}` : ''

  function move(direction: number) {
    const date = dateValue(activity!.start)
    date.setUTCDate(date.getUTCDate() + direction * (granularity === 'day' ? 182 : 7))
    // The daily range ends in the week containing its anchor.
    if (granularity === 'day') date.setUTCDate(date.getUTCDate() + 175)
    setAnchor(date.toISOString().slice(0, 10))
  }

  function showBucket(bucket: ActivityBucket, target: Element) {
    if (bucket.future) return
    setTooltip(bucketTooltip(bucket, target))
  }

  const hourLabel = (hour: number) => `${String(hour).padStart(2, '0')}:00`
  const bucketLabel = (bucket: ActivityBucket) =>
    `${formatDate(bucket.date, { year: 'numeric', month: 'long', day: 'numeric', weekday: 'short' })}${bucket.hour == null ? '' : ` · ${hourLabel(bucket.hour)} – ${hourLabel(bucket.hour + 1)}`}`
  const cellProps = (bucket: ActivityBucket) => ({
    className: `activity-cell heat-${level(bucket)}${bucket.future ? ' future' : ''}`,
    role: 'button', tabIndex: bucket.future ? -1 : 0,
    'data-date': bucket.date, 'data-hour': bucket.hour, 'data-calls': bucket.calls,
    'aria-label': `${bucketLabel(bucket)} · ${t('调用次数')} ${number(bucket.calls)}`,
    'aria-disabled': bucket.future,
    'aria-describedby': tooltip?.bucket === bucket ? 'activity-tooltip' : undefined,
    onMouseEnter: (event: { currentTarget: Element }) => showBucket(bucket, event.currentTarget),
    onFocus: (event: { currentTarget: Element }) => showBucket(bucket, event.currentTarget),
    onBlur: () => setTooltip(null),
    onClick: (event: { currentTarget: Element }) => showBucket(bucket, event.currentTarget),
    onKeyDown: (event: { key: string; preventDefault: () => void; currentTarget: Element }) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); showBucket(bucket, event.currentTarget) }
    },
  })

  return <section className="card activity-card" aria-labelledby="activity-title">
    <div className="panel-head activity-heading"><h2 className="panel-title" id="activity-title">{t('请求趋势')}</h2>
      <div className="segmented" role="tablist" aria-label={t('趋势粒度')}>
        {([['day', '按天'], ['hour', '按小时']] as const).map(([value, label]) =>
          <button key={value} role="tab" aria-selected={granularity === value} onClick={() => {
            if (granularity === value) return
            setActivity(null); setTooltip(null); setGranularity(value); setAnchor('')
          }}>{t(label)}</button>)}
      </div>
    </div>
    <div className="activity-body" aria-busy={!activity && !error}>
      <ErrorBar error={error} />
      {activity ? <>
        <div className="activity-toolbar"><div className="activity-range"><strong>{label}</strong>
          <span>{t(granularity === 'day' ? '26 周 · 每格一天' : '一周 · 每格一小时')} · {t('调用次数')} {number(buckets.reduce((sum, bucket) => sum + bucket.calls, 0))}</span>
        </div><div className="activity-navigation">
          <button className="icon-btn" aria-label={t('上一时段')} onClick={() => move(-1)}><ChevronLeft size={15} /></button>
          <button className="btn btn-sm" disabled={!!current} onClick={() => setAnchor('')}>{t('当前时段')}</button>
          <button className="icon-btn" disabled={activity.end >= activity.today}
            aria-label={t('下一时段')} onClick={() => move(1)}><ChevronRight size={15} /></button>
        </div></div>
        <div className={`activity-plot ${granularity}`} onMouseLeave={() => setTooltip(null)}>
          {granularity === 'day' ? <ActivityCalendar data={buckets.map(bucket => ({
            date: bucket.date, count: bucket.calls, level: level(bucket),
          }))} weekStart={1} blockSize={22} blockMargin={5} blockRadius={4} fontSize={11}
            colorScheme="light" theme={{ light: ['#edf0f1', '#c6edf5', '#87d9ed', '#42c2e1', '#08a7d3'] }}
            showWeekdayLabels showColorLegend={false} showTotalCount={false} labels={{ months, weekdays }}
            renderBlock={(block, day) => cloneElement(block, cellProps(byDate.get(day.date)!))} />
            : <svg className="activity-hour-chart" width="742" height="217" viewBox="0 0 742 217"
              aria-label={t('每周小时调用热力图')}>
              {Array.from({ length: 24 }, (_, hour) => <text key={hour} x={94 + hour * 27} y={12}
                textAnchor="middle" className="activity-axis">{String(hour).padStart(2, '0')}</text>)}
              {buckets.filter(bucket => bucket.hour === 0).map((bucket, index) =>
                <text key={bucket.date} x={68} y={41 + index * 27} textAnchor="end" className="activity-axis">
                  {formatDate(bucket.date, { month: 'numeric', day: 'numeric' })} {formatDate(bucket.date, { weekday: 'short' })}
                </text>)}
              {buckets.map((bucket, index) => <rect key={`${bucket.date}-${bucket.hour}`} x={83 + bucket.hour! * 27}
                y={26 + Math.floor(index / 24) * 27} width={22} height={22} rx={4} {...cellProps(bucket)} />)}
            </svg>}
        </div>
        <div className="activity-footer"><span>{t(granularity === 'day' ? '每格一天 · 颜色越深，调用越多' : '每格一小时 · 颜色越深，调用越多')}</span>
          <span className="activity-legend" aria-label={t('调用密度')}>{t('少')}{[0, 1, 2, 3, 4].map(level =>
            <i className={`heat-${level}`} key={level} />)}{t('多')}</span></div>
        <div className="activity-timezone">{t('统计时区')} · {activity.timezone}
          {granularity === 'hour' && <> · {t('按本地小时汇总，夏令时重复小时合并。')}</>}</div>
      </> : !error && <Empty text="正在加载" />}
    </div>
    {tooltip && activity && createPortal(<div role="tooltip" id="activity-tooltip"
      className={`activity-tooltip heat-${level(tooltip.bucket)}${tooltip.above ? ' above' : ''}`} style={{ left: tooltip.left, top: tooltip.top }}>
      <strong>{bucketLabel(tooltip.bucket)}</strong>
      <dl>{[
        ['调用次数', number(tooltip.bucket.calls)], ['成功率', rate(tooltip.bucket.success_rate)],
        ['缓存命中率', rate(tooltip.bucket.cache_hit_rate)], ['输入 Token', number(tooltip.bucket.input_tokens)],
        ['输出 Token', number(tooltip.bucket.output_tokens)], ['缓存命中 Token', number(tooltip.bucket.cached_tokens)],
        ['平均首字延迟', tooltip.bucket.first_token_ms == null ? '—' : `${Math.round(tooltip.bucket.first_token_ms)} ms`],
      ].map(([label, value]) => <div key={label}><dt>{t(label)}</dt><dd>{value}</dd></div>)}</dl>
      <span>{t('仅汇总已知用量')} · {activity.timezone}</span>
    </div>, document.body)}
  </section>
}

export default function Dashboard() {
  const { data, refresh } = useConsole()
  const navigate = useNavigate()
  const action = useAction()
  const [period, setPeriod] = useState('today')
  const [overview, setOverview] = useState<Entity | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    setOverview(null); setError('')
    api(`/diagnostics/usage?period=${period}`, { signal: controller.signal })
      .then(setOverview).catch(cause => { if (!controller.signal.aborted) setError(cause.message) })
    return () => controller.abort()
  }, [period, data])
  const metrics = overview?.summary
  return <div className="page"><PageHeader title="总览"
    actions={<><button className="btn" onClick={() => { void action.run(refresh) }}>{t('刷新')}</button><button className="btn btn-primary" onClick={() => navigate('/upstreams')}>{t('管理上游')}</button></>} />
    <ErrorBar error={action.error || error} />
    <div className="toolbar dashboard-range"><div className="segmented" role="tablist" aria-label={t('数据范围')}>
      {[['today', '当天'], ['24h', '24 小时'], ['7d', '7 天'], ['30d', '30 天'], ['all', '总数据']].map(([value, label]) =>
        <button key={value} role="tab" aria-selected={period === value} onClick={() => setPeriod(value)}>{t(label)}</button>)}
    </div>{overview && <span className="hint">{t('统计时区')} · {overview.timezone}</span>}</div>
    <div className="stat-grid" aria-busy={!overview && !error}>{[
      [t('请求次数'), number(metrics?.calls ?? null), metrics ? `${t('已完成')} ${number(metrics.finished)}` : '—'],
      [t('Token 总量'), number(metrics?.total_tokens ?? null), t('仅汇总已知用量')],
      [t('缓存命中率'), rate(metrics?.cache_hit_rate ?? null), t('按已知输入 Token 加权')],
      [t('成功率'), rate(metrics?.success_rate ?? null), metrics ? `${t('成功')} ${number(metrics.successful)} / ${number(metrics.finished)}` : '—'],
    ].map(([label, value, foot]) => <div className="stat-card" key={label}><div className="stat-label">{label}</div><div className="stat-value">{value}</div><div className="stat-foot">{foot}</div></div>)}</div>
    <RequestActivity />
  </div>
}
