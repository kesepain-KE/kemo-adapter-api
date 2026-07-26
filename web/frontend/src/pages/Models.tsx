import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, CheckCircle2, ChevronDown, CircleX, Copy, LoaderCircle, RadioTower, Search } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import {
  adminApi,
  type ModelCapabilityDeclaration,
  type ModelProbeResult,
} from '../adminApi'
import { Card, EmptyState, Subtabs } from '../components/UI'

interface RegisteredModel {
  id: string
  providerId: string
  capability?: ModelCapabilityDeclaration
  upstreamName: string | null
}

function capabilityTags(capability?: ModelCapabilityDeclaration): string[] {
  if (!capability) return []
  const values = [
    ...capability.input_modalities.map(value => `输入 · ${value}`),
    ...capability.output_modalities.map(value => `输出 · ${value}`),
  ]
  if (capability.streaming) values.push('流式输出')
  if (capability.reasoning.supported) values.push('推理')
  if (capability.tools.function_calling) values.push('工具调用')
  if (capability.structured_output) values.push('结构化输出')
  return [...new Set(values)]
}

export default function Models() {
  const { token, data, saveControl } = useAdmin()
  const [tab, setTab] = useState<'all' | 'enabled' | 'disabled'>('all')
  const [providerFilter, setProviderFilter] = useState('all')
  const [providerMenuOpen, setProviderMenuOpen] = useState(false)
  const providerMenuRef = useRef<HTMLDivElement>(null)
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState('')
  const [testing, setTesting] = useState('')
  const [copied, setCopied] = useState('')
  const [error, setError] = useState('')
  const [capabilityError, setCapabilityError] = useState('')
  const [capabilities, setCapabilities] = useState<Record<string, ModelCapabilityDeclaration>>({})
  const [probeResults, setProbeResults] = useState<Record<string, ModelProbeResult>>({})
  const providerKey = data.providers.map(provider => provider.provider_id).sort().join('|')

  useEffect(() => {
    if (!providerMenuOpen) return

    const closeOnOutsideClick = (event: PointerEvent) => {
      if (!providerMenuRef.current?.contains(event.target as Node)) setProviderMenuOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setProviderMenuOpen(false)
    }

    document.addEventListener('pointerdown', closeOnOutsideClick)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsideClick)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [providerMenuOpen])

  useEffect(() => {
    let active = true
    const load = async () => {
      const results = await Promise.allSettled(
        data.providers.map(provider => adminApi.providerCapabilities(token, provider.provider_id)),
      )
      if (!active) return
      const next: Record<string, ModelCapabilityDeclaration> = {}
      results.forEach(result => {
        if (result.status === 'fulfilled') {
          result.value.models.forEach(model => { next[model.model] = model })
        }
      })
      setCapabilities(next)
      setCapabilityError(results.some(result => result.status === 'rejected') ? '部分模型能力声明读取失败；页面未显示推测能力。' : '')
    }
    void load()
    return () => { active = false }
    // 只在实际厂商集合变化时重新读取能力，避免控制台轮询产生重复请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providerKey, token])

  const models = useMemo<RegisteredModel[]>(() => data.providers.flatMap(provider => provider.models.map(id => {
    const capability = capabilities[id]
    const upstream = capability?.metadata.upstream_model
    return {
      id,
      providerId: provider.provider_id,
      capability,
      upstreamName: typeof upstream === 'string' && upstream.trim() ? upstream : null,
    }
  })), [capabilities, data.providers])

  const modelState = (model: RegisteredModel) => ({
    providerDisabled: data.disabled_providers.includes(model.providerId),
    modelDisabled: data.disabled_models.includes(model.id),
  })

  const visible = useMemo(() => {
    const eligible = models.filter(model => {
      const state = modelState(model)
      const disabled = state.providerDisabled || state.modelDisabled
      return (tab === 'all' || (tab === 'enabled' ? !disabled : disabled))
        && (providerFilter === 'all' || model.providerId === providerFilter)
    })
    const normalized = query.trim().toLowerCase()
    if (!normalized) return eligible
    const primary = eligible.filter(model => model.id.toLowerCase().includes(normalized))
    const primaryIds = new Set(primary.map(model => model.id))
    const secondary = eligible.filter(model => !primaryIds.has(model.id) && model.upstreamName?.toLowerCase().includes(normalized))
    return [...primary, ...secondary]
    // modelState reads the current live disable arrays.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.disabled_models, data.disabled_providers, models, providerFilter, query, tab])

  const toggle = async (modelId: string) => {
    const disabledModels = data.disabled_models.includes(modelId)
      ? data.disabled_models.filter(value => value !== modelId)
      : [...data.disabled_models, modelId]
    setBusy(modelId)
    setError('')
    try {
      await saveControl(data.highest_priority_system_prompt, data.disabled_providers, disabledModels)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败')
    } finally { setBusy('') }
  }

  const probe = async (modelId: string) => {
    setTesting(modelId)
    setError('')
    try {
      const result = await adminApi.probeModel(token, modelId)
      setProbeResults(current => ({ ...current, [modelId]: result }))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '可达性测试失败')
    } finally { setTesting('') }
  }

  const copyModelName = async (modelId: string) => {
    setError('')
    try {
      await navigator.clipboard.writeText(modelId)
      setCopied(modelId)
      window.setTimeout(() => setCopied(current => current === modelId ? '' : current), 1600)
    } catch {
      setError('浏览器拒绝访问剪贴板，请手动复制模型名。')
    }
  }

  return <>
    <div className="model-filter-bar">
      <Subtabs items={[{ id: 'all', label: '全部模型' }, { id: 'enabled', label: '可用模型' }, { id: 'disabled', label: '不可用模型' }]} value={tab} onChange={setTab}/>
      <div className="provider-filter" ref={providerMenuRef}>
        <span>厂商</span>
        <div className={`provider-select ${providerMenuOpen ? 'open' : ''}`}>
          <button
            type="button"
            className="provider-select-trigger"
            aria-haspopup="listbox"
            aria-expanded={providerMenuOpen}
            onClick={() => setProviderMenuOpen(open => !open)}
          >
            <span>{providerFilter === 'all' ? '全部厂商' : providerFilter}</span>
            <ChevronDown size={14}/>
          </button>
          <div className="provider-select-popover" role="listbox" aria-hidden={!providerMenuOpen}>
            {[{ provider_id: 'all', label: '全部厂商' }, ...data.providers.map(provider => ({ provider_id: provider.provider_id, label: provider.provider_id }))].map(provider => {
              const selected = providerFilter === provider.provider_id
              return <button
                type="button"
                key={provider.provider_id}
                className={`provider-select-option ${selected ? 'selected' : ''}`}
                role="option"
                aria-selected={selected}
                tabIndex={providerMenuOpen ? 0 : -1}
                onClick={() => {
                  setProviderFilter(provider.provider_id)
                  setProviderMenuOpen(false)
                }}
              >
                <span>{provider.label}</span>
                <Check size={14}/>
              </button>
            })}
          </div>
        </div>
      </div>
    </div>
    {error && <div className="global-alert"><span>{error}</span><button onClick={() => setError('')}>×</button></div>}
    {capabilityError && <div className="alert"><span>{capabilityError}</span></div>}
    <div className="searchbar"><Search size={17}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索网关模型名或厂商原始模型名…"/><span>{visible.length} 个结果</span></div>
    {!visible.length ? <EmptyState title="没有匹配的模型" description="先匹配网关完整模型名，未命中时再匹配厂商原始模型名。"/> : <div className="model-grid enhanced-model-grid">{visible.map(model => {
      const { providerDisabled, modelDisabled } = modelState(model)
      const enabled = !providerDisabled && !modelDisabled
      const probeResult = probeResults[model.id]
      const tags = capabilityTags(model.capability)
      return <Card key={model.id} className={`enhanced-model-card ${!enabled ? 'model-disabled' : ''}`}>
        <div className="model-head"><div className="model-copy"><strong title={model.id}>{model.id}</strong><span>厂商 · {model.providerId}</span></div><button className={`toggle ${enabled ? 'on' : ''}`} disabled={providerDisabled || busy === model.id} onClick={() => void toggle(model.id)} aria-label={`${enabled ? '禁用' : '启用'} ${model.id}`}><i/></button></div>
        <div className="model-facts"><div><span>厂商名字</span><strong>{model.providerId}</strong></div><div><span>厂商原始模型名</span><strong title={model.upstreamName ?? 'Provider 未声明'}>{model.upstreamName ?? '未声明'}</strong></div><div><span>模型任务</span><strong>{model.capability?.task ?? '未声明'}</strong></div></div>
        <div className="model-capabilities"><span>多模态能力声明</span>{tags.length ? <div className="tags">{tags.map(value => <span key={value}>{value}</span>)}</div> : <p>Provider 尚未返回可展示的能力声明。</p>}</div>
        <div className="model-policy-row"><span>Provider <b>{providerDisabled ? '已禁用' : '已启用'}</b></span><button className={`btn model-copy-name ${copied === model.id ? 'copied' : ''}`} onClick={() => void copyModelName(model.id)} title={model.id}>{copied === model.id ? <Check size={14}/> : <Copy size={14}/>} {copied === model.id ? '已复制' : '复制模型名'}</button><button className={`btn primary model-probe ${probeResult ? (probeResult.reachable ? 'probe-success' : 'probe-danger') : ''}`} disabled={!enabled || testing === model.id} onClick={() => void probe(model.id)} title={probeResult ? '点击重新测试可达性' : undefined}>{testing === model.id ? <LoaderCircle className="spin" size={14}/> : probeResult?.reachable ? <CheckCircle2 size={14}/> : probeResult ? <CircleX size={14}/> : <RadioTower size={14}/>} {testing === model.id ? '测试中' : probeResult?.reachable ? `可达 · ${probeResult.latency_ms} ms` : probeResult ? `不可达 · ${probeResult.error_code ?? probeResult.status}` : '测试可达性'}</button></div>
        {!enabled && <div className="model-card-footer"><small>{providerDisabled ? '请先启用厂商' : '请先启用模型'}</small></div>}
      </Card>
    })}</div>}
  </>
}
