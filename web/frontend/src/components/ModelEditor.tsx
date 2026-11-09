import { useState } from 'react'
import { parse, stringify } from 'yaml'
import { FlaskConical, LoaderCircle, Radar, Save } from 'lucide-react'
import { useAction, useConsole } from '../console'
import type { Entity } from '../console'
import { ErrorBar, Field } from './Form'
import { Modal, Toggle, VerifyBadge } from './UI'
import { t } from '../i18n'
import { Select } from './Select'

const modalities = ['text', 'image', 'audio', 'video', 'file']
const modalityNames: Record<string, string> = { text: '文本', image: '图片', audio: '音频', video: '视频', file: '文件' }
const operations: Record<string, { name: string; input: string[]; output: string[] }> = {
  vision: { name: '图片理解', input: ['text', 'image'], output: ['text'] },
  audio_transcription: { name: '音频转写', input: ['audio'], output: ['text'] },
  video_understanding: { name: '视频理解', input: ['video'], output: ['text'] },
  image_generation: { name: '图片生成', input: ['text'], output: ['image'] },
  image_edit: { name: '图片编辑', input: ['text', 'image'], output: ['image'] },
  speech_generation: { name: '语音生成', input: ['text'], output: ['audio'] },
  speech_to_speech: { name: '语音转语音', input: ['audio'], output: ['audio'] },
  video_generation: { name: '视频生成', input: ['text'], output: ['video'] },
}
const isSupported = (value: any) => value === true || value?.supported === true

