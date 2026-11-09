import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { Check, Copy, PanelLeft } from 'lucide-react'
import { useConsole } from '../console'
import { useToast } from './UI'
import { t } from '../i18n'

const TITLES: Record<string, string> = {
  '/': '总览',
  '/upstreams': '上游服务',
  '/models': '模型管理',
  '/routing': '路由策略',
  '/keys': '下游密钥',
  '/logs': '调用记录',
  '/adapters': '协议适配器',
  '/templates': '提供商模板',
  '/diagnostics': '运行诊断',
  '/settings': '设置',
}
const PHASES: Record<string, string> = { running: '运行中', starting: '启动中', draining: '排空中', stopping: '停止中' }

export function TopBar({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const { pathname } = useLocation()
  const title = TITLES[pathname] ?? '总览'
  const { data } = useConsole()
  const toast = useToast()
  const [copied, setCopied] = useState(false)
  const copyTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(copyTimer.current), [])
  const configured = data!.startup.base_url.trim()
  const address = new URL(configured || window.location.origin)
  if (['0.0.0.0', '[::]', '::'].includes(address.hostname)) address.hostname = window.location.hostname
  const endpoint = address.href.replace(/\/+$/, '')
  const phase = data!.runtime.phase
  const phaseColor = phase === 'running' ? 'green' : phase === 'stopping' ? 'gray' : 'amber'

  async function copyAddress() {
    try {
      if (navigator.clipboard) await navigator.clipboard.writeText(endpoint)
      else {
        const input = document.createElement('textarea')
        input.value = endpoint
        input.className = 'clipboard-input'
        document.body.appendChild(input)
        try {
          input.select()
          if (!document.execCommand('copy')) throw new Error(t('浏览器拒绝访问剪贴板'))
        } finally { input.remove() }
      }
      setCopied(true)
      window.clearTimeout(copyTimer.current)
      copyTimer.current = window.setTimeout(() => setCopied(false), 2000)
      toast(t('Kemo 地址已复制'))
    } catch { toast(t('浏览器拒绝访问剪贴板')) }
  }

  return (
    <header className="topbar">
      <button type="button" className="icon-btn menu-toggle" onClick={onToggleSidebar} aria-label={t('折叠侧栏')} title={t('折叠侧栏')}>
        <PanelLeft size={16} strokeWidth={1.8} />
      </button>
      <div className="crumbs">
        <span className="crumb">Kemo Gateway</span>
        <span className="crumb-sep">/</span>
        <span className="crumb is-last">{t(title)}</span>
      </div>
      <div className="top-actions">
        <button type="button" className={`gateway-address${copied ? ' copied' : ''}`}
          onClick={() => { void copyAddress() }} title={`${t('复制 Kemo 地址')} · ${endpoint}`}
          aria-label={`${t('复制 Kemo 地址')} ${endpoint}`}>
          <span className="address-label">Kemo</span><span className="mono address-value">{endpoint}</span>
          {copied ? <Check size={13} /> : <Copy size={13} />}
        </button>
        <span className={`runtime-status ${phaseColor}`} role="status">
          <span className={`dot dot-${phaseColor}`} />
          <span className="runtime-phase">{t(PHASES[phase] ?? phase)}</span>
          <span className="runtime-active" title={t('在途')}>{data!.runtime.active_executions} <span>{t('在途')}</span></span>
        </span>
      </div>
    </header>
  )
}
