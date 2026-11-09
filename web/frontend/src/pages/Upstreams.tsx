import { parse, stringify } from 'yaml'
import { useState } from 'react'
import { ArrowDown, ArrowUp, ChevronRight, KeyRound, Layers, Link, LoaderCircle, Pencil, Plus, Radar, Save, Search, Settings2, Trash2 } from 'lucide-react'
import { api, ApiError, useAction, useConsole } from '../console'
import type { Entity } from '../console'
import { Modal, PageHeader, Toggle, VerifyBadge } from '../components/UI'
import { Empty, ErrorBar, Field } from '../components/Form'
import { ModelEditor } from '../components/ModelEditor'
import { Select } from '../components/Select'
import { t } from '../i18n'

type Tab = 'models' | 'connection' | 'credentials' | 'advanced'
const tabs = [
  { id: 'models' as Tab, label: '模型', icon: Layers },
  { id: 'connection' as Tab, label: '连接配置', icon: Link },
  { id: 'credentials' as Tab, label: '凭据', icon: KeyRound },
  { id: 'advanced' as Tab, label: '高级配置', icon: Settings2 },
]

export default function Upstreams() {
  const { data, mutate } = useConsole()
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState('')
  const [creating, setCreating] = useState(false)
  const visible = data!.upstreams.filter(u => [
    u.name, u.id, u.base_url, u.adapter_id,
    ...data!.deployments.filter(d => d.upstream_id === u.id).map(d => `${d.upstream_model} ${d.public_model}`),
  ].join(' ').toLowerCase().includes(query.toLowerCase()))
  const active = visible.find(u => u.id === selected) || visible[0]
  return <div className="page upstreams-page">
    <PageHeader title="上游服务"
      actions={<button className="btn btn-primary" onClick={() => setCreating(true)}><Plus size={14} />{t('添加上游')}</button>} />
    <div className="provider-workspace">
      <aside className="provider-sidebar">
        <div className="provider-sidebar-head"><span>{t('提供商')}</span><span className="tag">{data!.upstreams.length}</span></div>
        <div className="provider-search"><Search size={15} /><input className="input" placeholder={t('搜索名称、ID、地址或模型')} aria-label={t('搜索提供商')} value={query} onChange={e => setQuery(e.target.value)} /></div>
        <nav className="provider-list" aria-label={t('提供商')}>
          {visible.map(u => <button key={u.id} className={`provider-item${u.id === active?.id ? ' selected' : ''}`}
            aria-current={u.id === active?.id ? 'true' : undefined} onClick={() => setSelected(u.id)}>
            <span className="up-logo">{u.name.slice(0, 2).toUpperCase()}</span>
            <span className="up-main"><span className="provider-name">{u.name}</span>
              <span className="provider-meta">{u.credentials.length} {t('密钥')} · {data!.deployments.filter(d => d.upstream_id === u.id).length} {t('模型')}</span></span>
            <span className={`dot ${u.enabled ? 'dot-green' : 'dot-gray'}`} /><ChevronRight size={13} className="provider-chevron" />
          </button>)}
        </nav>
        {!visible.length && <Empty text="添加第一个上游，或调整搜索条件" />}
      </aside>
      <main className="provider-panel">
        {active ? <UpstreamEditor key={active.id} upstream={active} onClose={() => setSelected('')} onSave={async (value, revision) => (
          mutate(`/upstreams/${active.id}`, 'PATCH', value, false, revision)
        )} /> : <div className="provider-welcome"><Layers size={28} /><Empty text="选择或添加提供商" />
          <button className="btn" onClick={() => setCreating(true)}><Plus size={14} />{t('添加上游')}</button></div>}
      </main>
    </div>
    <Modal open={creating} onClose={() => setCreating(false)} title="添加上游" width={680}>
      <UpstreamEditor upstream={null} onClose={() => setCreating(false)} onSave={async (value, revision) => {
        const result = await mutate('/upstreams', 'POST', value, false, revision)
        setCreating(false); setQuery(''); setSelected(result.id)
        return result
      }} />
    </Modal>
  </div>
}

