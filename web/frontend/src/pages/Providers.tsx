import { useState } from 'react'
import { KeyRound, Power, RefreshCw, Settings2 } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import { Badge, Card, CardHeader, EmptyState, SectionTitle } from '../components/UI'

export default function Providers({ onSettings }: { onSettings: () => void }) {
  const { data, refreshing, refresh, saveControl } = useAdmin()
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
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
    <SectionTitle title="已加载 Provider" description="只展示当前进程实际发现的厂商包；API 配置可热更新，代码变更需要重启" eyebrow={`${data.providers.length} packages`} action={<><button className="btn" disabled={refreshing} onClick={() => void refresh()}><RefreshCw className={refreshing ? 'spin' : ''} size={15}/>刷新</button><button className="button" onClick={onSettings}><Settings2 size={15}/>配置与重启</button></>}/>
    {message && <div className="global-alert"><span>{message}</span><button onClick={() => setMessage('')}>×</button></div>}
    {!data.providers.length ? <EmptyState title="没有已加载的 Provider" description="请在 providers 下部署厂商包并通过系统设置执行平滑重启。"/> : <div className="provider-grid">{data.providers.map(provider => {
      const id = provider.provider_id
      const disabled = data.disabled_providers.includes(id)
      const config = data.provider_configs[id] ?? {}
      const endpoint = typeof config.base_url === 'string' ? config.base_url : '未公开 Base URL'
      return <Card key={id} className={`provider-card status-${disabled ? 'disabled' : 'healthy'}`}>
        <CardHeader title={id} description={endpoint} action={<Badge tone={disabled ? 'muted' : 'success'}>{disabled ? '已禁用' : '已加载'}</Badge>}/>
        <div className="provider-identity"><div className="provider-id"><span>Provider ID</span><strong>{id}</strong></div><Badge>{provider.models.length} models</Badge></div>
        <div className="tags">{provider.models.map(model => { const prefix = `${id}-`; return <span key={model}>{model.startsWith(prefix) ? model.slice(prefix.length) : model}</span> })}</div>
        <div className="provider-stats provider-stats-single"><div><span>注册模型</span><strong>{provider.models.length}</strong></div></div>
        <div className="card-actions"><button className="btn primary" onClick={onSettings}><KeyRound size={14}/>API 配置</button><button className={`btn ${disabled ? '' : 'danger'}`} disabled={busy === id} onClick={() => void toggle(id)}><Power size={14}/>{disabled ? '启用' : '禁用'}</button></div>
      </Card>
    })}</div>}
  </>
}
