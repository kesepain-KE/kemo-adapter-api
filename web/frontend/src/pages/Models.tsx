import { Fragment, useEffect, useState } from 'react'
import { Check, CircleAlert, FlaskConical, Layers, LoaderCircle, Search, X } from 'lucide-react'
import { useAction, useConsole } from '../console'
import type { Entity } from '../console'
import { PageHeader, VerifyBadge } from '../components/UI'
import { Empty, ErrorBar } from '../components/Form'
import { ModelEditor } from '../components/ModelEditor'
import { t } from '../i18n'

const testLabels = { success: '成功', failure: '失败', partial: '部分成功' } as const

export default function Models() {
  const { data, mutate } = useConsole()
  const [query, setQuery] = useState('')
  const [provider, setProvider] = useState('')
  const [editing, setEditing] = useState<Entity | null>(null)
  const [testing, setTesting] = useState<string | null>(null)
  const [results, setResults] = useState<Entity[]>([])
  const [feedback, setFeedback] = useState<{ modelId: string; status: keyof typeof testLabels } | null>(null)
  const action = useAction()
  useEffect(() => {
    if (!feedback) return
    const timer = window.setTimeout(() => setFeedback(null), 3000)
    return () => window.clearTimeout(timer)
  }, [feedback])
  const models = data!.models.filter(m => m.enabled)
  const groups = data!.upstreams.map(upstream => ({
    id: upstream.id as string, name: upstream.name as string,
    models: models.filter(m => data!.deployments.some(d => d.public_model === m.id && d.upstream_id === upstream.id)),
  }))
  groups.push({
    id: '/unassigned', name: t('未关联提供商'),
    models: models.filter(m => !data!.deployments.some(d => d.public_model === m.id)),
  })
  const providerGroups = groups.filter(group => group.models.length)
  const activeProvider = providerGroups.find(group => group.id === provider)
  const search = query.trim().toLowerCase()
  const visibleModels = (activeProvider?.models ?? models).filter(m =>
    `${m.id} ${m.display_name}`.toLowerCase().includes(search))
  const testModel = async (model: Entity) => {
    const revision = data!.revision
    const targets: Entity[] = data!.deployments.filter(d => d.public_model === model.id).map(d => {
      const upstream = data!.upstreams.find(u => u.id === d.upstream_id)!
      return { ...d, upstream_name: upstream.name, status: d.enabled && upstream.enabled ? 'pending' : 'disabled' }
    })
    const update = (identity: string, value: Entity) => setResults(current =>
      current.map(item => item.id === identity ? { ...item, ...value } : item))
    let passed = 0
    let total = 0
    setFeedback(null)
    setResults(targets)
    setTesting(model.id)
    try {
      for (const target of targets) {
        if (target.status === 'disabled') continue
        total += 1
        update(target.id, { status: 'testing' })
        try {
          const result = await mutate(`/deployments/${target.id}/test?first_token=true`, 'POST', {}, false, revision)
          if (result.status === 'supported') passed += 1
          update(target.id, { status: 'done', result })
        } catch (cause) {
          update(target.id, { status: 'failed', error: (cause as Error).message })
        }
      }
    } finally {
      setTesting(null)
      setResults([])
      setFeedback({ modelId: model.id, status: total > 0 && passed === total ? 'success' : passed > 0 ? 'partial' : 'failure' })
    }
  }
  return <div className="page models-page"><PageHeader title="模型管理" />
    <ErrorBar error={action.error} />
    <div className="toolbar model-list-toolbar">
      <div className="search"><Search size={15} /><input className="input" placeholder={t('搜索模型')} aria-label={t('搜索模型')}
        value={query} onChange={e => setQuery(e.target.value)} /></div>
    </div>
    <div className="provider-workspace model-workspace">
      <aside className="provider-sidebar">
        <div className="provider-sidebar-head"><span>{t('提供商')}</span><span className="tag">{providerGroups.length}</span></div>
        <nav className="provider-list model-provider-list" aria-label={t('提供商')}>
          <button type="button" className={`provider-item${!activeProvider ? ' selected' : ''}`}
            aria-current={!activeProvider ? 'page' : undefined} aria-controls="model-provider-panel" onClick={() => setProvider('')}>
            <span className="up-logo"><Layers size={17} /></span>
            <span className="up-main"><span className="provider-name">{t('全部提供商')}</span></span>
            <span className="tag">{models.length}</span>
          </button>
          {providerGroups.map(group => <button type="button" key={group.id}
            className={`provider-item${activeProvider?.id === group.id ? ' selected' : ''}`}
            aria-current={activeProvider?.id === group.id ? 'page' : undefined} aria-controls="model-provider-panel"
            onClick={() => setProvider(group.id)}>
            <span className="up-logo">{group.name.slice(0, 2).toUpperCase()}</span>
            <span className="up-main"><span className="provider-name" title={group.name}>{group.name}</span></span>
            <span className="tag">{group.models.length}</span>
          </button>)}
        </nav>
      </aside>
      <section className="provider-panel model-provider-panel" id="model-provider-panel" aria-labelledby="model-provider-title">
      <div className="panel-head"><h2 className="panel-title" id="model-provider-title">{activeProvider?.name ?? t('全部提供商')}</h2><span className="tag">{visibleModels.length} {t('模型')}</span></div>
    <div className="table-scroll"><table className="model-table"><thead><tr>{['公开模型', '任务类型', '路由', '状态', '操作'].map(label => <th key={label}>{t(label)}</th>)}</tr></thead><tbody>
      {visibleModels.map(m => <Fragment key={m.id}><tr><td><div className="model-id" title={m.id}>{m.id}</div><div className="muted small model-display-name" title={m.display_name}>{m.display_name}</div></td><td>{m.task}</td><td>{data!.deployments.filter(d => d.public_model === m.id).length}</td><td><VerifyBadge status={m.verification} /></td><td className="model-actions-cell"><div className="row-actions model-actions">
        <button className={`btn btn-sm model-test-button${feedback && feedback.modelId === m.id ? ` test-${feedback.status}` : ''}`} disabled={action.busy || !!testing || !data!.deployments.some(d => d.public_model === m.id && d.enabled && data!.upstreams.some(u => u.id === d.upstream_id && u.enabled))}
          onClick={() => { void testModel(m) }}>
          {testing === m.id ? <LoaderCircle size={13} className="spin" /> : !feedback || feedback.modelId !== m.id ? <FlaskConical size={13} /> : feedback.status === 'success' ? <Check size={13} /> : feedback.status === 'partial' ? <CircleAlert size={13} /> : <X size={13} />}
          <span aria-live="polite">{t(testing === m.id ? '测试中' : feedback && feedback.modelId === m.id ? testLabels[feedback.status] : '测试')}</span>
        </button>
        <button className="btn btn-sm" disabled={!!testing} onClick={() => setEditing(m)}>{t('编辑')}</button>
        <button className="btn btn-sm" disabled={action.busy || !!testing} onClick={() => { void action.run(() => mutate(`/models/${encodeURIComponent(m.id)}`, 'PATCH', { enabled: !m.enabled })) }}>{t(m.enabled ? '停用' : '启用')}</button>
        <button className="btn btn-sm" disabled={action.busy || !!testing} onClick={() => {
          if (window.confirm(t('确认删除此公开模型及其全部上游关联？历史调用记录将保留。'))) void action.run(() => mutate(`/models/${encodeURIComponent(m.id)}`, 'DELETE'))
        }}>{t('删除')}</button>
      </div></td></tr>
        {testing === m.id && <tr className="model-test-row"><td colSpan={5}><div className="model-quick-test" role="status">
          <p className="hint">{t('逐个测试已启用的子模型；流式请求显示首字延迟，其余显示响应耗时。')}</p>
          {results.map(item => <div className="model-quick-test-item" key={item.id}>
            <div className="model-quick-test-target"><strong>{item.upstream_name}</strong><span className="mono muted">{item.upstream_model}</span></div>
            <div className="model-quick-test-result">
              <span className={item.result?.status === 'supported' ? 'test-success' : item.status === 'failed' || item.result ? 'test-failure' : 'muted'}>
                {t(item.status === 'disabled' ? '已停用' : item.status === 'pending' ? '等待中' : item.status === 'testing' ? '测试中' : item.result?.status === 'supported' ? '测试成功' : '测试未通过')}
              </span>
              {item.result && <span className="mono">{t(item.result.first_token_ms != null ? '首字延迟' : '响应耗时')} · {item.result.first_token_ms ?? item.result.latency_ms} ms</span>}
              {item.result?.status !== 'supported' && item.result?.reason && <span className="muted">{item.result.reason}{item.result.provider_status ? ` · HTTP ${item.result.provider_status}` : ''}</span>}
              {item.error && <span className="test-failure">{t(item.error)}</span>}
            </div>
          </div>)}
        </div></td></tr>}
      </Fragment>)}
      {!visibleModels.length && <tr><td colSpan={5}><Empty text={models.length ? '没有匹配的模型' : '在上游服务中发现并登记模型'} /></td></tr>}
    </tbody></table></div></section></div>
    {editing && <ModelEditor key={editing.id} model={editing} onClose={() => setEditing(null)} />}
  </div>
}
