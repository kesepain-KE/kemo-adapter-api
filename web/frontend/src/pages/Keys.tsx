import { useState } from 'react'
import { Copy, Trash2 } from 'lucide-react'
import { api, useAction, useConsole } from '../console'
import type { Entity } from '../console'
import { Modal, PageHeader, Toggle, useToast } from '../components/UI'
import { Select } from '../components/Select'
import { Empty, ErrorBar, Field } from '../components/Form'
import { t } from '../i18n'

export default function Keys() {
  const { data, mutate } = useConsole()
  const [draft, setDraft] = useState<Entity | null>(null)
  const [secret, setSecret] = useState('')
  const [revision, setRevision] = useState('')
  const action = useAction()
  const toast = useToast()
  const edit = (name: string, value: any) => setDraft({ ...draft, [name]: value })
  return <div className="page"><PageHeader title="下游密钥"
    actions={<button className="btn btn-primary" onClick={() => { setRevision(data!.key_revision); setDraft({ name: '', note: '', allowed_models: null,
      allowed_ops: ['llm', 'embedding', 'rerank'], rpm: 0, tpm: 0, budget: null, currency: 'USD', enabled: true }) }}>{t('创建密钥')}</button>} />
    <ErrorBar error={action.error} />
    <div className="card table-scroll"><table className="model-table"><thead><tr>{['名称', '密钥', '允许模型', '额度', '状态', '操作'].map(l => <th key={l}>{t(l)}</th>)}</tr></thead><tbody>
      {data!.keys.map(k => <tr key={k.id}><td>{k.name || k.id}<div className="muted small">{k.created_at}</div></td><td><div className="row-actions">
        <span className="mono">{k.preview}</span><button className="icon-btn" disabled={action.busy || !k.can_copy}
          title={t(k.can_copy ? '复制密钥' : '历史密钥仅有哈希，无法还原')} aria-label={`${t('复制密钥')} ${k.name || k.id}`}
          onClick={() => { void action.run(async () => {
            const result = await api(`/keys/${k.id}/reveal`, { method: 'POST', body: JSON.stringify({ expected_revision: data!.key_revision, value: {} }) })
            await navigator.clipboard.writeText(result.token); toast(t('已复制'))
          }) }}><Copy size={14} /></button>
        <button className="icon-btn danger-text" disabled={action.busy} title={t('删除密钥')} aria-label={`${t('删除密钥')} ${k.name || k.id}`}
          onClick={() => { if (window.confirm(t('确认删除此密钥？删除后立即停止接受新的调用。'))) void action.run(() => mutate(`/keys/${k.id}`, 'DELETE', {}, true)) }}><Trash2 size={14} /></button>
        </div>{!k.can_copy && <span className="small muted">{t('历史密钥仅有哈希，无法还原')}</span>}</td><td>{k.allowed_models === null ? t('全部模型') : k.allowed_models.join(', ') || t('无')}</td>
        <td>{k.rpm || '∞'} RPM · {k.tpm || '∞'} TPM<div className="small muted">{k.budget === null ? t('无预算上限') : `${k.budget} ${k.currency}`}</div></td>
        <td><Toggle checked={k.enabled} disabled={action.busy} label={t('启用')} onChange={value => { void action.run(() => mutate(`/keys/${k.id}`, 'PATCH', { enabled: value }, true)) }} /></td>
        <td><button className="btn btn-sm" onClick={() => { setDraft(k); setRevision(data!.key_revision) }}>{t('编辑')}</button></td></tr>)}
    </tbody></table>{!data!.keys.length && <Empty text="暂无下游密钥" />}</div>
    <Modal open={!!draft} onClose={() => setDraft(null)} title={draft?.id ? '编辑密钥' : '创建密钥'} width={760}>
      {draft && <><ErrorBar error={action.error} /><div className="form-grid two">
        <Field label="名称"><input className="input" value={draft.name} onChange={e => edit('name', e.target.value)} /></Field>
        <Field label="备注"><input className="input" value={draft.note} onChange={e => edit('note', e.target.value)} /></Field>
        <Field label="RPM 上限"><input type="number" min="0" className="input" value={draft.rpm} onChange={e => edit('rpm', Number(e.target.value))} /></Field>
        <Field label="TPM 上限"><input type="number" min="0" className="input" value={draft.tpm} onChange={e => edit('tpm', Number(e.target.value))} /></Field>
        <Field label="预算上限"><input type="number" min="0" step="any" className="input" placeholder={t('不限制')} value={draft.budget ?? ''} onChange={e => edit('budget', e.target.value === '' ? null : Number(e.target.value))} /></Field>
        <Field label="币种"><Select value={draft.currency} onChange={value => edit('currency', value)} options={['USD', 'CNY', 'EUR', 'JPY', 'GBP'].map(c => ({ value: c, label: c }))} /></Field>
      </div><Field label="允许模型"><label className="check-label"><input type="checkbox" checked={draft.allowed_models === null} onChange={e => edit('allowed_models', e.target.checked ? null : [])} />{t('全部模型')}</label>
        {draft.allowed_models !== null && <ModelScopePicker value={draft.allowed_models} onChange={value => edit('allowed_models', value)} />}</Field>
      <Field label="允许操作"><div className="cap-row">{['llm', 'embedding', 'rerank'].map(op => <label key={op} className="check-label"><input type="checkbox" checked={draft.allowed_ops.includes(op)} onChange={e => edit('allowed_ops', e.target.checked ? [...draft.allowed_ops, op] : draft.allowed_ops.filter((v: string) => v !== op))} />{op}</label>)}</div></Field>
      <p className="hint">{t('0 表示不限制。启用 TPM 或预算前，请配置模型 token 硬上限和价格。')}</p>
      <div className="editor-actions"><button className="btn btn-primary" disabled={action.busy || !draft.name.trim()} onClick={() => { void action.run(async () => {
        const { id, prefix: _prefix, preview: _preview, created_at: _created, can_copy: _canCopy, ...value } = draft
        const result = await mutate(id ? `/keys/${id}` : '/keys', id ? 'PATCH' : 'POST', value, true, revision)
        if (result.token) setSecret(result.token)
        setDraft(null)
      }) }}>{t('保存配置')}</button></div></>}
    </Modal>
    <Modal open={!!secret} onClose={() => setSecret('')} title="保存密钥">
      <p className="hint">{t('密钥已加密保存，之后可从列表复制。')}</p><code className="secret-once">{secret}</code>
      <div className="editor-actions"><button className="btn" onClick={() => { void action.run(() => navigator.clipboard.writeText(secret)) }}>{t('复制')}</button><button className="btn btn-primary" onClick={() => setSecret('')}>{t('已保存')}</button></div>
    </Modal>
  </div>
}

