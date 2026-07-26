import { Activity, Boxes, CircleAlert, Clock3, DatabaseZap, ShieldCheck, Sparkles } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import { Badge, Card, CardHeader, SectionTitle } from '../components/UI'

export default function Dashboard() {
  const { data, restart, isOwner } = useAdmin()
  const providers = data.providers
  const models = providers.flatMap(provider => provider.models ?? [])
  const disabledProviders = new Set(data.disabled_providers)
  const disabledModels = new Set(data.disabled_models)
  const enabledModels = models.filter(model => !disabledProviders.has(model.split('/', 1)[0]) && !disabledModels.has(model))
  const runtime = restart?.gateway ?? data.runtime
  const startedAt = new Date(runtime.started_at)
  return <>
    <div className="hero-strip"><div className="hero-logo">KG</div><div><strong>Kemo Unified Provider Gateway</strong><span>当前实例 {runtime.instance_id}</span></div><div className="hero-tags"><span>{runtime.phase.toUpperCase()}</span><span>REVISION</span><span>{isOwner ? 'OWNER' : 'ADMIN'}</span></div><b>KEMO</b></div>
    <div className="metrics-grid">
      <Card className="metric"><span>已加载 Provider</span><strong>{providers.length}</strong><em>{data.disabled_providers.length} 个已禁用</em></Card>
      <Card className="metric"><span>注册模型</span><strong>{models.length}</strong><em>{enabledModels.length} 个可接收新请求</em></Card>
      <Card className="metric"><span>活动执行</span><strong>{runtime.active_executions}</strong><em>Drain 会等待该值归零</em></Card>
      <Card className="metric"><span>对外模型 API</span><strong>{data.gateway_api.enabled === false ? '已停用' : '已启用'}</strong><em>配置立即影响后续请求</em></Card>
    </div>
    {data.live_config_error && <div className="alert"><CircleAlert size={18}/><span>运行时配置加载失败，网关正在使用最后一个有效版本：{data.live_config_error}</span></div>}
    <SectionTitle title="当前网关实例" description="以下内容直接来自管理 API，不包含估算的调用量或健康率" eyebrow={data.revision}/>
    <div className="overview-grid">
      <Card><CardHeader title="Provider 注册结果" description="进程启动时发现的真实厂商包" action={<Badge>{providers.length} loaded</Badge>}/>{providers.length ? <div className="rank-list">{providers.map(provider => { const disabled = disabledProviders.has(provider.provider_id); return <div key={provider.provider_id}><span><b>{provider.provider_id}</b><small>{provider.models.length} 个模型</small></span><Badge tone={disabled ? 'muted' : 'success'}>{disabled ? '已禁用' : '已加载'}</Badge></div> })}</div> : <p>当前没有加载任何 Provider。新增厂商目录后需要重启网关。</p>}</Card>
      <Card><CardHeader title="进程状态" description="用于平滑重启与 Drain" action={<Badge tone={runtime.phase === 'running' ? 'success' : 'warning'}>{runtime.phase}</Badge>}/><div className="info-list"><div><span><Activity size={15}/> 活动执行</span><strong>{runtime.active_executions}</strong></div><div><span><Clock3 size={15}/> 启动时间</span><strong>{Number.isNaN(startedAt.getTime()) ? '未知' : startedAt.toLocaleString()}</strong></div><div><span><ShieldCheck size={15}/> 管理权限</span><strong>{isOwner ? 'owner' : 'admin:web'}</strong></div><div><span><DatabaseZap size={15}/> 配置版本</span><code>{data.revision.slice(0, 22)}</code></div></div></Card>
    </div>
    <SectionTitle title="已注册模型" description="模型列表来自每个 Provider 的 diagnostics()" eyebrow={`${enabledModels.length} enabled`}/>
    <Card>{models.length ? <div className="route-list">{models.map(model => { const providerId = model.split('/', 1)[0]; const disabled = disabledProviders.has(providerId) || disabledModels.has(model); return <div key={model}><Sparkles size={15}/><span>{model}</span><Badge tone={disabled ? 'muted' : 'success'}>{disabled ? '不可接收新请求' : '可用'}</Badge></div> })}</div> : <p>没有注册模型。</p>}</Card>
    <div className="capability-row"><div><Boxes/>独立 Provider</div><div><ShieldCheck/>管理鉴权</div><div><DatabaseZap/>原子配置</div><div><Activity/>Drain 状态</div></div>
  </>
}
