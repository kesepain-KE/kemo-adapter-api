import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Check, X } from 'lucide-react'
import type { Health, Verification } from '../data/types'
import { t } from '../i18n'

/* ------------------------------ toasts ------------------------------ */

type ToastItem = { id: number; text: string }
type ToastFn = (text: string) => void

const ToastContext = createContext<ToastFn>(() => {})

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const nextId = useRef(1)
  const timers = useRef<number[]>([])

  const push = useCallback<ToastFn>((text) => {
    const id = nextId.current++
    setItems((prev) => [...prev, { id, text }])
    const timer = window.setTimeout(() => {
      setItems((prev) => prev.filter((item) => item.id !== id))
    }, 2600)
    timers.current.push(timer)
  }, [])

  useEffect(() => () => {
    for (const timer of timers.current) window.clearTimeout(timer)
  }, [])

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toast-wrap" aria-live="polite">
        {items.map((item) => (
          <div className="toast" key={item.id}>
            <Check size={14} strokeWidth={2.4} />
            {item.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastFn {
  return useContext(ToastContext)
}

/* ------------------------------ page header ------------------------------ */

export function PageHeader({
  title,
  desc,
  actions,
  meta,
}: {
  title: string
  desc?: string
  actions?: ReactNode
  meta?: ReactNode
}) {
  return (
    <header className="page-head">
      <div>
        <h1 className="page-title">{t(title)}</h1>
        {desc ? <p className="page-desc">{t(desc)}</p> : null}
        {meta ? <div className="page-meta">{meta}</div> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  )
}

/* ------------------------------ status ------------------------------ */

const HEALTH_LABEL: Record<Health, string> = {
  healthy: '正常',
  cooldown: '冷却中',
  exhausted: '已耗尽',
  disabled: '已禁用',
}

const HEALTH_DOT: Record<Health, string> = {
  healthy: 'dot-green',
  cooldown: 'dot-amber',
  exhausted: 'dot-red',
  disabled: 'dot-gray',
}

export function HealthDot({ status }: { status: Health }) {
  return <span className={`dot ${HEALTH_DOT[status]}`} />
}

export function HealthStatus({ status }: { status: Health }) {
  return (
    <span className="status">
      <HealthDot status={status} />
      {t(HEALTH_LABEL[status])}
    </span>
  )
}

const VERIFY_LABEL: Record<Verification, string> = {
  verified: '已验证',
  declared: '已声明',
  unknown: '未知',
  unsupported: '不支持',
}

const VERIFY_CLASS: Record<Verification, string> = {
  verified: 'green',
  declared: 'amber',
  unknown: '',
  unsupported: 'red',
}

export function VerifyBadge({ status }: { status: Verification }) {
  return <span className={`tag ${VERIFY_CLASS[status]}`}>{t(VERIFY_LABEL[status])}</span>
}

/* ------------------------------ toggle ------------------------------ */

export function Toggle({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean
  onChange: (value: boolean) => void
  disabled?: boolean
  label?: string
}) {
  const control = (
    <button
      type="button"
      className={`toggle${checked ? ' on' : ''}`}
      aria-pressed={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    />
  )
  if (!label) return control
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      {control}
      <span style={{ fontSize: 11.5, color: 'var(--text-2)' }}>{label}</span>
    </span>
  )
}

/* ------------------------------ modal ------------------------------ */

export function Modal({
  open,
  onClose,
  title,
  subtitle,
  children,
  footer,
  width,
}: {
  open: boolean
  onClose: () => void
  title: string
  subtitle?: string
  children: ReactNode
  footer?: ReactNode
  width?: number
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  return (
    <div className="overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <div className="modal" style={width ? { maxWidth: width } : undefined} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <div style={{ minWidth: 0 }}>
            <div className="modal-title">{t(title)}</div>
            {subtitle ? <div className="modal-sub">{t(subtitle)}</div> : null}
          </div>
          <button type="button" className="icon-btn" style={{ marginLeft: 'auto' }} onClick={onClose} aria-label={t('关闭')}>
            <X size={15} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer ? <div className="modal-foot">{footer}</div> : null}
      </div>
    </div>
  )
}

/* ------------------------------ misc ------------------------------ */

export function EmptyState({ title, desc, icon }: { title: string; desc?: string; icon?: ReactNode }) {
  return (
    <div className="empty">
      {icon ? <div className="empty-icon">{icon}</div> : null}
      <div className="empty-title">{title}</div>
      {desc ? <div className="empty-desc">{desc}</div> : null}
    </div>
  )
}

export function useDelayedBusy(): [boolean, (run: () => void, ms?: number) => void] {
  const [busy, setBusy] = useState(false)
  const run = useCallback((action: () => void, ms = 600) => {
    setBusy(true)
    window.setTimeout(() => {
      setBusy(false)
      action()
    }, ms)
  }, [])
  return [busy, run]
}

export function useNow(interval = 30_000): Date {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), interval)
    return () => window.clearInterval(timer)
  }, [interval])
  return now
}
