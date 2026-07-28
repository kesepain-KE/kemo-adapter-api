import { useEffect, useState } from 'react'
import { KeyRound, LoaderCircle, RefreshCw, ShieldCheck, ShieldOff } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import { adminApi, type GatewayApiKey, type GatewayKeyModel } from '../adminApi'
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

function keyIdentity(key: GatewayApiKey): string {
  return key.id ?? `${key.source}:${key.name}`
}

export default function Keys() {
  const { csrfToken: token } = useAdmin()
  const [keys, setKeys] = useState<GatewayApiKey[]>([])
  const [models, setModels] = useState<GatewayKeyModel[]>([])
  const [revision, setRevision] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const [detailTab, setDetailTab] = useState<'info' | 'usage' | 'models'>('info')
  const [refreshVersion, setRefreshVersion] = useState(0)
  const [refreshing, setRefreshing] = useState(false)
  const [policyBusy, setPolicyBusy] = useState(false)

  useEffect(() => {
    let active = true
    if (refreshVersion === 0) setLoading(true)
    setError('')
    adminApi.gatewayApiKeys(token)
      .then(result => {
        if (!active) return
        setKeys(result.items)
        setModels(result.models)
        setRevision(result.revision)
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

  const selectedKey = keys.find(key => keyIdentity(key) === selectedId) ?? keys[0]
  const allowedRegisteredCount = selectedKey?.allowed_models?.filter(
    id => models.some(model => model.id === id),
  ).length ?? 0

  const updateModelPolicy = async (allowedModels: string[] | null) => {
    if (!selectedKey?.id || !selectedKey.writable || policyBusy) return
    setPolicyBusy(true)
    setError('')
    try {
      const result = await adminApi.updateKeyModelPolicy(
        token,
        selectedKey.id,
        revision,
        allowedModels,
      )
      setRevision(result.revision)
      setKeys(current => current.map(key => keyIdentity(key) === keyIdentity(selectedKey) ? {
        ...key,
        allowed_models: result.allowed_models,
        model_policy: result.model_policy,
      } : key))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '模型白名单保存失败')
    } finally {
      setPolicyBusy(false)
    }
  }

  const toggleModel = (modelId: string) => {
    if (!selectedKey) return
    const current = selectedKey.allowed_models
    if (current === null) {
      void updateModelPolicy(models.map(model => model.id).filter(id => id !== modelId))
      return
    }
    const registered = new Set(models.map(model => model.id))
    const currentRegistered = current.filter(id => registered.has(id))
    const next = currentRegistered.includes(modelId)
      ? currentRegistered.filter(id => id !== modelId)
      : [...currentRegistered, modelId]
    void updateModelPolicy(next)
  }

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
        <Subtabs items={[{ id: 'info', label: '密钥信息' }, { id: 'usage', label: '调用情况' }, { id: 'models', label: '模型限制' }]} value={detailTab} onChange={setDetailTab}/>
        {detailTab === 'info' ? <div className="key-detail-info">
          <div className="api-key-secret">
            <span>API 密钥（安全掩码）</span>
            <div>
              <code>{selectedKey.masked_token}</code>
            </div>
            <small>完整密钥不再返回浏览器；请在创建时保存，或在服务器端安全轮换。</small>
          </div>
          <div className="key-info-facts"><div><span>密钥来源</span><strong>{selectedKey.source === 'runtime' ? 'api/keys.json · 热加载' : '启动环境变量 · 重启生效'}</strong></div><div><span>创建日期</span><strong>{formatDate(selectedKey.created_at)}</strong></div><div><span>最近一次调用</span><strong>{formatDate(selectedKey.last_used_at)}</strong></div></div>
        </div> : detailTab === 'usage' ? <div className="key-usage-panel">
          <div className="key-usage-metrics"><div><span>累计调用</span><strong>{numberFormat.format(selectedKey.usage.calls)}</strong><small>全部日库真实请求</small></div><div><span>成功调用</span><strong>{numberFormat.format(selectedKey.usage.successes)}</strong><small>{selectedKey.usage.calls ? `${((selectedKey.usage.successes / selectedKey.usage.calls) * 100).toFixed(1)}% 成功率` : '尚无调用'}</small></div><div><span>失败及其他</span><strong>{numberFormat.format(Math.max(0, selectedKey.usage.calls - selectedKey.usage.successes))}</strong><small>失败、取消、未完成或在途</small></div><div><span>累计 Token</span><strong>{selectedKey.usage.total_tokens === null ? '—' : numberFormat.format(selectedKey.usage.total_tokens)}</strong><small>{selectedKey.usage.total_tokens === null ? '尚无可靠计量样本' : 'Provider 归一化计量'}</small></div></div>
          <div className="key-usage-summary"><span>最近一次调用</span><strong>{formatDate(selectedKey.last_used_at)}</strong><p>当前接口提供累计真实用量；逐次调用明细和按模型拆分尚未由后端暴露。</p></div>
        </div> : <div className="key-model-policy">
          <header className="key-model-policy-head">
            <div><span>模型调用白名单</span><strong>{selectedKey.allowed_models === null ? '全部模型允许' : allowedRegisteredCount ? `允许 ${allowedRegisteredCount} 个模型` : '全部模型禁止'}</strong><p>设置即时影响此密钥发起的后续请求；白名单使用网关对外完整模型名。</p></div>
            <div className="key-model-policy-actions">
              <button className={`btn ${selectedKey.allowed_models === null ? 'primary' : ''}`} disabled={policyBusy || !selectedKey.writable} onClick={() => void updateModelPolicy(null)}><ShieldCheck size={14}/>全部允许</button>
              <button className={`btn ${selectedKey.allowed_models?.length === 0 ? 'danger' : ''}`} disabled={policyBusy || !selectedKey.writable} onClick={() => void updateModelPolicy([])}><ShieldOff size={14}/>全部禁止</button>
            </div>
          </header>
          {!selectedKey.writable && <div className="key-model-policy-notice">环境变量密钥为启动配置，不能在网页端热修改。请将密钥迁移到 api/keys.json 后设置白名单。</div>}
          {policyBusy && <div className="key-model-policy-saving"><LoaderCircle className="spin" size={14}/>正在保存并热加载…</div>}
          <div className="key-model-list">{models.map(model => {
            const allowed = selectedKey.allowed_models === null || selectedKey.allowed_models.includes(model.id)
            return <div className={`key-model-row ${allowed ? 'allowed' : 'denied'}`} key={model.id}>
              <div className="key-model-copy"><strong>{model.id}</strong><span>{model.provider_id} · 原始模型名 {model.provider_model}</span></div>
              <div className="key-model-state"><Badge tone={model.enabled ? 'success' : 'muted'}>{model.enabled ? '全局可用' : '全局不可用'}</Badge><button className={`toggle ${allowed ? 'on' : ''}`} disabled={policyBusy || !selectedKey.writable} onClick={() => toggleModel(model.id)} aria-label={`${allowed ? '禁止' : '允许'} ${model.id}`}><i/></button><b>{allowed ? '允许' : '禁止'}</b></div>
            </div>
          })}</div>
          {!models.length && <div className="key-model-policy-empty">当前没有已注册模型。</div>}
        </div>}
      </Card>
    </div>}
  </>
}
