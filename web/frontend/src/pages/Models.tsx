import { useMemo, useState } from 'react'
import { Power, Search } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import { Badge, Card, EmptyState, SectionTitle, Subtabs } from '../components/UI'

export default function Models() {
  const { data, saveControl } = useAdmin()
  const [tab, setTab] = useState<'all' | 'enabled' | 'disabled'>('all')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const models = useMemo(() => data.providers.flatMap(provider => provider.models.map(id => ({ id, providerId: provider.provider_id }))), [data.providers])
  const state = (model: { id: string; providerId: string }) => ({
    providerDisabled: data.disabled_providers.includes(model.providerId),
    modelDisabled: data.disabled_models.includes(model.id),
  })
  const visible = models.filter(model => {
    const disabled = state(model).providerDisabled || state(model).modelDisabled
    return (tab === 'all' || (tab === 'enabled' ? !disabled : disabled)) && model.id.toLowerCase().includes(query.toLowerCase())
  })
  const toggle = async (modelId: string) => {
    const disabledModels = data.disabled_models.includes(modelId) ? data.disabled_models.filter(value => value !== modelId) : [...data.disabled_models, modelId]
    setBusy(modelId); setError('')
    try { await saveControl(data.highest_priority_system_prompt, data.disabled_providers, disabledModels) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败') }
    finally { setBusy('') }
  }
  return <>
    <Subtabs items={[{ id: 'all', label: '全部模型' }, { id: 'enabled', label: '可用模型' }, { id: 'disabled', label: '不可用模型' }]} value={tab} onChange={setTab}/>
    <SectionTitle title="真实模型注册表" description="模型启停立即影响后续新请求，不会取消在途响应" eyebrow={`${models.length} registered`}/>
    {error && <div className="global-alert"><span>{error}</span><button onClick={() => setError('')}>×</button></div>}
    <div className="searchbar"><Search size={17}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索完整模型 ID…"/><span>{visible.length} 个结果</span></div>
    {!visible.length ? <EmptyState title="没有匹配的模型" description="模型来自已加载 Provider 的 diagnostics()。"/> : <div className="model-grid">{visible.map(model => {
      const { providerDisabled, modelDisabled } = state(model)
      const enabled = !providerDisabled && !modelDisabled
      return <Card key={model.id} className={!enabled ? 'model-disabled' : ''}><div className="model-head"><div className="model-icon">{model.providerId.slice(0, 2).toUpperCase()}</div><div><strong>{model.id.split('/').slice(1).join('/')}</strong><span>{model.providerId}</span></div><button className={`toggle ${enabled ? 'on' : ''}`} disabled={providerDisabled || busy === model.id} onClick={() => void toggle(model.id)} aria-label={`${enabled ? '禁用' : '启用'} ${model.id}`}><i/></button></div><div className="tags"><span>{model.id}</span></div><div className="provider-stats"><div><span>Provider</span><strong>{providerDisabled ? '已禁用' : '已启用'}</strong></div><div><span>模型策略</span><strong>{modelDisabled ? '已禁用' : '已启用'}</strong></div></div><div className="card-actions"><button className={`btn ${enabled ? 'danger' : 'primary'}`} disabled={providerDisabled || busy === model.id} onClick={() => void toggle(model.id)}><Power size={14}/>{providerDisabled ? '先启用 Provider' : enabled ? '禁用模型' : '启用模型'}</button></div></Card>
    })}</div>}
  </>
}