function ModelScopePicker({ value, onChange }: { value: string[]; onChange: (value: string[]) => void }) {
  const { data } = useConsole()
  const [provider, setProvider] = useState('')
  const [query, setQuery] = useState('')
  const groups = data!.upstreams.map(upstream => ({
    id: upstream.id as string, name: upstream.name as string,
    models: [...new Set(data!.deployments.filter(d => d.upstream_id === upstream.id).map(d => d.public_model as string))],
  })).filter(group => group.models.length)
  const unassigned = [...new Set([...data!.models.map(m => m.id as string), ...value])].filter(id =>
    !data!.deployments.some(d => d.public_model === id))
  if (unassigned.length) groups.push({ id: '/unassigned', name: t('未关联提供商'), models: unassigned })
  const active = groups.find(group => group.id === provider) || groups[0]
  const models = active?.models.filter(id => id.toLowerCase().includes(query.toLowerCase())) || []
  return <div className="key-model-picker">
    <div className="key-model-providers" role="tablist" aria-label={t('提供商')}>{groups.map(group =>
      <button key={group.id} type="button" role="tab" aria-selected={group.id === active?.id}
        className={`key-model-provider${group.id === active?.id ? ' active' : ''}`} onClick={() => { setProvider(group.id); setQuery('') }}>
        <span>{group.name}</span><span className="tag">{group.models.filter(id => value.includes(id)).length}/{group.models.length}</span>
      </button>)}</div>
    <div className="key-model-options" role="tabpanel">
      <input className="input" placeholder={t('搜索模型')} aria-label={t('搜索允许模型')} value={query} onChange={event => setQuery(event.target.value)} />
      {!!models.length && <button type="button" className="btn btn-sm" onClick={() => {
        const all = models.every(id => value.includes(id))
        onChange(all ? value.filter(id => !models.includes(id)) : [...new Set([...value, ...models])])
      }}>{t('选择 / 取消当前列表')}</button>}
      <div className="key-model-list">{models.map(id => <label className="check-label" key={id}>
        <input type="checkbox" checked={value.includes(id)} onChange={event => onChange(event.target.checked ? [...value, id] : value.filter(item => item !== id))} />
        <span className="mono">{id}</span></label>)}
        {!models.length && <Empty text="没有匹配的模型" />}</div>
    </div>
  </div>
}
