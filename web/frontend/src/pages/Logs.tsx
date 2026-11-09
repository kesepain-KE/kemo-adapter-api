import { useEffect, useState } from 'react'
import { ChevronRight, RefreshCw, Search } from 'lucide-react'
import { api, useConsole } from '../console'
import type { Entity } from '../console'
import { Modal, PageHeader } from '../components/UI'
import { Empty, ErrorBar, Field } from '../components/Form'
import { Select } from '../components/Select'
import { DateTimePicker } from '../components/DateTimePicker'
import { t } from '../i18n'

const emptyFilters = { start: '', end: '', model: '', task: '', log_type: '', status: '' }
type Filters = typeof emptyFilters
const logLabels: Record<string, string> = { request: '模型调用', 'model-test': '模型测试', 'capability-probe': '能力探测' }
const statusLabels: Record<string, string> = { running: '进行中', completed: '已完成', failed: '失败', cancelled: '已取消' }
const sourceLabels: Record<string, string> = { exact: '厂商精确计量', estimate: '本地估算', partial: '部分厂商用量', unknown: '未知' }
const time = (value: number | null | undefined) => value == null ? '—' : `${Math.round(value)} ms`
const count = (value: number | null | undefined) => value == null ? '—' : value.toLocaleString()
const cacheRatio = (usage: Entity | null) => usage && usage.input_tokens > 0 && usage.cached_input_tokens != null
  ? `${(100 * usage.cached_input_tokens / usage.input_tokens).toFixed(1)}%` : '—'
const cost = (item: Entity) => item.cost == null ? t('未知') : `${item.complete ? '' : '≈ '}${Number(item.cost).toFixed(6)} ${item.currency}`
const source = (item: Entity) => item.key_name || item.key_id || (logLabels[item.subject_id] ? t(logLabels[item.subject_id]) : item.subject_id) || t('历史记录')

