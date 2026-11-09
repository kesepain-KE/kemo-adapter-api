import { useEffect, useRef, useState } from 'react'
import { RefreshCw, Terminal } from 'lucide-react'
import { api, useAction, useConsole } from '../console'
import type { Entity } from '../console'
import { PageHeader, Toggle } from '../components/UI'
import { Empty, ErrorBar } from '../components/Form'
import { getLanguage, t } from '../i18n'
import { formatCost } from '../costs'

const number = (value: number | null) => value == null ? '—' : value.toLocaleString(getLanguage())
const rate = (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(1)}%`
const latency = (value: number | null) => value == null ? '—' : `${Math.round(value)} ms`
const periods = [['today', '今天'], ['7d', '近 7 天'], ['30d', '近 30 天'], ['all', '全部数据']]

export default function Diagnostics() {
  const { data, refresh } = useConsole()
  const [tab, setTab] = useState('usage')
  const [period, setPeriod] = useState('today')
  const [dimension, setDimension] = useState('provider')
  const [overview, setOverview] = useState<Entity | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [version, setVersion] = useState(0)
  const action = useAction()
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError(''); setOverview(null)
    api(`/diagnostics/usage?period=${period}`, { signal: controller.signal })
      .then(setOverview).catch(cause => { if (!controller.signal.aborted) setError(cause.message) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [period, version])
  const metrics = overview?.summary
  const rankingColumns = [
    '调用次数', '成功率', ...(dimension === 'provider' ? [] : ['Token 总量']),
    '缓存命中率', '平均首字延迟', ...(dimension === 'provider' ? [] : ['成本总计']),
  ]
  return <div className="page diagnostics-page"><PageHeader title="运行诊断"
    actions={<button className="btn" disabled={loading || action.busy} onClick={() => {
      setVersion(value => value + 1); void action.run(refresh)
    }}><RefreshCw size={14} />{t('刷新')}</button>} />
    <div className="page-tabs diagnostics-tabs" role="tablist" aria-label={t('诊断栏目')}>
      {[['usage', '调用数据'], ['logs', '日志']].map(([id, label]) => <button key={id} id={`diagnostics-${id}`}
        role="tab" aria-selected={tab === id} aria-controls={`diagnostics-content-${id}`} onClick={() => setTab(id)}>{t(label)}</button>)}
    </div>
    <ErrorBar error={action.error} />
    {tab === 'usage' ? <div id="diagnostics-content-usage" className="diagnostics-content" role="tabpanel" aria-labelledby="diagnostics-usage">
    <div className="diagnostics-toolbar"><div className="segmented" role="tablist" aria-label={t('数据范围')}>
      {periods.map(([value, label]) => <button key={value} role="tab" aria-selected={period === value} onClick={() => setPeriod(value)}>{t(label)}</button>)}
    </div><span className="hint">{overview ? `${t('统计时区')} · ${overview.timezone}` : t('正在加载')}</span></div>
    <ErrorBar error={error} />
    {metrics && <><div className="diagnostics-metrics">
      {[
        ['调用次数', number(metrics.calls), `${t('已完成')} ${number(metrics.finished)}`],
        ['调用成功率', rate(metrics.success_rate), `${t('成功')} ${number(metrics.successful)}`],
        ['Token 总量', number(metrics.total_tokens), t('仅汇总已知用量')],
        ['输入 Token', number(metrics.input_tokens), `${t('输出 Token')} ${number(metrics.output_tokens)}`],
        ['缓存命中率', rate(metrics.cache_hit_rate), t('按已知输入 Token 加权')],
        ['缓存命中 Token', number(metrics.cached_tokens), `${t('已知缓存用量')} ${number(metrics.cache_samples)}`],
        ['平均响应延迟', latency(metrics.latency_ms), `${t('平均首字延迟')} ${latency(metrics.first_token_ms)}`],
        ['成本总计', formatCost(metrics.display_cost), `${t('价格未知调用')} ${number(metrics.unknown_cost_calls)}`],
      ].map(([label, value, foot]) => <div className="stat-card" key={label}>
        <div className="stat-label">{t(label)}</div><div className={`stat-value${label === '成本总计' ? ' cost-value' : ''}`}
          title={value}>{value}</div><div className="stat-foot">{foot}</div>
      </div>)}
    </div>
      <section className="ranking-section"><div className="ranking-head"><h2 className="panel-title">{t('调用排行')}</h2>
        <div className="segmented" role="tablist" aria-label={t('排行维度')}>
          {[['provider', '提供商'], ['model', '模型'], ['key', '密钥']].map(([value, label]) =>
            <button key={value} role="tab" aria-selected={dimension === value} onClick={() => setDimension(value)}>{t(label)}</button>)}
        </div></div><div className="ranking-table-wrap">
        <table className={`ranking-table${dimension === 'provider' ? '' : ' extended'}`} aria-label={t('调用排行')}>
          <thead><tr><th scope="col">{t(dimension === 'provider' ? '提供商' : dimension === 'model' ? '模型' : '密钥')}</th>
            {rankingColumns.map(label => <th scope="col" key={label} className={label === '成本总计' ? 'ranking-cost' : undefined}>{t(label)}</th>)}
          </tr></thead><tbody>
          {overview.rankings[dimension].map((item: Entity, index: number) => <tr className="ranking-row" key={item.id}>
            <th scope="row"><div className="ranking-title"><span className="ranking-position">{index + 1}</span><div>
              <span className={`ranking-name${dimension === 'model' ? ' mono' : ''}`}
                title={item.name || t(item.id === '/unattributed' ? '未关联密钥' : '密钥')}>
                {item.name || t(item.id === '/unattributed' ? '未关联密钥' : '密钥')}</span>
            </div></div></th>
            {[
              number(item.calls), rate(item.success_rate),
              ...(dimension === 'provider' ? [] : [number(item.total_tokens)]),
              rate(item.cache_hit_rate), latency(item.first_token_ms),
              ...(dimension === 'provider' ? [] : [formatCost(item.display_cost)]),
            ].map((value, column) => <td key={rankingColumns[column]} title={value}
              className={rankingColumns[column] === '成本总计' ? 'ranking-cost' : undefined}>{value}</td>)}
          </tr>)}
          {!overview.rankings[dimension].length && <tr><td colSpan={rankingColumns.length + 1}><Empty text="暂无调用记录" /></td></tr>}
          </tbody>
        </table>
      </div></section>
    </>}
    {loading && <Empty text="正在加载" />}
    </div> : <div id="diagnostics-content-logs" className="diagnostics-content" role="tabpanel" aria-labelledby="diagnostics-logs">
    <TerminalLogs />
    <details className="card runtime-details"><summary>{t('运行时快照')}</summary><div className="panel-body">
      <div className="runtime-summary"><span>{t('运行状态')} <strong>{data!.runtime.phase}</strong></span>
        <span>{t('网关版本')} <strong>{data!.version.version}</strong></span><span>Kemo Protocol <strong>2.0</strong></span>
        <span>{t('排空中的旧快照')} <strong>{data!.retired_generations}</strong></span></div>
      <div className="list-row"><span>revision</span><code className="wrap-code">{data!.revision}</code></div>
      <pre className="config-preview">{JSON.stringify({ runtime: data!.runtime, cooldown: data!.health }, null, 2)}</pre>
    </div></details>
    </div>}
  </div>
}

function TerminalLogs() {
  const [snapshot, setSnapshot] = useState<Entity | null>(null)
  const [stream, setStream] = useState('all')
  const [follow, setFollow] = useState(true)
  const [error, setError] = useState('')
  const [version, setVersion] = useState(0)
  const viewport = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const controller = new AbortController()
    const load = () => api(`/system/terminal-logs?limit=300&stream=${stream}`, { signal: controller.signal })
      .then(value => { setSnapshot(value); setError('') }).catch(cause => {
        if (!controller.signal.aborted) setError(cause.message)
      })
    void load()
    const timer = window.setInterval(() => { if (!document.hidden) void load() }, 2000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [stream, version])
  useEffect(() => {
    if (follow && viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight
  }, [snapshot, follow])
  return <section className="terminal-section">
    <div className="diagnostics-toolbar"><div className="segmented" role="tablist" aria-label={t('终端日志')}>
      {[['all', '全部'], ['out', '标准输出'], ['err', '标准错误']].map(([value, label]) =>
        <button key={value} role="tab" aria-selected={stream === value} onClick={() => setStream(value)}>{t(label)}</button>)}
    </div><div className="row-actions"><span className="hint">{t('每 2 秒刷新')}</span>
      <button className="btn btn-sm" aria-label={t('刷新终端日志')} onClick={() => setVersion(value => value + 1)}><RefreshCw size={13} />{t('刷新')}</button></div></div>
    <ErrorBar error={error} />
    <div className="terminal-stats">{[
      ['采集状态', snapshot?.capturing ? t('实时接收') : t('未采集')],
      ['载入窗口', `${snapshot?.items.length ?? 0} / ${snapshot?.retained ?? 0}`],
      ['标准输出', number(snapshot?.stdout ?? 0)], ['标准错误', number(snapshot?.stderr ?? 0)],
    ].map(([label, value]) => <div className="stat-card" key={label}><div className="stat-label">{t(label)}</div><strong>{value}</strong></div>)}</div>
    <div className="card terminal-card"><div className="panel-head"><h2 className="panel-title"><Terminal size={16} />{t('网关启动终端')}</h2><span className="tag">{t('只读')}</span></div>
      <div ref={viewport} className="terminal-lines" role="log" aria-label={t('网关启动终端')} aria-live="off">
        {snapshot?.items.map((line: Entity) => <div key={line.id} className={`terminal-line ${line.stream}`}>
          <time>{new Date(line.at).toLocaleTimeString(getLanguage(), { hour12: false })}</time>
          <span className="terminal-stream">{line.stream.toUpperCase()}</span><code>{line.text || ' '}</code>
        </div>)}
        {!snapshot?.items.length && <Empty text={snapshot?.capturing ? '暂无终端输出' : '使用 start_web.py 启动后采集终端输出'} />}
      </div><div className="panel-foot terminal-footer"><Toggle checked={follow} onChange={setFollow} label={t('跟随最新输出')} />
        <span className="hint">{t('保留 3600 秒 · 仅当前进程')}</span></div>
    </div>
  </section>
}
