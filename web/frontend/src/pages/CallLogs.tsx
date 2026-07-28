import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Cpu,
  ChevronLeft,
  ChevronRight,
  Factory,
  LoaderCircle,
  RefreshCw,
  XCircle,
} from 'lucide-react'
import { useAdmin } from '../AdminContext'
import {
  adminApi,
  type InvocationLogItem,
  type InvocationOutcome,
  type HourlyStatistics,
} from '../adminApi'
import DatePicker, { localDate } from '../components/DatePicker'
import HourPicker from '../components/HourPicker'
import { Badge, Card, EmptyState, SectionTitle, Subtabs } from '../components/UI'

const numberFormat = new Intl.NumberFormat('zh-CN')
const PAGE_SIZE = 20

function integer(value: number | null): string {
  return value === null ? '—' : numberFormat.format(value)
}

function formatTime(value: string | null): string {
  if (!value) return '尚未结束'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

function latency(value: number | null): string {
  if (value === null) return '—'
  return `${value.toFixed(value >= 100 ? 0 : 1)} ms`
}

function statusView(status: InvocationLogItem['status']) {
  if (status === 'completed' || status === 'requires_action') {
    return { label: '成功', tone: 'success' as const, icon: CheckCircle2 }
  }
  if (status === 'failed' || status === 'incomplete') {
    return { label: '失败', tone: 'danger' as const, icon: XCircle }
  }
  if (status === 'cancelled') {
    return { label: '已取消', tone: 'warning' as const, icon: AlertTriangle }
  }
  return { label: '执行中', tone: 'primary' as const, icon: LoaderCircle }
}

function InvocationCard({ item }: { item: InvocationLogItem }) {
  const status = statusView(item.status)
  const StatusIcon = status.icon
  const failed = item.status === 'failed' || item.status === 'incomplete'

  return <article className={`invocation-card invocation-${status.tone}`}>
    <header className="invocation-head">
      <div className="invocation-model">
        <span>实际模型名</span>
        <strong title={item.model}>{item.model}</strong>
      </div>
      <Badge tone={status.tone}><StatusIcon className={item.status === 'running' ? 'spin' : ''} size={13}/>{status.label}</Badge>
    </header>

    <div className="invocation-facts">
      <div><Factory size={15}/><span>涉及厂商</span><strong>{item.provider_id}</strong></div>
      <div><Cpu size={15}/><span>厂商原模型名</span><strong title={item.provider_model}>{item.provider_model}</strong></div>
      <div><Clock3 size={15}/><span>调用时间</span><strong>{formatTime(item.started_at)}</strong><small>延迟 {latency(item.latency_ms)}</small></div>
    </div>

    <div className="invocation-token-row">
      <div className="invocation-token-total"><span>Token 计量</span><strong>{integer(item.tokens.total_tokens)}</strong><small>{item.usage.exact === true ? 'Provider 精确计量' : item.usage.mode ? `${item.usage.mode} 计量` : '暂无完整计量'}</small></div>
      <div><span>输入</span><b>{integer(item.tokens.input_tokens)}</b></div>
      <div><span>缓存</span><b>{integer(item.tokens.cached_input_tokens)}</b></div>
      <div><span>输出</span><b>{integer(item.tokens.output_tokens)}</b></div>
      <div><span>推理</span><b>{integer(item.tokens.reasoning_tokens)}</b></div>
    </div>

    {failed && <div className="invocation-error" role="note">
      <AlertTriangle size={17}/>
      <div><span>失败日志</span><strong>{item.error_code || 'UNKNOWN_ERROR'}{item.error_type ? ` · ${item.error_type}` : ''}</strong><p>{item.error_message || '该历史调用只记录了错误码，没有可展示的脱敏错误信息。'}</p></div>
    </div>}
  </article>
}

export default function CallLogs() {
  const { csrfToken: token } = useAdmin()
  const [date, setDate] = useState(localDate)
  const [hour, setHour] = useState<number | null>(null)
  const [outcome, setOutcome] = useState<InvocationOutcome>('all')
  const [items, setItems] = useState<InvocationLogItem[]>([])
  const [page, setPage] = useState(1)
  const [pageCount, setPageCount] = useState(1)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [hourly, setHourly] = useState<HourlyStatistics['items']>()

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await adminApi.invocationLogs(
        token,
        outcome,
        page,
        PAGE_SIZE,
        date || undefined,
        hour,
      )
      const databasePaginated = typeof result.total === 'number' && typeof result.pages === 'number'
      const nextTotal = databasePaginated ? result.total! : result.items.length
      const nextPageCount = databasePaginated
        ? result.pages!
        : Math.max(1, Math.ceil(result.items.length / PAGE_SIZE))
      setItems(databasePaginated
        ? result.items
        : result.items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE))
      setTotal(nextTotal)
      setPageCount(nextPageCount)
      if (page > nextPageCount) setPage(nextPageCount)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '调用日志读取失败')
    } finally {
      setLoading(false)
    }
  }, [date, hour, outcome, page, token])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    if (!date) {
      setHourly(undefined)
      return
    }
    let active = true
    void adminApi.hourlyStatistics(token, date)
      .then(result => { if (active) setHourly(result.items) })
      .catch(() => { if (active) setHourly(undefined) })
    return () => { active = false }
  }, [date, token])

  const firstItem = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1
  const lastItem = Math.min(page * PAGE_SIZE, total)

  const changeOutcome = (nextOutcome: InvocationOutcome) => {
    setOutcome(nextOutcome)
    setPage(1)
  }

  const changeDate = (nextDate: string) => {
    setDate(nextDate)
    if (!nextDate) setHour(null)
    setPage(1)
  }

  const changeHour = (nextHour: number | null) => {
    setHour(nextHour)
    setPage(1)
  }

  return <>
    <SectionTitle
      title="调用日志"
      description="所选日期内的真实模型调用；失败信息经过 Provider 和网关脱敏"
      action={<div className="stats-toolbar call-log-toolbar">
        <button className="btn primary" disabled={loading} onClick={() => void load()}><RefreshCw className={loading ? 'spin' : ''} size={14}/>{loading ? '读取中' : '刷新'}</button>
        <DatePicker value={date} onChange={changeDate}/>
      <HourPicker value={hour} onChange={changeHour} statistics={hourly}/>
      </div>}
    />
    {error && <div className="alert"><AlertTriangle size={18}/><span>{error}</span></div>}

    <Card className="invocation-log-shell">
      <header className="invocation-log-toolbar">
        <div><span>调用日志行列</span><strong>{loading ? '正在同步' : `${numberFormat.format(total)} 条记录`}</strong></div>
        <Subtabs<InvocationOutcome>
          items={[{ id: 'all', label: '全部调用' }, { id: 'success', label: '成功' }, { id: 'failure', label: '失败' }]}
          value={outcome}
          onChange={changeOutcome}
        />
      </header>

      {loading ? <div className="invocation-loading"><LoaderCircle className="spin"/><span>正在读取调用日志…</span></div> : items.length ? <>
        <div className="invocation-list">{items.map((item, index) => <InvocationCard key={`${item.started_at}:${item.model}:${(page - 1) * PAGE_SIZE + index}`} item={item}/>)}</div>
        <footer className="invocation-pagination">
          <span>显示 {firstItem}–{lastItem} / {numberFormat.format(total)} 条</span>
          <div>
            <button className="btn" disabled={page === 1} onClick={() => setPage((current) => Math.max(1, current - 1))} aria-label="上一页"><ChevronLeft size={14}/>上一页</button>
            <strong>第 {page} / {pageCount} 页</strong>
            <button className="btn" disabled={page >= pageCount} onClick={() => setPage((current) => Math.min(pageCount, current + 1))} aria-label="下一页">下一页<ChevronRight size={14}/></button>
          </div>
        </footer>
      </> : <EmptyState title="暂无调用日志" description="产生真实 Provider 调用后，这里会显示对应记录。"/>}
    </Card>
  </>
}
