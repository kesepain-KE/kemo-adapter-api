import { useCallback, useEffect, useState } from 'react'
import { Code2, LockKeyhole, RefreshCw } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import { adminApi, type GatewayConfigView } from '../adminApi'
import { Badge, Card, CardHeader } from '../components/UI'
import { configDisplay, configLabel } from './gatewayConfigDisplay'

export default function GatewayConfig() {
  const { csrfToken } = useAdmin()
  const [config, setConfig] = useState<GatewayConfigView | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showNames, setShowNames] = useState(false)
  const load = useCallback(async (signal: { cancelled: boolean }) => {
    setLoading(true)
    setError('')
    try {
      const next = await adminApi.gatewayConfig(csrfToken)
      if (!signal.cancelled) setConfig(next)
    } catch (reason) {
      if (!signal.cancelled) setError(reason instanceof Error ? reason.message : '读取网关配置失败')
    } finally {
      if (!signal.cancelled) setLoading(false)
    }
  }, [csrfToken])
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    const signal = { cancelled: false }
    void load(signal)
    return () => { signal.cancelled = true }
  }, [load, refresh])

  const groups = config?.groups.filter(group => !group.items.every(item => item.sensitive)) ?? []
  const privateGroups = config?.groups.filter(group => group.items.every(item => item.sensitive)) ?? []
  const groupTitles: Record<string, string> = { 'Web 认证与访问': '访问安全', '执行与流式传输': '执行与流式', '请求与媒体资源': '请求与媒体', '敏感配置': '认证与密钥' }
  const renderGroup = (group: GatewayConfigView['groups'][number], privateGroup = false) => <Card key={group.title} className={`gateway-config-card${privateGroup ? ' gateway-config-private' : ''}`}>
    <CardHeader title={groupTitles[group.title] ?? group.title} action={privateGroup ? <span className="gateway-config-private-note"><LockKeyhole size={12}/>仅显示掩码</span> : undefined}/>
    <dl className="gateway-config-values">{group.items.map(item => {
      const display = configDisplay(item)
      return <div key={item.name}>
        <dt><span>{configLabel(item)}</span>{showNames && <small>{item.name}</small>}</dt>
        <dd className={display.tone ? `config-value-${display.tone}` : undefined}><span>{display.value}</span>{display.unit && <small>{display.unit}</small>}</dd>
      </div>
    })}</dl>
  </Card>

  return <div className="gateway-config-panel">
    <Card className="gateway-config-summary">
      <div className="gateway-config-intro">
        <div className="gateway-config-summary-title"><h3>当前配置</h3><Badge>只读</Badge></div>
        <p>修改 .env 后重启生效，密钥仅显示掩码。</p>
      </div>
      <div className="gateway-config-actions">
        <button className={`btn${showNames ? ' primary' : ''}`} aria-pressed={showNames} onClick={() => setShowNames(value => !value)}><Code2 size={14}/>{showNames ? '隐藏变量名' : '显示变量名'}</button>
        <button className="btn" disabled={loading} onClick={() => setRefresh(value => value + 1)}><RefreshCw size={14} className={loading ? 'spin' : ''}/>{loading ? '读取中' : '刷新'}</button>
      </div>
    </Card>
    {error && <div className="global-alert" role="alert">{error}</div>}
    {loading && !config && <Card><p role="status">正在读取网关配置…</p></Card>}
    {config && <>
      <div className="gateway-config-grid">{[0, 1].map(column => <div className="gateway-config-column" key={column}>{groups.filter((_, index) => index % 2 === column).map(group => renderGroup(group))}</div>)}</div>
      {privateGroups.map(group => renderGroup(group, true))}
    </>}
  </div>
}
