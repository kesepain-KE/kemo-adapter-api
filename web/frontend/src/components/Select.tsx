import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import type { CSSProperties, KeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown } from 'lucide-react'

export type SelectOption = { value: string; label: string; description?: string; disabled?: boolean }

export function Select({ value, options, onChange, disabled, id: suppliedId, className = '', 'aria-label': label }: {
  value: string; options: SelectOption[]; onChange: (value: string) => void
  disabled?: boolean; id?: string; className?: string; 'aria-label'?: string
}) {
  const generatedId = useId()
  const id = suppliedId || generatedId
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [position, setPosition] = useState<CSSProperties>({})
  const trigger = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const search = useRef({ text: '', at: 0 })
  const selected = options.findIndex(option => option.value === value)
  const close = () => setOpen(false)
  const show = () => { setActive(Math.max(0, selected)); setOpen(true) }
  const choose = (index: number) => {
    const option = options[index]
    if (!option || option.disabled) return
    onChange(option.value); close(); trigger.current?.focus()
  }
  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const rect = trigger.current!.getBoundingClientRect()
      const below = window.innerHeight - rect.bottom - 12
      const above = rect.top - 12
      const upwards = below < 220 && above > below
      setPosition({
        position: 'fixed', left: Math.max(8, Math.min(rect.left, window.innerWidth - Math.max(rect.width, 180) - 8)),
        width: Math.min(Math.max(rect.width, 180), window.innerWidth - 16),
        maxHeight: Math.min(340, upwards ? above : below),
        ...(upwards ? { bottom: window.innerHeight - rect.top + 6 } : { top: rect.bottom + 6 }),
      })
    }
    place()
    const outside = (event: PointerEvent) => {
      if (!trigger.current?.contains(event.target as Node) && !menu.current?.contains(event.target as Node)) close()
    }
    document.addEventListener('pointerdown', outside)
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      document.removeEventListener('pointerdown', outside)
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open])
  useEffect(() => {
    if (open) menu.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [open, active])
  useEffect(() => { if (disabled) close() }, [disabled])
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'Escape' && open) {
      event.preventDefault(); event.stopPropagation(); close(); return
    }
    if (event.key === 'Tab') { close(); return }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault()
      if (!open) { show(); return }
      const direction = event.key === 'ArrowUp' || event.key === 'End' ? -1 : 1
      let index = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : active + direction
      while (index >= 0 && index < options.length && options[index].disabled) index += direction
      if (index >= 0 && index < options.length) setActive(index)
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      if (open) choose(active); else show()
    } else if (event.key.length === 1) {
      const now = Date.now()
      search.current = { text: (now - search.current.at < 700 ? search.current.text : '') + event.key.toLowerCase(), at: now }
      const index = options.findIndex(option => !option.disabled && option.label.toLowerCase().startsWith(search.current.text))
      if (index >= 0) { setActive(index); setOpen(true) }
    }
  }
  return <>
    <button ref={trigger} id={id} type="button" className={`select theme-select ${className}`} role="combobox"
      aria-label={label} aria-expanded={open} aria-haspopup="listbox" aria-controls={`${id}-menu`}
      aria-activedescendant={open ? `${id}-option-${active}` : undefined} disabled={disabled}
      onClick={() => open ? close() : show()} onKeyDown={onKeyDown}>
      <span>{options[selected]?.label || value || '—'}</span><ChevronDown size={14} />
    </button>
    {open && createPortal(<div ref={menu} id={`${id}-menu`} role="listbox" aria-labelledby={id}
      className="select-menu" style={position}>
      {options.map((option, index) => <div key={option.value} id={`${id}-option-${index}`} data-index={index}
        role="option" aria-selected={option.value === value} aria-disabled={option.disabled || undefined}
        className={`select-option${index === active ? ' active' : ''}${option.value === value ? ' selected' : ''}`}
        onMouseDown={event => event.preventDefault()} onMouseMove={() => { if (!option.disabled) setActive(index) }}
        onClick={() => choose(index)}>
        <span><span className="select-option-label">{option.label}</span>
          {option.description && <span className="select-option-description">{option.description}</span>}</span>
        {option.value === value && <Check size={16} />}
      </div>)}
    </div>, document.fullscreenElement || document.body)}
  </>
}