export function ModelEditor({ model, deploymentId, onClose, onSaved }: {
  model: Entity; deploymentId?: string; onClose: () => void; onSaved?: (revision: string) => void
}) {
  const { data, mutate } = useConsole()
  const [revision] = useState(data!.revision)
  const [name, setName] = useState(model.display_name)
  const [publicName, setPublicName] = useState(model.id)
  const [enabled, setEnabled] = useState(model.enabled)
  const [yaml, setYaml] = useState(stringify(model.capabilities))
  const deployments = data!.deployments.filter(d => d.public_model === model.id)
  const [target, setTarget] = useState(deploymentId || (deployments.length === 1 ? deployments[0].id : ''))
  const [operation, setOperation] = useState<'test' | 'probe' | null>(null)
  const [test, setTest] = useState<Entity | null>(null)
  const [report, setReport] = useState<Entity | null>(null)
  const action = useAction()
  const targetDeployment = deployments.find(d => d.id === target)
  const targetUpstream = data!.upstreams.find(u => u.id === targetDeployment?.upstream_id)
  const canRun = !!targetDeployment?.enabled && !!targetUpstream?.enabled
  const stale = data!.revision !== revision
  const supported = (side: 'input' | 'output') => modalities.filter(modality => deployments.every(d => {
    const upstream = data!.upstreams.find(u => u.id === d.upstream_id)
    const adapter = data!.adapters.find(a => a.id === upstream?.adapter_id)
    return (adapter?.modalities?.[side] || (adapter?.builtin ? ['text'] : modalities)).includes(modality)
  }))
  let recorded: Entity = {}
  try {
    const evidence = JSON.parse(model.evidence)
    if (evidence.source === 'registration-probe') recorded = evidence.checks
  } catch { /* Operator declarations are plain text. */ }
  const checks: Entity = report?.checks || recorded
  const checkNames: Record<string, string> = {
    text: '文本生成', streaming: '流式输出', tools: '工具调用', 'tools.parallel_calls': '并行工具',
    structured_output: '结构化输出',
    vision: '图片输入', audio: '音频输入', video: '视频输入', file: '文件输入',
    'output.image': '图片输出', 'output.audio': '音频输出', 'output.video': '视频输出', 'output.file': '文件输出',
    limits: '上下文与上限', embedding: 'Embedding', rerank: 'Rerank',
  }
  let caps: Entity = {}
  let invalid = false
  try {
    const value = parse(yaml)
    if (!value || typeof value !== 'object' || Array.isArray(value)) invalid = true
    else caps = value
  } catch { invalid = true }
  const update = (patch: Entity) => setYaml(stringify({ ...caps, ...patch }))
  const changeModalities = (side: 'input' | 'output', selected: string[]) => {
    const input = side === 'input' ? selected : (caps.input_modalities || ['text'])
    const output = side === 'output' ? selected : (caps.output_modalities || ['text'])
    const next = { ...caps.extensions?.operations }
    for (const [key, required] of Object.entries(operations)) {
      if (!required.input.every(m => input.includes(m)) || !required.output.every(m => output.includes(m))) {
        if (key in next) next[key] = false
      }
    }
    for (const [modality, key] of [['image', 'vision'], ['audio', 'audio_transcription'], ['video', 'video_understanding']]) {
      if (side === 'input' && selected.includes(modality) && !(caps.input_modalities || ['text']).includes(modality)
        && operations[key].input.every(m => input.includes(m)) && output.includes('text')) next[key] = true
    }
    update({ [`${side}_modalities`]: selected, extensions: { ...caps.extensions, operations: next } })
  }
  const run = (kind: 'test' | 'probe') => { void action.run(async () => {
    setOperation(kind)
    try {
      const result = await mutate(`/deployments/${target}/${kind}`, 'POST', {}, false, revision)
      if (kind === 'test') setTest(result)
      else setReport(result)
    } finally { setOperation(null) }
  }) }
  const applyDetected = () => {
    const detected = report!.capabilities
    const patch: Entity = { limits: { ...caps.limits, ...detected.limits } }
    if (model.task !== 'llm') {
      if (model.task === 'embedding') patch.embedding = { ...caps.embedding,
        default_dimensions: detected.embedding.default_dimensions,
        supported_dimensions: [...new Set([...(caps.embedding?.supported_dimensions || []), ...detected.embedding.supported_dimensions])],
      }
      update(patch)
      return
    }
    for (const [check, field, disabled] of [
      ['streaming', 'streaming', false],
      ['structured_output', 'structured_output', { supported: false }],
    ] as const) {
      if (checks[check]?.status === 'supported') patch[field] = detected[field]
      if (checks[check]?.status === 'unsupported') patch[field] = disabled
    }
    if (checks.tools?.status === 'supported') {
      const parallelStatus = checks['tools.parallel_calls']?.status
      patch.tools = { ...caps.tools, ...detected.tools,
        parallel_calls: parallelStatus === 'supported' ? true
          : parallelStatus === 'unsupported' ? false : !!caps.tools?.parallel_calls,
      }
    }
    if (checks.tools?.status === 'unsupported') {
      patch.tools = { ...caps.tools, function_calling: false, parallel_calls: false }
    }
    const input = new Set<string>(caps.input_modalities || ['text'])
    if (checks.text?.status === 'supported') input.add('text')
    const next = { ...caps.extensions?.operations }
    for (const [check, modality, operation] of [
      ['vision', 'image', 'vision'], ['audio', 'audio', 'audio_transcription'],
      ['video', 'video', 'video_understanding'], ['file', 'file', ''],
    ]) {
      if (checks[check]?.status === 'supported') {
        input.add(modality)
        if (operation && (caps.output_modalities || ['text']).includes('text')) next[operation] = true
      }
      if (checks[check]?.status === 'unsupported') input.delete(modality)
    }
    for (const [key, required] of Object.entries(operations)) {
      if (!required.input.every(m => input.has(m))
        || !required.output.every(m => (caps.output_modalities || ['text']).includes(m))) {
        if (key in next) next[key] = false
      }
    }
    patch.input_modalities = [...input]
    patch.extensions = { ...caps.extensions, operations: next }
    if (detected.reasoning?.supported) patch.reasoning = { ...caps.reasoning, ...detected.reasoning,
      efforts: [...new Set([...(caps.reasoning?.efforts || []), ...detected.reasoning.efforts])],
    }
    update(patch)
  }
  const footer = <>
    <button className="btn" disabled={action.busy} onClick={onClose}>{t('取消')}</button>
    <button className="btn btn-primary" disabled={action.busy || invalid || stale || !publicName.trim()} onClick={() => { void action.run(async () => {
      const capabilities = parse(yaml)
      const value: Entity = { display_name: name, enabled }
      if (publicName !== model.id) value.id = publicName
      if (JSON.stringify(capabilities) !== JSON.stringify(model.capabilities)) value.capabilities = capabilities
      const result = await mutate(`/models/${encodeURIComponent(model.id)}`, 'PATCH', value, false, revision)
      onSaved?.(result.revision)
      onClose()
    }) }}><Save size={14} />{t('保存配置')}</button>
  </>
  return <Modal open onClose={onClose} title="编辑模型" subtitle={model.id} width={780} footer={footer}>
    <ErrorBar error={action.error} />
    {stale && <ErrorBar error="目录已更新，请关闭并重新打开编辑面板。" />}
    {invalid && <ErrorBar error="能力配置必须为 YAML 对象" />}
    <section className="model-diagnostics">
      <Field label="测试目标"><Select value={target} disabled={action.busy} onChange={value => {
        setTarget(value); setTest(null); setReport(null)
      }} options={[{ value: '', label: t(deployments.length ? '选择要测试的部署' : '此模型尚未关联部署') },
        ...deployments.map(d => ({ value: d.id, label: `${data!.upstreams.find(u => u.id === d.upstream_id)?.name} · ${d.upstream_model}${!d.enabled ? ` · ${t('已停用')}` : ''}` })),
      ]} /></Field>
      <div className="row-actions">
        <button className="btn" disabled={action.busy || !canRun || stale} onClick={() => run('test')}>
          {operation === 'test' ? <LoaderCircle size={14} className="spin" /> : <FlaskConical size={14} />}{t(operation === 'test' ? '测试中' : '测试模型')}</button>
        <button className="btn" disabled={action.busy || !canRun || stale} onClick={() => run('probe')}>
          {operation === 'probe' ? <LoaderCircle size={14} className="spin" /> : <Radar size={14} />}{t(operation === 'probe' ? '探测中' : '探测能力')}</button>
      </div>
      <p className="hint">{t('使用已保存的连接配置。测试发送一次请求，能力探测发送多次请求，产生的用量计入日志。')}</p>
      {model.task === 'llm' && <p className="hint">{t('能力探测内置音频、视频和 PDF 样本，按适配器支持范围发送并核对回答。')}</p>}
      {test && <div className="model-test-result" role="status">
        <div className="probe-result"><strong>{t(test.status === 'supported' ? '测试成功' : '测试未通过')}</strong><span className="muted">{test.latency_ms} ms{test.provider_status ? ` · HTTP ${test.provider_status}` : ''}</span></div>
        {test.preview && <pre>{test.preview}</pre>}
        {test.dimensions && <span>{t('向量维度')} · {test.dimensions}</span>}
        {test.result_count != null && <span>{t('排序结果数')} · {test.result_count}</span>}
        {test.usage && <span className="hint">{t('总 token')} · {test.usage.total_tokens ?? t('未知')}</span>}
        {test.reason !== 'response-confirmed' && <span className="hint">{test.reason}</span>}
      </div>}
    </section>
    <div className="model-edit-status"><VerifyBadge status={model.verification} /><span className="hint">{t('手动调整能力后，将保存为已声明。')}</span></div>
    <div className="form-grid two">
      <div className="span-2"><Field label="公开模型" hint="默认格式：提供商-模型厂商-模型名；可自定义，不能包含斜杠或空白。">
        <input className="input mono" maxLength={200} value={publicName} onChange={e => setPublicName(e.target.value.replace(/[\s/\\]+/g, '-'))} />
      </Field></div>
      <Field label="显示名称"><input className="input" value={name} onChange={e => setName(e.target.value)} /></Field>
      <Field label="启用状态"><Toggle checked={enabled} onChange={setEnabled} label={t('启用')} /></Field>
      {['context_window', 'max_input_tokens', 'max_output_tokens'].map((field, index) => <Field
        key={field} label={['上下文窗口', '最大输入 token', '最大输出 token'][index]}>
        <input className="input" type="number" min="1" step="1" placeholder={t('未知')} disabled={invalid}
          value={caps.limits?.[field] ?? ''} onChange={e => update({
            limits: { ...caps.limits, [field]: e.target.value === '' ? null : Number(e.target.value) },
          })} />
      </Field>)}
    </div>
    {model.task === 'llm' && <>
      <div className="model-modality-grid">{(['input', 'output'] as const).map(side => {
        const selected: string[] = caps[`${side}_modalities`] || ['text']
        const available = supported(side)
        return <fieldset className="model-modality-group" key={side}><legend>{t(side === 'input' ? '输入模态' : '输出模态')}</legend>
          <div className="model-modality-options">{modalities.map(modality => {
            const implemented = available.includes(modality)
            return <label className={`model-modality-option${implemented ? '' : ' unavailable'}`} key={modality}
              title={implemented ? undefined : t('关联适配器尚未实现此模态')}>
              <input type="checkbox" aria-label={t(`${modalityNames[modality]}${side === 'input' ? '输入' : '输出'}`)}
                disabled={invalid || !implemented || selected.length === 1 && selected.includes(modality)}
                checked={selected.includes(modality)} onChange={e => changeModalities(side, e.target.checked ? [...selected, modality] : selected.filter(m => m !== modality))} />
              <span>{t(modalityNames[modality])}</span>{!implemented && <span className="muted">{t('未实现')}</span>}
            </label>
          })}</div>
        </fieldset>
      })}</div>
      <div className="model-capability-grid">
        <Toggle checked={!!caps.streaming} disabled={invalid} label={t('流式输出')} onChange={value => update({ streaming: value })} />
        <Toggle checked={!!caps.tools?.function_calling} disabled={invalid} label={t('工具调用')} onChange={value => update({
          tools: value ? { ...caps.tools, function_calling: true } : { ...caps.tools, function_calling: false, parallel_calls: false },
        })} />
        <Toggle checked={!!caps.tools?.function_calling && !!caps.tools?.parallel_calls}
          disabled={invalid || !caps.tools?.function_calling} label={t('并行工具')} onChange={value => update({
          tools: { ...caps.tools, function_calling: true, parallel_calls: value },
        })} />
        <Toggle checked={!!caps.structured_output?.supported} disabled={invalid} label={t('结构化输出')} onChange={value => update({
          structured_output: value ? { supported: true, strict: false, schema_subset: ['json_schema'] } : { supported: false },
        })} />
      </div>
      <details className="edit-section"><summary>{t('媒体操作')}</summary>
        <div className="model-operation-grid">{Object.entries(operations).map(([key, required]) => <Toggle key={key}
          checked={isSupported(caps.extensions?.operations?.[key])} label={t(required.name)}
          disabled={invalid || !required.input.every(m => (caps.input_modalities || ['text']).includes(m) && supported('input').includes(m))
            || !required.output.every(m => (caps.output_modalities || ['text']).includes(m) && supported('output').includes(m))}
          onChange={value => update({ extensions: { ...caps.extensions, operations: { ...caps.extensions?.operations, [key]: value } } })} />)}</div>
      </details>
    </>}
    {!!Object.keys(checks).length && <details className="edit-section" open={report ? true : undefined}><summary>{t(report ? '本次探测结果' : '检测记录')}</summary>
      {report && <div className="model-probe-actions">
        <span className="hint">{t(deployments.length > 1 ? '此模型有多个部署，单个部署的探测结果仅供参考，请确认全部部署后手动调整。' : '应用已确认结果会更新当前表单，未知项保留原值，保存配置后生效。')}</span>
        <button className="btn btn-sm" disabled={action.busy || invalid || stale || deployments.length !== 1 || report.verification !== 'verified'} onClick={applyDetected}>{t('应用已确认能力')}</button>
      </div>}
      <div className="probe-results">{Object.entries(checks).map(([key, value]) => <div className="probe-result" key={key}>
        <span>{t(checkNames[key] || (key.startsWith('reasoning.') ? '推理能力' : key))}{key.startsWith('reasoning.') ? ` · ${key.split('.')[1]}` : ''}</span>
        <span className="muted">{t(value.reason === 'adapter-not-implemented' ? '适配器未实现'
          : value.reason === 'asset-access-unavailable' ? '扩展尚不支持样本访问'
          : value.reason === 'basic-test-failed' ? '基础测试未通过，未探测'
          : value.reason === 'response-not-confirmed' ? '回答未通过验证'
          : value.reason === 'sample-required' ? '需要样本验证'
          : value.status === 'supported' ? '已验证' : value.status === 'declared' ? '已声明' : value.status === 'unsupported' ? '不支持' : '未知')}
          {value.provider_status ? ` · HTTP ${value.provider_status}` : ''}</span>
      </div>)}</div>
    </details>}
    <details className="edit-section"><summary>{t('高级能力配置')}</summary>
      <textarea className="input mono config-editor model-capability-yaml" aria-label={t('能力声明')} value={yaml} spellCheck={false} onChange={e => setYaml(e.target.value)} />
    </details>
  </Modal>
}