export default function Logs() {
  const { data } = useConsole()
  const [draft, setDraft] = useState<Filters>(emptyFilters)
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const [items, setItems] = useState<Entity[]>([])
  const [offset, setOffset] = useState(0)
  const [refresh, setRefresh] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [detail, setDetail] = useState<Entity | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    const params = new URLSearchParams({ limit: '50', offset: String(offset) })
    Object.entries(filters).forEach(([key, value]) => {
      if (value) params.set(key, key === 'start' || key === 'end' ? new Date(value).toISOString() : value)
    })
    setBusy(true); setError('')
    api(`/logs?${params}`, { signal: controller.signal }).then(value => setItems(value.items)).catch(cause => {
      if (!controller.signal.aborted) setError(cause.message)
    }).finally(() => { if (!controller.signal.aborted) setBusy(false) })
    return () => controller.abort()
  }, [filters, offset, refresh])
  const edit = (key: keyof Filters, value: string) => setDraft({ ...draft, [key]: value })
  const options = (labels: Record<string, string>, all: string) => [
    { value: '', label: t(all) }, ...Object.entries(labels).map(([value, label]) => ({ value, label: t(label) })),
  ]
  return <div className="page logs-page">
    <PageHeader title="调用记录"
      actions={<button className="btn" disabled={busy} onClick={() => setRefresh(refresh + 1)}><RefreshCw size={13} />{t('刷新')}</button>} />
    <form className="card log-filters" onSubmit={event => {
      event.preventDefault(); setOffset(0); setFilters({ ...draft }); setRefresh(refresh + 1)
    }}>
      <Field label="开始时间"><DateTimePicker value={draft.start}
        max={draft.end || undefined} onChange={value => edit('start', value)} /></Field>
      <Field label="结束时间"><DateTimePicker value={draft.end}
        min={draft.start || undefined} onChange={value => edit('end', value)} /></Field>
      <Field label="日志类型"><Select value={draft.log_type} onChange={value => edit('log_type', value)} options={options(logLabels, '全部类型')} /></Field>
      <Field label="模型类型"><Select value={draft.task} onChange={value => edit('task', value)} options={options({ llm: 'LLM', embedding: 'Embedding', rerank: 'Rerank' }, '全部类型')} /></Field>
      <Field label="公开模型"><Select value={draft.model} onChange={value => edit('model', value)}
        options={[{ value: '', label: t('全部模型') }, ...[...new Set([...data!.models.map(m => m.id), ...items.map(item => item.model)])].map(id => ({ value: id, label: id }))]} /></Field>
      <Field label="状态"><Select value={draft.status} onChange={value => edit('status', value)} options={options(statusLabels, '全部状态')} /></Field>
      <div className="log-filter-actions"><span className="hint">{t('按当前浏览器时区筛选')}</span>
        <button type="button" className="btn" onClick={() => { setDraft(emptyFilters); setFilters(emptyFilters); setOffset(0) }}>{t('重置')}</button>
        <button className="btn btn-primary" disabled={busy}><Search size={13} />{t('查询')}</button></div>
    </form>
    <ErrorBar error={error} />
    <div className="card table-scroll"><table className="model-table log-table" aria-label={t('调用记录')}><thead><tr>
      {['时间', '公开模型', '上游提供商', '状态 / 耗时', 'Token 用量', '请求来源', '成本'].map(label => <th key={label}>{t(label)}</th>)}<th />
    </tr></thead><tbody>
      {items.map(item => <tr className="clickable" key={`${item.request_id}-${item.attempt_no}`} tabIndex={0}
        aria-label={`${t('查看调用详情')} ${item.model}`} onClick={() => setDetail(item)} onKeyDown={event => {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setDetail(item) }
        }}>
        <td className="small mono"><span>{new Date(item.started_at).toLocaleDateString()}</span>
          <div className="muted">{new Date(item.started_at).toLocaleTimeString()}</div></td>
        <td><div className="log-model mono" title={item.model}>{item.model}</div><span className="small muted">
          {t(logLabels[item.log_type] || '历史记录')} · {item.task || '—'} · #{item.attempt_no}</span></td>
        <td><div className="log-provider" title={item.upstream_name || item.upstream_id}>{item.upstream_name || data!.upstreams.find(u => u.id === item.upstream_id)?.name || item.upstream_id}</div>
          <div className="small muted mono log-model" title={item.upstream_model}>{item.upstream_model || item.deployment_id}</div></td>
        <td title={`${t('首字延迟')}: ${time(item.first_token_ms)}\n${t('总耗时')}: ${time(item.latency_ms)}\n${item.error_code || ''}`}>
          <span className={`tag ${item.status === 'completed' ? 'green' : item.status === 'running' ? 'amber' : 'red'}`}>{t(statusLabels[item.status] || item.status)}</span>
          <div className="small muted">{item.first_token_ms != null ? `${time(item.first_token_ms)} / ` : ''}{time(item.latency_ms)}</div></td>
        <td><div className="log-token-panel" title={`${t('输入')}: ${count(item.usage?.input_tokens)}\n${t('输出')}: ${count(item.usage?.output_tokens)}\n${t('缓存数量')}: ${count(item.usage?.cached_input_tokens)}\n${t('缓存命中率')}: ${cacheRatio(item.usage)}\n${t('计量来源')}: ${t(sourceLabels[item.source] || '未知')}${item.tokenizer ? ` · ${item.tokenizer}` : ''}`}>
          <span className="mono">{item.source === 'estimate' ? '≈ ' : ''}{count(item.usage?.total_tokens)}</span>
          <span className="small muted">↑ {count(item.usage?.input_tokens)} · ↓ {count(item.usage?.output_tokens)}</span>
          {item.usage?.cached_input_tokens != null && <span className="small muted">{t('缓存')} {cacheRatio(item.usage)}</span>}
        </div></td>
        <td><div className="log-source" title={`${source(item)}\n${item.key_id || ''}\n${item.credential_id || ''}`}>{source(item)}</div>
          <div className="small muted">{t(sourceLabels[item.source] || '未知')}</div></td>
        <td className="mono small" title={`${t('价格快照')}: ${JSON.stringify(item.price)}`}>{cost(item)}</td>
        <td><ChevronRight size={14} className="muted" /></td>
      </tr>)}
    </tbody></table>{!items.length && <Empty text={busy ? '正在加载' : '暂无调用记录'} />}</div>
    <div className="editor-actions"><button className="btn" disabled={!offset || busy} onClick={() => setOffset(Math.max(0, offset - 50))}>{t('上一页')}</button>
      <span className="small muted">{items.length ? `${offset + 1}–${offset + items.length}` : '0'}</span>
      <button className="btn" disabled={items.length < 50 || busy} onClick={() => setOffset(offset + 50)}>{t('下一页')}</button></div>
    {detail && <LogDetail key={`${detail.request_id}-${detail.attempt_no}`} item={detail} onClose={() => setDetail(null)} />}
  </div>
}