function UpstreamEditor({ upstream, onSave, onClose }: {
  upstream: Entity | null; onSave: (value: Entity, revision: string) => Promise<Entity>; onClose: () => void
}) {
  const { data, mutate } = useConsole()
  const [tab, setTab] = useState<Tab>('models')
  const [revision, setRevision] = useState(data!.revision)
  const [name, setName] = useState(upstream?.name || '')
  const [template, setTemplate] = useState(upstream?.template_id || '')
  const [url, setUrl] = useState(upstream?.base_url || '')
  const [adapter, setAdapter] = useState(upstream?.adapter_id || 'openai-chat')
  const [auth, setAuth] = useState(upstream?.auth_scheme || 'bearer')
  const [proxy, setProxy] = useState(upstream?.proxy || '')
  const [note, setNote] = useState(upstream?.note || '')
  const [fieldError, setFieldError] = useState<{ field: string; message: string } | null>(null)
  const [tokenizer, setTokenizer] = useState(upstream?.tokenizer || 'auto')
  const [timeout, setTimeout] = useState(upstream?.timeout || 120)
  const [enabled, setEnabled] = useState(upstream?.enabled ?? true)
  const [privateNetwork, setPrivate] = useState(upstream?.allow_private_network ?? false)
  const [keys, setKeys] = useState<Entity[]>(upstream?.credentials.map((k: Entity) => ({ ...k })).sort((a: Entity, b: Entity) => b.priority - a.priority) || [])
  const [newKey, setNewKey] = useState('')
  const [profile, setProfile] = useState(stringify(upstream?.profile || {}))
  const [headers, setHeaders] = useState(stringify(upstream?.headers || {}))
  const [discovered, setDiscovered] = useState<string[]>([])
  const [discoveryNotice, setDiscoveryNotice] = useState('')
  const [references, setReferences] = useState<string[]>([])
  const [manual, setManual] = useState('')
  const [modelQuery, setModelQuery] = useState('')
  const [selected, setSelected] = useState<Record<string, string>>({})
  const [registering, setRegistering] = useState(false)
  const [report, setReport] = useState<Entity[]>([])
  const [editing, setEditing] = useState<{ model: Entity; deploymentId: string } | null>(null)
  const action = useAction()
  const selectedAdapter = data!.adapters.find(a => a.id === adapter)
  const deployments = data!.deployments.filter(d => d.upstream_id === upstream?.id)
  const registered = new Set(deployments.map(d => d.upstream_model))
  const models = discovered.filter(model => model.toLowerCase().includes(modelQuery.toLowerCase()))
  const templates = data!.templates.filter(item => item.enabled && item.valid)
  const defaultName = (remote: string) => {
    const parts = remote.replace(/^@cf\//, '').split('/')
    const vendor = parts.length > 1 ? parts.shift()! : data!.model_vendors[template] || template || 'custom'
    return `${template || name}-${vendor}-${parts.join('-')}`.trim().replace(/[\s/\\]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 200)
  }
  const moveKey = (index: number, direction: number) => {
    const next = [...keys]
    ;[next[index], next[index + direction]] = [next[index + direction], next[index]]
    setKeys(next)
  }
  const save = async () => {
    const newKeys = newKey.trim() ? [...keys, { value: newKey.trim(), enabled: true, priority: 0 }] : keys
    setFieldError(null)
    let result: Entity
    try {
      result = await onSave({
        name, template_id: template || null, base_url: url, adapter_id: adapter, auth_scheme: auth, timeout,
        enabled, allow_private_network: privateNetwork, profile: parse(profile), headers: parse(headers), tokenizer,
        proxy: proxy.trim() || null, note,
        keys: newKeys.map((k, index) => ({ id: k.id, value: k.value, enabled: k.enabled, priority: newKeys.length - index })),
      }, revision)
    } catch (cause) {
      if (cause instanceof ApiError && cause.field) setFieldError({ field: cause.field, message: cause.message })
      throw cause
    }
    setRevision(result.revision)
    setKeys(result.upstream.credentials.map((credential: Entity) => ({ ...credential })).sort((a: Entity, b: Entity) => b.priority - a.priority))
    setNewKey('')
    return result.revision as string
  }
  const connection = <div className="form-grid two">
    <Field label="显示名称"><input className="input" value={name} onChange={e => setName(e.target.value)} /></Field>
    <Field label="协议适配器"><Select value={adapter} onChange={value => {
      const next = data!.adapters.find(a => a.id === value)!
      setAdapter(next.id); setAuth(next.auth_schemes[0].id); setProfile('{}'); setTemplate('')
    }} options={data!.adapters.filter(a => a.builtin || a.enabled).map(a => ({ value: a.id, label: a.name, description: a.id }))} /></Field>
    <Field label="Base URL" error={fieldError?.field === 'base_url' ? fieldError.message : ''}><input className="input mono" placeholder="https://…" value={url} onChange={e => setUrl(e.target.value)} /></Field>
    <Field label="鉴权方式" error={fieldError?.field === 'auth_scheme' ? fieldError.message : ''}><Select value={auth} onChange={setAuth} options={(selectedAdapter?.auth_schemes || []).map((a: Entity) => ({ value: a.id, label: a.id, description: a.name ? `${a.location}: ${a.name}` : '' }))} /></Field>
    <Field label="超时（秒）"><input className="input" type="number" min="1" max="7200" value={timeout} onChange={e => setTimeout(Number(e.target.value))} /></Field>
    <Field label="启用状态"><Toggle checked={enabled} onChange={setEnabled} label={t('参与路由')} /></Field>
    <label className="check-label"><input type="checkbox" checked={privateNetwork} onChange={e => setPrivate(e.target.checked)} />{t('允许连接本机和私网服务')}</label>
  </div>
  const credentials = <div className="edit-section">
    {keys.map((k, index) => {
      const health = data!.credential_health[upstream?.id]?.[k.id]
      const status = !k.enabled ? 'disabled' : health?.status || 'unknown'
      const labels: Record<string, string> = { healthy: '正常', invalid: '凭据无效', cooldown: '冷却中', unavailable: '服务异常', disabled: '已禁用', unknown: '未检测' }
      return <div className="key-row credential-row" key={k.id || index}>
        <span className="key-mask mono">{k.preview || t('待保存')}</span>
        <div className="credential-priority"><span className="mono">{index + 1}</span>
          <button className="icon-btn" disabled={index === 0 || action.busy} aria-label={`${t('上移密钥')} ${index + 1}`} onClick={() => moveKey(index, -1)}><ArrowUp size={14} /></button>
          <button className="icon-btn" disabled={index === keys.length - 1 || action.busy} aria-label={`${t('下移密钥')} ${index + 1}`} onClick={() => moveKey(index, 1)}><ArrowDown size={14} /></button></div>
        <span className={`credential-health ${status}`} title={[
          health?.checked_at ? `${t('最近检测')} · ${new Date(health.checked_at).toLocaleString()}` : t('等待实际调用'),
          health?.provider_status, health?.cooldown_seconds ? `${health.cooldown_seconds}s` : '',
        ].filter(Boolean).join('\n')}><span className={`dot ${status === 'healthy' ? 'dot-green' : status === 'cooldown' ? 'dot-amber' : ['invalid', 'unavailable'].includes(status) ? 'dot-red' : 'dot-gray'}`} />{t(labels[status])}</span>
        <div className="credential-actions"><Toggle checked={k.enabled} onChange={value => setKeys(keys.map((item, i) => i === index ? { ...item, enabled: value } : item))} label={t('启用')} />
          <button className="icon-btn" title={t('删除')} aria-label={`${t('删除密钥')} ${index + 1}`} onClick={() => setKeys(keys.filter((_, i) => i !== index))}><Trash2 size={14} /></button></div>
      </div>
    })}
    <Field label="API Key"><input className="input mono" type="password" autoComplete="new-password" placeholder={t('粘贴新的上游 API Key')} value={newKey} onChange={e => setNewKey(e.target.value)} /></Field>
    {upstream && <button className="btn provider-key-add" disabled={!newKey.trim()} onClick={() => {
      const raw = newKey.trim()
      setKeys([...keys, { value: raw, preview: raw.length > 15 ? `${raw.slice(0, 10)}…${raw.slice(-5)}` : '***', enabled: true, priority: 0 }]); setNewKey('')
    }}><Plus size={14} />{t('添加密钥')}</button>}
  </div>
  const advanced = <div className="form-grid two provider-advanced">
    <div className="span-2"><Field label="本地计量器" hint="优先使用厂商返回的用量；本地补全始终标记为估算，不推算多模态和缓存用量。">
      <Select value={tokenizer} onChange={setTokenizer} options={[
        { value: 'auto', label: t('自动选择，DeepSeek 兜底') }, ...data!.tokenizers.map(item => ({ value: item.id, label: item.label })),
      ]} /></Field></div>
    <Field label="适配器 Profile" hint="可配置 extra_body 与 extra_query；不能覆盖协议字段或鉴权参数。" error={fieldError?.field.startsWith('profile') ? fieldError.message : ''}><textarea className="input mono config-editor" value={profile} onChange={e => setProfile(e.target.value)} /></Field>
    <Field label="非敏感请求头" error={fieldError?.field === 'headers' ? fieldError.message : ''}><textarea className="input mono config-editor" value={headers} onChange={e => setHeaders(e.target.value)} /></Field>
    <Field label="出口代理" hint="可选 HTTP(S) 代理地址，例如 http://127.0.0.1:7890" error={fieldError?.field === 'proxy' ? fieldError.message : ''}><input className="input mono" value={proxy} onChange={e => setProxy(e.target.value)} placeholder="http://127.0.0.1:7890" /></Field>
    <Field label="备注"><textarea className="input" value={note} onChange={e => setNote(e.target.value)} /></Field>
  </div>
  return <div className={`provider-editor${upstream ? '' : ' provider-create'}`}>
    {upstream && <>
      <header className="provider-detail-head">
        <span className="up-logo">{name.slice(0, 2).toUpperCase()}</span>
        <div className="up-main"><h2>{upstream.name}</h2><span className="up-sub">{selectedAdapter?.name || adapter} · {upstream.id}</span></div>
        <span className="provider-status"><span className={`dot ${upstream.enabled ? 'dot-green' : 'dot-gray'}`} />{t(upstream.enabled ? '已启用' : '已停用')}</span>
      </header>
      <div className="provider-tabs" role="tablist" aria-label={t('提供商面板')}>
        {tabs.map(({ id, label, icon: Icon }) => <button key={id} id={`tab-${upstream.id}-${id}`} className={`provider-tab${tab === id ? ' active' : ''}`} role="tab"
          aria-selected={tab === id} aria-controls={`panel-${upstream.id}`} onClick={() => setTab(id)}><Icon size={15} />{t(label)}
          {id === 'models' && <span className="tag">{deployments.length}</span>}</button>)}
      </div>
    </>}
    <div className="provider-body" role={upstream ? 'tabpanel' : undefined} id={upstream ? `panel-${upstream.id}` : undefined}
      aria-labelledby={upstream ? `tab-${upstream.id}-${tab}` : undefined}>
      <ErrorBar error={action.error} />
      {!upstream ? <>
        <Field label="提供商模板"><Select value={template} onChange={value => {
          setTemplate(value)
          const defaults = templates.find(item => item.id === value)?.template
          if (!defaults) return
          setName(defaults.name); setUrl(defaults.base_url); setAdapter(defaults.adapter_id)
          setAuth(defaults.auth_scheme); setProfile(stringify(defaults.profile)); setHeaders(stringify(defaults.headers))
          setTokenizer(defaults.tokenizer || 'auto')
          setTimeout(defaults.timeout ?? 120); setPrivate(defaults.allow_private_network ?? false)
          setNote(defaults.note || '')
        }} options={[{ value: '', label: t('自定义连接') }, ...templates.map(item => ({ value: item.id, label: item.name,
          description: `${t(item.category === 'coding_service' ? '编码工具服务' : '模型服务')} · ${item.template.adapter_id} · ${item.verification || 'declared'}` }))]} /></Field>
        {auth !== 'none' && credentials}
        {template && !url.includes('{') ? <details className="edit-section"><summary>{t('连接配置')}</summary>{connection}</details> : connection}
        <details className="edit-section"><summary>{t('高级配置')}</summary>{advanced}</details>
      </> : tab === 'connection' ? connection : tab === 'credentials' ? credentials : tab === 'advanced' ? <>
        {advanced}
        <div className="provider-danger"><button className="btn btn-danger" disabled={action.busy} onClick={() => {
          if (window.confirm(t('确认删除此上游？'))) void action.run(async () => {
            await mutate(`/upstreams/${upstream.id}`, 'DELETE', {}, false, revision); onClose()
          })
        }}><Trash2 size={13} />{t('删除上游')}</button></div>
      </> : <>
        <section className="edit-section">
          <div className="provider-section-head"><h3>{t('添加模型')}</h3>
            <button className="btn" disabled={action.busy} onClick={() => { void action.run(async () => {
              const result = await api(`/upstreams/${upstream.id}/discover`, { method: 'POST' })
              setDiscovered(result.models)
              setDiscoveryNotice(result.message || '')
              setReferences(result.reference_models || [])
            }) }}><Radar size={14} />{t('发现模型')}</button></div>
          {!selectedAdapter?.discovery && <p className="hint">{t('此协议不提供模型发现，请手动添加模型名。')}</p>}
          <p className="hint">{t(selectedAdapter?.capability_discovery
            ? '此协议直接读取上游能力声明，登记和探测不会生成内容；测试模型会发送真实请求。'
            : '模型目录可能不完整；可手动填写准确模型 ID，登记时会实际探测。')}</p>
          {discoveryNotice && <p className="hint" role="status">{discoveryNotice}</p>}
          {!!references.length && <div className="row-actions"><span className="small muted">{t('未验证的模板参考')}</span>
            {references.map(model => <button key={model} className="btn btn-sm mono" disabled={action.busy || discovered.includes(model)} onClick={() => {
              setDiscovered([...new Set([...discovered, model])])
              if (!registered.has(model)) setSelected({ ...selected, [model]: defaultName(model) })
            }}>{model}</button>)}</div>}
          <div className="key-add"><input className="input mono" placeholder={t('手动填写上游模型名')} aria-label={t('手动填写上游模型名')} value={manual} onChange={e => setManual(e.target.value)} />
            <button className="btn" disabled={action.busy || !manual.trim()} onClick={() => {
              const model = manual.trim(); setDiscovered([...new Set([...discovered, model])])
              if (!registered.has(model)) setSelected({ ...selected, [model]: defaultName(model) })
              setManual('')
            }}><Plus size={13} />{t('添加')}</button></div>
          {!!discovered.length && <div className="model-discovery">
            <div className="model-discovery-toolbar"><input className="input" placeholder={t('搜索模型')} value={modelQuery} onChange={e => setModelQuery(e.target.value)} />
              <button className="btn btn-sm" disabled={action.busy} onClick={() => {
                const available = models.filter(model => !registered.has(model))
                const all = available.every(model => model in selected)
                const next = { ...selected }
                available.forEach(model => { if (all) delete next[model]; else next[model] = next[model] || defaultName(model) })
                setSelected(next)
              }}>{t('选择 / 取消当前列表')}</button></div>
            <div className="discovery-column-head" aria-hidden="true"><span>{t('上游模型')}</span><span>{t('公开模型')}</span></div>
            <div className="discovered-models table-scroll"><table className="model-table"><thead className="visually-hidden"><tr><th>{t('上游模型')}</th><th>{t('公开模型')}</th></tr></thead><tbody>
              {models.map(model => <tr key={model}><td><label className="check-label"><input type="checkbox" disabled={action.busy || registered.has(model)} checked={registered.has(model) || model in selected} onChange={e => {
                const next = { ...selected }; if (e.target.checked) next[model] = defaultName(model); else delete next[model]; setSelected(next)
              }} /><span className="mono">{model}</span></label>{registered.has(model) && <span className="tag">{t('已登记')}</span>}</td>
                <td>{registered.has(model) ? <span className="muted mono">{deployments.find(d => d.upstream_model === model)?.public_model}</span>
                  : <input className="input mono" aria-label={`${t('公开模型')} ${model}`} disabled={action.busy || !(model in selected)} placeholder={defaultName(model)} maxLength={200}
                    value={selected[model] || ''} onChange={e => setSelected({ ...selected, [model]: e.target.value.replace(/[\s/\\]+/g, '-') })} />}</td></tr>)}
              {!models.length && <tr><td colSpan={2}><Empty text="暂无数据" /></td></tr>}
            </tbody></table></div>
          </div>}
          <div className="model-register-footer">{!selectedAdapter?.capability_discovery &&
            <p className="hint">{t('能力检测仅对选中模型运行，会发送少量真实请求并产生上游费用。')}</p>}
            <button className="btn btn-primary" disabled={action.busy || !Object.keys(selected).length || Object.values(selected).some(value => !value.trim())} onClick={() => { void action.run(async () => {
              setRegistering(true)
              try {
                const result = await mutate(`/upstreams/${upstream.id}/register`, 'POST', {
                  models: Object.entries(selected).map(([upstream_model, public_model]) => ({ upstream_model, public_model })),
                }, false, revision)
                setRevision(result.revision); setReport(result.detection); setSelected({})
              } finally { setRegistering(false) }
            }) }}>{registering ? <LoaderCircle size={14} className="spin" /> : <Plus size={14} />}{t(registering ? '检测并登记中' : '登记选中模型')}</button></div>
          {!!report.length && <div className="registration-report" role="status">
            <span>{t('已登记 {count} 个模型，可在下方编辑详细参数。', { count: report.length })}</span>
            {report.some(item => item.verification !== 'verified' || Object.values(item.checks as Entity).some((check: any) => check.status === 'unknown')) &&
              <span className="hint">{t('部分能力或上限尚未确认，编辑模型可查看和调整。')}</span>}
          </div>}
        </section>
        <section className="edit-section registered-models">
          <div className="edit-head"><h3>{t('已登记模型')}</h3><span className="tag">{deployments.length}</span></div>
          {!deployments.length && <Empty text="选择需要的模型并登记，即可开始使用" />}
          {deployments.map(d => {
            const model = data!.models.find(m => m.id === d.public_model)!
            return <div className="registered-model-row" key={d.id}>
              <div className="up-main"><span className="mono model-id">{d.public_model}</span><span className="muted model-up">← {d.upstream_model}</span></div>
              <VerifyBadge status={model.verification} />
              <div className="registered-model-actions"><Toggle checked={model.enabled} disabled={action.busy}
                label={t('启用')} onChange={enabled => { void action.run(async () => {
                  const result = await mutate(`/models/${encodeURIComponent(model.id)}`, 'PATCH', { enabled }, false, revision)
                  setRevision(result.revision)
                }) }} />
                <button className="btn btn-sm" aria-label={`${t('编辑模型')} ${model.id}`} onClick={() => setEditing({ model, deploymentId: d.id })}><Pencil size={12} />{t('编辑')}</button>
                <button className="btn btn-sm btn-danger" disabled={action.busy} aria-label={`${t('删除模型')} ${model.id}`} onClick={() => {
                  if (window.confirm(t('确认删除此处的模型登记？其他提供商的关联会保留。'))) void action.run(async () => {
                    const result = await mutate(`/deployments/${d.id}?remove_orphan_model=true`, 'DELETE', {}, false, revision)
                    setRevision(result.revision)
                  })
                }}><Trash2 size={12} />{t('删除')}</button></div>
            </div>
          })}
        </section>
      </>}
      {(!upstream || tab !== 'models') && <div className="editor-actions provider-save-actions">
        {!upstream && <button className="btn" onClick={onClose}>{t('取消')}</button>}
        <button className="btn btn-primary" disabled={action.busy || !name.trim() || !url.trim() || (!upstream && auth !== 'none' && !newKey.trim())}
          onClick={() => { void action.run(save) }}><Save size={14} />{t(upstream ? '保存配置' : '保存并选择模型')}</button>
      </div>}
    </div>
    {editing && <ModelEditor key={editing.deploymentId} model={editing.model} deploymentId={editing.deploymentId} onClose={() => setEditing(null)} onSaved={setRevision} />}
  </div>
}
