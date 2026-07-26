import { useEffect, useState } from 'react'
import { Check, Copy, Eye, EyeOff, KeyRound, LoaderCircle, RefreshCw } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import { adminApi, type GatewayApiKey } from '../adminApi'
import { Badge, Card, EmptyState, SectionTitle, Subtabs } from '../components/UI'

const numberFormat = new Intl.NumberFormat('zh-CN')

function formatDate(value: string | null): string {
  if (!value) return '未记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '未记录'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

function maskedToken(token: string): string {
  return `${'•'.repeat(16)}${token.slice(-6)}`
}

function keyIdentity(key: GatewayApiKey): string {
  return key.id ?? `${key.source}:${key.name}`
}

export default function Keys() {
  const { token } = useAdmin()
  const [keys, setKeys] = useState<GatewayApiKey[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [revealed, setRevealed] = useState<Set<string>>(new Set())
  const [copied, setCopied] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const [detailTab, setDetailTab] = useState<'info' | 'usage'>('info')
  const [refreshVersion, setRefreshVersion] = useState(0)
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => {
    let active = true
    if (refreshVersion === 0) setLoading(true)
    setError('')
    adminApi.gatewayApiKeys(token)
      .then(result => {
        if (!active) return
        setKeys(result.items)
        setSelectedId(current => result.items.some(key => keyIdentity(key) === current) ? current : (result.items[0] ? keyIdentity(result.items[0]) : ''))
      })
      .catch(reason => {
        if (active) setError(reason instanceof Error ? reason.message : 'API 密钥读取失败')
      })
      .finally(() => {
        if (!active) return
        setLoading(false)
        setRefreshing(false)
      })
    return () => { active = false }
  }, [refreshVersion, token])

  const toggleReveal = (name: string) => {
    setRevealed(current => {
      const next = new Set(current)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const copyToken = async (key: GatewayApiKey) => {
    try {
      await navigator.clipboard.writeText(key.token)
      const identity = keyIdentity(key)
      setCopied(identity)
      window.setTimeout(() => setCopied(current => current === identity ? '' : current), 1600)
    } catch {
      setError('浏览器拒绝访问剪贴板，请先使用查看按钮后手动复制。')
    }
  }

  const selectedKey = keys.find(key => keyIdentity(key) === selectedId) ?? keys[0]

  return <>
    <SectionTitle title="API 密钥管理" description="查看当前网关密钥、累计真实用量和最后调用时间" action={<button className={`btn provider-refresh ${refreshing ? 'is-refreshing' : ''}`} disabled={loading || refreshing} onClick={() => { setRefreshing(true); setRefreshVersion(value => value + 1) }}><RefreshCw className={refreshing ? 'spin' : ''} size={15}/>刷新</button>}/>
    {error && <div className="global-alert" role="alert"><span>{error}</span><button onClick={() => setError('')}>×</button></div>}
    {loading ? <Card className="key-loading"><LoaderCircle className="spin"/><span>正在读取 API 密钥…</span></Card> : !keys.length ? <EmptyState title="没有 API 密钥" description="当前启动环境和 api/keys.json 都没有配置网关 API 密钥。"/> : selectedKey && <div className="key-master-detail">
      <Card className="key-directory">
        <header className="key-directory-head"><div><span>密钥行列</span><strong>{keys.length} 个密钥</strong></div><KeyRound size={18}/></header>
        <div className="key-directory-list">{keys.map(key => { const identity = keyIdentity(key); return <button key={identity} className={keyIdentity(selectedKey) === identity ? 'active' : ''} onClick={() => setSelectedId(identity)}>
          <div className="key-row-head"><strong title={key.name}>{key.name}</strong><Badge tone={key.source === 'runtime' ? 'primary' : 'muted'}>{key.source === 'runtime' ? '热配置' : '环境变量'}</Badge></div>
          <div className="key-row-stats"><span><small>累计调用</small><b>{numberFormat.format(key.usage.calls)}</b></span><span><small>Token</small><b>{key.usage.total_tokens === null ? '—' : numberFormat.format(key.usage.total_tokens)}</b></span></div>
          <small className="key-row-last">最近调用 · {formatDate(key.last_used_at)}</small>
        </button> })}</div>
      </Card>
      <Card className="key-detail-panel">
        <header className="key-detail-head"><div><span>API 密钥</span><h3>{selectedKey.name}</h3><p>当前选中密钥的配置与真实累计调用数据</p></div><Badge tone={selectedKey.source === 'runtime' ? 'primary' : 'muted'}>{selectedKey.source === 'runtime' ? '热配置' : '环境变量'}</Badge></header>
        <Subtabs items={[{ id: 'info', label: '密钥信息' }, { id: 'usage', label: '调用情况' }]} value={detailTab} onChange={setDetailTab}/>
        {detailTab === 'info' ? <div className="key-detail-info">
          <div className="api-key-secret">
            <span>完整 API 密钥</span>
            <div>
              <code title={revealed.has(keyIdentity(selectedKey)) ? selectedKey.token : undefined}>{revealed.has(keyIdentity(selectedKey)) ? selectedKey.token : maskedToken(selectedKey.token)}</code>
              <button className="key-icon-button" onClick={() => toggleReveal(keyIdentity(selectedKey))} aria-label={`${revealed.has(keyIdentity(selectedKey)) ? '隐藏' : '查看'} ${selectedKey.name} 密钥`}>{revealed.has(keyIdentity(selectedKey)) ? <EyeOff size={15}/> : <Eye size={15}/>}</button>
              <button className={`key-icon-button ${copied === keyIdentity(selectedKey) ? 'copied' : ''}`} onClick={() => void copyToken(selectedKey)} aria-label={`复制 ${selectedKey.name} 密钥`}>{copied === keyIdentity(selectedKey) ? <Check size={15}/> : <Copy size={15}/>}</button>
            </div>
          </div>
          <div className="key-info-facts"><div><span>密钥来源</span><strong>{selectedKey.source === 'runtime' ? 'api/keys.json · 热加载' : '启动环境变量 · 重启生效'}</strong></div><div><span>创建日期</span><strong>{formatDate(selectedKey.created_at)}</strong></div><div><span>最近一次调用</span><strong>{formatDate(selectedKey.last_used_at)}</strong></div></div>
        </div> : <div className="key-usage-panel">
          <div className="key-usage-metrics"><div><span>累计调用</span><strong>{numberFormat.format(selectedKey.usage.calls)}</strong><small>全部日库真实请求</small></div><div><span>成功调用</span><strong>{numberFormat.format(selectedKey.usage.successes)}</strong><small>{selectedKey.usage.calls ? `${((selectedKey.usage.successes / selectedKey.usage.calls) * 100).toFixed(1)}% 成功率` : '尚无调用'}</small></div><div><span>失败及其他</span><strong>{numberFormat.format(Math.max(0, selectedKey.usage.calls - selectedKey.usage.successes))}</strong><small>失败、取消、未完成或在途</small></div><div><span>累计 Token</span><strong>{selectedKey.usage.total_tokens === null ? '—' : numberFormat.format(selectedKey.usage.total_tokens)}</strong><small>{selectedKey.usage.total_tokens === null ? '尚无可靠计量样本' : 'Provider 归一化计量'}</small></div></div>
          <div className="key-usage-summary"><span>最近一次调用</span><strong>{formatDate(selectedKey.last_used_at)}</strong><p>当前接口提供累计真实用量；逐次调用明细和按模型拆分尚未由后端暴露。</p></div>
        </div>}
      </Card>
    </div>}
  </>
}