function LogDetail({ item, onClose }: { item: Entity; onClose: () => void }) {
  const [body, setBody] = useState<Entity | null>(null)
  const [error, setError] = useState('')
  const [tab, setTab] = useState('request')
  useEffect(() => {
    const controller = new AbortController()
    api(`/logs/${encodeURIComponent(item.request_id)}/${item.attempt_no}`, { signal: controller.signal })
      .then(setBody).catch(cause => { if (!controller.signal.aborted) setError(cause.message) })
    return () => controller.abort()
  }, [item.request_id, item.attempt_no])
  const metrics = [
    ['时间', new Date(item.started_at).toLocaleString()], ['公开模型', item.model],
    ['上游提供商', item.upstream_name || item.upstream_id], ['上游模型', item.upstream_model || '—'],
    ['状态', t(statusLabels[item.status] || item.status)], ['首字延迟', time(item.first_token_ms)], ['总耗时', time(item.latency_ms)],
    ['输入 token', count(item.usage?.input_tokens)], ['输出 token', count(item.usage?.output_tokens)],
    ['缓存数量', count(item.usage?.cached_input_tokens)], ['缓存命中率', cacheRatio(item.usage)],
    ['请求来源', source(item)], ['上游凭据', item.credential_id || '—'], ['成本', cost(item)],
    ['计量来源', [t(sourceLabels[item.source] || '未知'), item.tokenizer].filter(Boolean).join(' · ')],
  ]
  const values: Record<string, unknown> = {
    request: body?.detail?.request, response: body?.gateway_response || body?.detail?.response,
    upstream: body?.detail?.upstream_exchanges, metadata: item,
  }
  const truncated = body?.detail?.truncated === true
  const bodyTabs = truncated ? [['request', '正文预览'], ['metadata', '计量详情']]
    : [['request', '完整请求'], ['response', '完整返回'], ['upstream', '上游原始交互'], ['metadata', '计量详情']]
  return <Modal open onClose={onClose} title="调用详情" subtitle={`${item.request_id} · #${item.attempt_no}`} width={1080}>
    <ErrorBar error={error} />
    <div className="log-detail-metrics">{metrics.map(([label, value]) => <div key={label}><span>{t(label)}</span><strong>{value}</strong></div>)}</div>
    <div className="log-price"><span>{t('价格快照')} · / 1M token</span>
      <span>{t('输入')} {item.price.input_per_million ?? '—'} · {t('输出')} {item.price.output_per_million ?? '—'} · {t('缓存')} {item.price.cached_input_per_million ?? '—'} {item.price.currency}</span></div>
    <div className="provider-tabs log-detail-tabs" role="tablist" aria-label={t('调用正文')}>
      {bodyTabs.map(([id, label]) =>
        <button key={id} type="button" role="tab" aria-selected={tab === id} className={`provider-tab${tab === id ? ' active' : ''}`} onClick={() => setTab(id)}>{t(label)}</button>)}
    </div>
    {truncated && <p className="hint" role="status">{t('日志正文已截断，原始大小 {value0} 字节；以下为脱敏后的内容预览。', { value0: count(body.detail.original_bytes) })}</p>}
    {truncated && tab !== 'metadata'
      ? <pre className="config-preview log-body" role="tabpanel">{body.detail.body}</pre>
      : !body && !error ? <p className="hint">{t('正在加载')}</p> : values[tab] == null
      ? <p className="hint">{t('此记录未留存正文，或正文已按保留期清理。')}</p>
      : <pre className="config-preview log-body" role="tabpanel">{JSON.stringify(values[tab], null, 2)}</pre>}
  </Modal>
}
