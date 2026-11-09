import { useState } from 'react'
import { useAction, useConsole } from '../console'
import type { Entity } from '../console'
import { Modal, PageHeader, Toggle } from '../components/UI'
import { Empty, ErrorBar, Field } from '../components/Form'
import { t } from '../i18n'
import { Select } from '../components/Select'

export default function Routing() {
  const { data, mutate } = useConsole()
  const [draft, setDraft] = useState<Entity | null>(null)
  const [revision, setRevision] = useState('')
  const action = useAction()
  const edit = (key: string, value: any) => setDraft({ ...draft, [key]: value })
  return <div className="page"><PageHeader title="路由策略"
    actions={<button className="btn btn-primary" disabled={!data!.models.length || !data!.upstreams.length} onClick={() => { setRevision(data!.revision); setDraft({
      public_model: data!.models[0].id, upstream_id: data!.upstreams[0].id,
      upstream_model: '', priority: 0, weight: 1, rpm: 0, enabled: true,
      price: { input_per_million: null, output_per_million: null, cached_input_per_million: null, currency: 'USD' },
    }) }}>{t('添加 Deployment')}</button>} />
    <ErrorBar error={action.error} /><div className="card table-scroll"><table className="model-table"><thead><tr>{['公开模型', '上游服务', '上游模型', '优先级', '权重', '状态', '操作'].map(l => <th key={l}>{t(l)}</th>)}</tr></thead><tbody>
      {data!.deployments.map(d => <tr key={d.id}><td className="mono">{d.public_model}</td><td>{data!.upstreams.find(u => u.id === d.upstream_id)?.name || d.upstream_id}</td><td className="mono">{d.upstream_model}</td><td>{d.priority}</td><td>{d.weight}</td><td><span className={`dot ${!d.enabled ? 'dot-gray' : data!.health[d.id] ? 'dot-amber' : 'dot-green'}`} /> {t(!d.enabled ? '已停用' : data!.health[d.id] ? '冷却中' : '可路由')}</td><td><div className="row-actions">
        <button className="btn btn-sm" onClick={() => { setDraft(d); setRevision(data!.revision) }}>{t('编辑')}</button>
        <button className="btn btn-sm" disabled={action.busy} onClick={() => { void action.run(() => mutate(`/deployments/${d.id}`, 'PATCH', { enabled: !d.enabled })) }}>{t(d.enabled ? '停用' : '启用')}</button>
        <button className="btn btn-sm" disabled={action.busy} onClick={() => { if (window.confirm(t('确认删除此 Deployment？'))) void action.run(() => mutate(`/deployments/${d.id}`, 'DELETE')) }}>{t('删除')}</button>
      </div></td></tr>)}
    </tbody></table>{!data!.deployments.length && <Empty text="暂无 Deployment" />}</div>
    <Modal open={!!draft} onClose={() => setDraft(null)} title="Deployment" width={760}>
      {draft && <><ErrorBar error={action.error} /><div className="form-grid two">
        <Field label="公开模型"><Select value={draft.public_model} onChange={value => edit('public_model', value)} options={data!.models.map(m => ({ value: m.id, label: m.id }))} /></Field>
        <Field label="上游服务"><Select value={draft.upstream_id} onChange={value => edit('upstream_id', value)} options={data!.upstreams.map(u => ({ value: u.id, label: u.name }))} /></Field>
        <Field label="上游模型"><input className="input mono" value={draft.upstream_model} onChange={e => edit('upstream_model', e.target.value)} /></Field>
        {(['priority', 'weight', 'rpm'] as const).map((field, i) => <Field label={['优先级', '权重', 'RPM 上限'][i]} key={field}><input className="input" type="number" value={draft[field]} onChange={e => edit(field, Number(e.target.value))} /></Field>)}
        <Field label="启用状态"><Toggle checked={draft.enabled} onChange={v => edit('enabled', v)} label={t('参与路由')} /></Field>
        <Field label="币种"><Select value={draft.price.currency} onChange={value => edit('price', { ...draft.price, currency: value })} options={['USD', 'CNY', 'EUR', 'JPY', 'GBP'].map(c => ({ value: c, label: c }))} /></Field>
        {['input_per_million', 'output_per_million', 'cached_input_per_million'].map((field, i) => <Field key={field} label={['输入价格 / 百万 token', '输出价格 / 百万 token', '缓存价格 / 百万 token'][i]}><input className="input" type="number" min="0" step="any" placeholder={t('未知')} value={draft.price[field] ?? ''} onChange={e => edit('price', { ...draft.price, [field]: e.target.value === '' ? null : Number(e.target.value) })} /></Field>)}
      </div><div className="editor-actions"><button className="btn btn-primary" disabled={action.busy || !draft.upstream_model.trim()} onClick={() => { void action.run(async () => {
        await mutate(draft.id ? `/deployments/${draft.id}` : '/deployments', draft.id ? 'PATCH' : 'POST', draft, false, revision); setDraft(null)
      }) }}>{t('保存配置')}</button></div></>}
    </Modal>
  </div>
}
