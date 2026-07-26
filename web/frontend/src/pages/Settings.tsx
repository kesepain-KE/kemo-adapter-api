import { useEffect, useState } from 'react'
import { Check, CircleAlert, Power, RotateCw, Save, Shield, TerminalSquare } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import { Badge, Card, CardHeader, EmptyState, SectionTitle, Subtabs } from '../components/UI'

type Tab = 'runtime' | 'providers' | 'startup'
type ProviderDraft = { configText: string; apiKey: string }

function draftsFrom(configs: Record<string, Record<string, unknown>>, ids: string[]) {
  return Object.fromEntries(ids.map(id => [id, { configText: JSON.stringify(configs[id] ?? {}, null, 2), apiKey: '' }]))
}

export default function Settings() {
  const { data, restart, isOwner, saveRuntime, saveProvider, requestRestart } = useAdmin()
  const [tab, setTab] = useState<Tab>('runtime')
  const [gatewayEnabled, setGatewayEnabled] = useState(data.gateway_api.enabled !== false)
  const [prompt, setPrompt] = useState(data.highest_priority_system_prompt)
  const [disabledProviders, setDisabledProviders] = useState(data.disabled_providers)
  const [disabledModels, setDisabledModels] = useState(data.disabled_models)
  const [providerDrafts, setProviderDrafts] = useState<Record<string, ProviderDraft>>(() => draftsFrom(data.provider_configs, data.providers.map(provider => provider.provider_id)))
  const [busy, setBusy] = useState('')
  const [saved, setSaved] = useState('')
  const [error, setError] = useState('')
  const [restartReason, setRestartReason] = useState('configuration updated')
  const [forceRestart, setForceRestart] = useState(false)

  useEffect(() => {
    setGatewayEnabled(data.gateway_api.enabled !== false)
    setPrompt(data.highest_priority_system_prompt)
    setDisabledProviders(data.disabled_providers)
    setDisabledModels(data.disabled_models)
    setProviderDrafts(current => {
      const next = { ...current }
      for (const provider of data.providers) {
        if (!next[provider.provider_id]) next[provider.provider_id] = { configText: JSON.stringify(data.provider_configs[provider.provider_id] ?? {}, null, 2), apiKey: '' }
      }
      return next
    })
  }, [data.gateway_api.enabled, data.highest_priority_system_prompt, data.disabled_providers, data.disabled_models, data.provider_configs, data.providers])

  const flashSaved = (message: string) => {
    setSaved(message)
    window.setTimeout(() => setSaved(''), 1800)
  }

  const saveRuntimeSettings = async () => {
    setBusy('runtime'); setError('')
    try {
      await saveRuntime(gatewayEnabled, prompt, disabledProviders, disabledModels)
      flashSaved('运行时配置已保存')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败') }
    finally { setBusy('') }
  }

  const toggleProvider = (providerId: string) => {
    setDisabledProviders(current => current.includes(providerId) ? current.filter(value => value !== providerId) : [...current, providerId])
  }

  const updateDraft = (providerId: string, patch: Partial<ProviderDraft>) => {
    setProviderDrafts(current => ({ ...current, [providerId]: { ...(current[providerId] ?? { configText: '{}', apiKey: '' }), ...patch } }))
  }

  const saveProviderSettings = async (providerId: string) => {
    const draft = providerDrafts[providerId]
    if (!draft) return
    let config: Record<string, unknown>
    try {
      const parsed: unknown = JSON.parse(draft.configText)
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('配置必须是 JSON object')
      config = parsed as Record<string, unknown>
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Provider JSON 无效')
      return
    }
    setBusy(providerId); setError('')
    try {
      await saveProvider(providerId, config, draft.apiKey || undefined)
      updateDraft(providerId, { configText: JSON.stringify(config, null, 2), apiKey: '' })
      flashSaved(`${providerId} API 配置已保存`)
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Provider 配置保存失败') }
    finally { setBusy('') }
  }

  const startRestart = async () => {
    if (forceRestart && !window.confirm('强制重启可能中断仍在执行的请求，确定继续吗？')) return
    setBusy('restart'); setError('')
    try {
      await requestRestart(restartReason, forceRestart)
      flashSaved('重启请求已提交，页面将自动跟踪新实例')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法提交重启') }
    finally { setBusy('') }
  }

  const providerIds = data.providers.map(provider => provider.provider_id)
  const restartPhase = restart?.restart.phase ?? 'unavailable'
  return <>
    <Subtabs items={[{ id: 'runtime', label: '运行时控制' }, { id: 'providers', label: 'Provider API 配置' }, { id: 'startup', label: '启动与重启' }]} value={tab} onChange={setTab}/>
    <SectionTitle title="系统设置" description="蓝色配置无需重启；环境和源码变更必须平滑重启" eyebrow={`Revision ${data.revision}`} action={<>{saved && <Badge tone="success"><Check size={13}/>{saved}</Badge>}{tab === 'runtime' && <button className="button" disabled={busy === 'runtime'} onClick={() => void saveRuntimeSettings()}><Save size={15}/>保存运行时配置</button>}</>}/>
    {error && <div className="global-alert" role="alert"><span>{error}</span><button onClick={() => setError('')}>×</button></div>}
    {tab === 'runtime' && <div className="settings-grid">
      <Card><CardHeader title="对外模型 API" description="对下一次新请求即时生效" action={<Badge>无需重启</Badge>}/><label className="setting-switch"><span><b>接受新的 LLM API 请求</b><small>停用后已有 Response 仍可查询和取消</small></span><button className={`toggle ${gatewayEnabled ? 'on' : ''}`} onClick={() => setGatewayEnabled(!gatewayEnabled)}><i/></button></label></Card>
      <Card><CardHeader title="最高权限系统提示词" description="独立于 kemo-agent 的 system_prompt" action={<Badge>无需重启</Badge>}/><div className="form-field"><textarea value={prompt} onChange={event => setPrompt(event.target.value)} rows={8}/><small>仅影响配置生效后的新请求。</small></div></Card>
      <Card><CardHeader title="Provider 启停" description="提交完整禁用列表" action={<Badge>无需重启</Badge>}/>{providerIds.length ? <div className="control-list">{providerIds.map(id => { const enabled = !disabledProviders.includes(id); return <label key={id}><span><b>{id}</b><small>{data.providers.find(provider => provider.provider_id === id)?.models.length ?? 0} models</small></span><button className={`toggle ${enabled ? 'on' : ''}`} onClick={() => toggleProvider(id)}><i/></button></label> })}</div> : <p>没有已加载 Provider。</p>}</Card>
      <Card className="policy-card"><Shield size={24}/><div><h3>Revision 保护</h3><p>保存时携带当前 revision；发生 409 时不会覆盖其他操作者的新配置，请刷新后重新确认。</p></div></Card>
    </div>}
    {tab === 'providers' && (!providerIds.length ? <EmptyState title="没有已加载 Provider" description="新厂商目录需要部署代码并重启后才会出现。"/> : <div className="provider-settings-list">{providerIds.map(id => { const draft = providerDrafts[id] ?? { configText: '{}', apiKey: '' }; return <Card key={id}><CardHeader title={id} description="厂商自定义 JSON 配置" action={<Badge>无需重启</Badge>}/><div className="form-field"><label>config.json</label><textarea className="code-editor" rows={10} spellCheck={false} value={draft.configText} onChange={event => updateDraft(id, { configText: event.target.value })}/><small>保留不认识的厂商字段；敏感值不能放在此对象中。</small></div><div className="form-field"><label>API Key（只写）</label><input type="password" autoComplete="new-password" value={draft.apiKey} onChange={event => updateDraft(id, { apiKey: event.target.value })} placeholder="留空表示不轮换现有密钥"/><small>现有密钥不会从后端读回。</small></div><div className="card-actions"><button className="btn primary" disabled={busy === id} onClick={() => void saveProviderSettings(id)}><Save size={14}/>保存 {id} 配置</button></div></Card> })}</div>)}
    {tab === 'startup' && <div className="two-grid">
      <Card><CardHeader title="环境与部署配置" description="网页当前只展示边界，不在线编辑 .env" action={<Badge tone="warning">必须重启</Badge>}/><div className="info-list"><div>HOST / PORT / LOG_LEVEL <Badge tone="warning">重启</Badge></div><div>启动密钥和 Provider 启动配置 <Badge tone="warning">重启</Badge></div><div>Provider、Core、API、Web 源码 <Badge tone="warning">重启</Badge></div><div>依赖与协议模型 <Badge tone="warning">重启</Badge></div></div></Card>
      <Card className="restart-card"><TerminalSquare/><h3>平滑重启</h3>{isOwner ? <><p>当前状态：<Badge tone={restartPhase === 'failed' ? 'danger' : restartPhase === 'succeeded' || restartPhase === 'idle' ? 'success' : 'warning'}>{restartPhase}</Badge> · 活动执行 {restart?.gateway.active_executions ?? data.runtime.active_executions}</p><div className="form-field restart-form"><label>重启原因</label><input value={restartReason} onChange={event => setRestartReason(event.target.value)} maxLength={500}/></div><label className="force-check"><input type="checkbox" checked={forceRestart} onChange={event => setForceRestart(event.target.checked)}/><span>Drain 超时后强制继续（可能中断请求）</span></label><button className="button" disabled={busy === 'restart' || !['idle', 'succeeded', 'failed'].includes(restartPhase)} onClick={() => void startRestart()}><RotateCw size={15}/>提交平滑重启</button></> : <><p>当前密钥没有 owner scope。普通 admin:web 无权重启进程。</p><button className="button" disabled><Power size={15}/>需要 owner 权限</button></>}</Card>
      <Card className="warning-card"><CircleAlert/><div><h3>内存执行限制</h3><p>当前 InMemoryExecutionStore 不能跨进程恢复。默认 Drain 超时会撤销重启；只有显式强制才会继续。</p></div></Card>
    </div>}
  </>
}
