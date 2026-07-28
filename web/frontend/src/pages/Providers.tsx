import { useEffect, useMemo, useState } from 'react'
import { Boxes, KeyRound, Power, RefreshCw } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import {
  adminApi,
  type ModelCapabilityDeclaration,
  type ProviderCapabilities,
  type StatisticsRanking,
} from '../adminApi'
import { Badge, Card, CardHeader, EmptyState, SectionTitle, Subtabs } from '../components/UI'

type DetailTab = 'overview' | 'capabilities' | 'ranking'

function integer(value: number | null | undefined): string {
  return value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value)
}

function percent(value: number | null | undefined): string {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

function shortModel(providerId: string, model: string): string {
  const prefix = `${providerId}-`
  return model.startsWith(prefix) ? model.slice(prefix.length) : model
}

function capabilityLabels(models: ModelCapabilityDeclaration[]): string[] {
  const labels = new Set<string>()
  for (const model of models) {
    labels.add(model.task === 'llm' ? '大语言模型' : model.task === 'embedding' ? '向量化' : '重排序')
    if (model.streaming) labels.add('流式输出')
    if (model.reasoning.supported) labels.add('推理')
    if (model.reasoning.summary) labels.add('推理摘要')
    if (model.tools.function_calling) labels.add('工具调用')
    if (model.tools.parallel_calls) labels.add('并行工具')
    if (model.structured_output) labels.add('结构化输出')
    model.input_modalities.forEach(value => labels.add(`输入:${value}`))
    model.output_modalities.forEach(value => labels.add(`输出:${value}`))
  }
  return [...labels]
}

function modelCapabilityLabels(model: ModelCapabilityDeclaration): string[] {
  return capabilityLabels([model])
}

function limitText(model: ModelCapabilityDeclaration): string | null {
  const limits = model.extensions.limits
  if (!limits || typeof limits !== 'object' || Array.isArray(limits)) return null
  const values = limits as Record<string, unknown>
  const input = typeof values.max_input_tokens === 'number' ? integer(values.max_input_tokens) : null
  const output = typeof values.max_output_tokens === 'number' ? integer(values.max_output_tokens) : null
  if (!input && !output) return null
  return `${input ? `最大输入 ${input}` : ''}${input && output ? ' · ' : ''}${output ? `最大输出 ${output}` : ''}`
}

export default function Providers({ onSettings }: { onSettings: () => void }) {
  const { csrfToken: token, data, refreshing, refresh, saveControl } = useAdmin()
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [selectedId, setSelectedId] = useState(() => data.providers[0]?.provider_id ?? '')
  const [tab, setTab] = useState<DetailTab>('overview')
  const [capabilities, setCapabilities] = useState<Record<string, ProviderCapabilities>>({})
  const [providerRanking, setProviderRanking] = useState<StatisticsRanking | null>(null)
  const [modelRanking, setModelRanking] = useState<StatisticsRanking | null>(null)
  const [detailError, setDetailError] = useState('')
  const [detailRevision, setDetailRevision] = useState(0)
  const [detailLoading, setDetailLoading] = useState(false)
  const providerKey = data.providers.map(provider => provider.provider_id).sort().join('|')

  useEffect(() => {
    if (!data.providers.some(provider => provider.provider_id === selectedId)) {
      setSelectedId(data.providers[0]?.provider_id ?? '')
    }
  }, [data.providers, selectedId])

  useEffect(() => {
    let active = true
    const load = async () => {
      setDetailLoading(true)
      setDetailError('')
      try {
        const [providers, models, declarations] = await Promise.all([
          adminApi.statisticsRanking(token, undefined, 'provider'),
          adminApi.statisticsRanking(token, undefined, 'model'),
          Promise.allSettled(data.providers.map(provider => adminApi.providerCapabilities(token, provider.provider_id))),
        ])
        if (!active) return
        setProviderRanking(providers)
        setModelRanking(models)
        const next: Record<string, ProviderCapabilities> = {}
        declarations.forEach(result => {
          if (result.status === 'fulfilled') next[result.value.provider_id] = result.value
        })
        setCapabilities(next)
        if (declarations.some(result => result.status === 'rejected')) {
          setDetailError('部分 Provider 的能力声明读取失败；失败项未显示推测能力。')
        }
      } catch (reason) {
        if (!active) return
        setProviderRanking(null)
        setModelRanking(null)
        setDetailError(reason instanceof Error ? reason.message : '厂商详情读取失败')
      } finally {
        if (active) setDetailLoading(false)
      }
    }
    void load()
    return () => { active = false }
    // providerKey 只在实际厂商集合变化时触发，避免控制台轮询造成重复能力查询。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailRevision, providerKey, token])

  const selected = data.providers.find(provider => provider.provider_id === selectedId) ?? null
  const selectedCapabilities = selected ? capabilities[selected.provider_id] : undefined
  const totalCapabilities = useMemo(
    () => capabilityLabels(selectedCapabilities?.models ?? []),
    [selectedCapabilities],
  )
  const selectedStats = providerRanking?.items.find(item => item.id === selectedId)
  const enabledModels = selected
    ? selected.models.filter(model => !data.disabled_providers.includes(selected.provider_id) && !data.disabled_models.includes(model))
    : []
  const selectedModelIds = new Set(selected?.models ?? [])
  const selectedModelRanking = (modelRanking?.items ?? [])
    .filter(item => selectedModelIds.has(item.id))
    .sort((left, right) => (right.tokens.total_tokens ?? -1) - (left.tokens.total_tokens ?? -1))

  const toggle = async (providerId: string) => {
    const disabled = data.disabled_providers.includes(providerId)
      ? data.disabled_providers.filter(value => value !== providerId)
      : [...data.disabled_providers, providerId]
    setBusy(providerId)
    setMessage('')
    try {
      await saveControl(data.highest_priority_system_prompt, disabled, data.disabled_models)
      setMessage(`${providerId} 已${disabled.includes(providerId) ? '禁用' : '启用'}，只影响后续新请求。`)
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '保存失败')
    } finally { setBusy('') }
  }

  return <>
    <SectionTitle title="已加载 Provider" description="左侧选择厂商，右侧查看真实能力声明与当日调用统计" action={<button className={`btn provider-refresh ${refreshing || detailLoading ? 'is-refreshing' : ''}`} disabled={refreshing || detailLoading} onClick={() => { void refresh(); setDetailRevision(value => value + 1) }}><RefreshCw className={refreshing || detailLoading ? 'spin' : ''} size={15}/>{refreshing || detailLoading ? '刷新中' : '刷新'}</button>}/>
    {message && <div className="global-alert"><span>{message}</span><button onClick={() => setMessage('')}>×</button></div>}
    {detailError && <div className="alert"><span>{detailError}</span></div>}
    {!data.providers.length ? <EmptyState title="没有已加载的 Provider" description="请在 providers 下部署厂商包并通过系统设置执行平滑重启。"/> : <div className="provider-master-detail">
      <Card className="provider-directory">
        <CardHeader title="厂商行列" description="选择一个 Provider 查看详情" action={<Boxes size={17}/>}/>
        <div className="provider-directory-list">{data.providers.map(provider => {
          const disabled = data.disabled_providers.includes(provider.provider_id)
          const labels = capabilityLabels(capabilities[provider.provider_id]?.models ?? [])
          return <button key={provider.provider_id} className={selectedId === provider.provider_id ? 'active' : ''} onClick={() => { setSelectedId(provider.provider_id); setTab('overview') }}>
            <span className="provider-row-head"><strong>{provider.provider_id}</strong><Badge tone={disabled ? 'muted' : 'success'}>{disabled ? '已禁用' : '已加载'}</Badge></span>
            <span className="provider-row-bubbles"><span><small>注册模型</small><b>{provider.models.length}</b></span><span><small>能力声明</small><b>{capabilities[provider.provider_id] ? labels.length : '—'}</b></span></span>
          </button>
        })}</div>
      </Card>

      {selected && <Card className="provider-detail-panel">
        <div className="provider-detail-head">
          <div><span>Provider ID</span><h3>{selected.provider_id}</h3><p>{typeof data.provider_configs[selected.provider_id]?.base_url === 'string' ? String(data.provider_configs[selected.provider_id].base_url) : '未公开 Base URL'}</p></div>
          <div className="card-actions"><button className="btn primary" onClick={onSettings}><KeyRound size={14}/>API 配置</button><button className={`btn ${data.disabled_providers.includes(selected.provider_id) ? '' : 'danger'}`} disabled={busy === selected.provider_id} onClick={() => void toggle(selected.provider_id)}><Power size={14}/>{data.disabled_providers.includes(selected.provider_id) ? '启用' : '禁用'}</button></div>
        </div>
        <Subtabs<DetailTab> items={[{ id: 'overview', label: '运行概览' }, { id: 'capabilities', label: '模型能力' }, { id: 'ranking', label: '模型排行' }]} value={tab} onChange={setTab}/>

        {tab === 'overview' && <>
          <div className="provider-detail-metrics">
            <div><span>实际启用模型</span><strong>{enabledModels.length}</strong><small>共注册 {selected.models.length} 个</small></div>
            <div><span>Token 调用量</span><strong>{integer(selectedStats?.tokens.total_tokens)}</strong><small>{selectedStats?.token_coverage.total_tokens ?? 0} 个计量样本</small></div>
            <div><span>Token 命中率</span><strong>{percent(selectedStats?.cache_hit_rate)}</strong><small>{selectedStats?.cache_eligible_samples ?? 0} 个精确样本</small></div>
            <div><span>缓存 Token</span><strong>{integer(selectedStats?.tokens.cached_input_tokens)}</strong><small>Provider 归一化计量</small></div>
          </div>
          <div className="provider-detail-section"><h4>实际启用的模型</h4>{enabledModels.length ? <div className="tags">{enabledModels.map(model => <span key={model}>{shortModel(selected.provider_id, model)}</span>)}</div> : <p>当前厂商没有可接收新请求的模型。</p>}</div>
          <div className="provider-detail-section"><h4>厂商总能力声明 <Badge>{totalCapabilities.length}</Badge></h4>{totalCapabilities.length ? <div className="tags capability-tags">{totalCapabilities.map(value => <span key={value}>{value}</span>)}</div> : <p>Provider 尚未返回可展示的能力声明。</p>}</div>
        </>}

        {tab === 'capabilities' && <div className="capability-model-list">
          {selectedCapabilities?.models.map(model => {
            const disabled = data.disabled_providers.includes(selected.provider_id) || data.disabled_models.includes(model.model)
            const description = typeof model.metadata.description === 'string' ? model.metadata.description : 'Provider 未提供模型说明'
            return <div key={model.model} className="capability-model-card"><div className="capability-model-head"><div><strong>{shortModel(selected.provider_id, model.model)}</strong><p>{description}</p></div><Badge tone={disabled ? 'muted' : 'success'}>{disabled ? '已禁用' : '已启用'}</Badge></div><div className="tags capability-tags">{modelCapabilityLabels(model).map(value => <span key={value}>{value}</span>)}</div>{limitText(model) && <small>{limitText(model)}</small>}</div>
          })}
          {!selectedCapabilities?.models.length && <EmptyState title="没有能力声明" description="该 Provider 没有返回可验证的模型能力。"/>}
          {!!selectedCapabilities?.errors.length && <div className="alert"><span>{selectedCapabilities.errors.map(item => item.model).join('、')} 的能力声明读取失败。</span></div>}
        </div>}

        {tab === 'ranking' && <>{selectedModelRanking.length ? <div className="table-card provider-ranking-table"><table><thead><tr><th>模型</th><th>调用</th><th>Token</th><th>缓存 Token</th><th>命中率</th><th>成功率</th></tr></thead><tbody>{selectedModelRanking.map(item => <tr key={item.id}><td><code>{shortModel(selected.provider_id, item.id)}</code></td><td>{item.calls}</td><td>{integer(item.tokens.total_tokens)}</td><td>{integer(item.tokens.cached_input_tokens)}</td><td>{percent(item.cache_hit_rate)}</td><td>{percent(item.success_rate)}</td></tr>)}</tbody></table></div> : <EmptyState title="暂无模型排行" description="今天尚未记录该厂商模型的真实 Token 调用。"/>}</>}
      </Card>}
    </div>}
  </>
}
