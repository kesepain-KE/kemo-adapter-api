import type { ReactNode } from 'react'
import { CheckCircle2, CircleAlert, CircleOff, CircleHelp } from 'lucide-react'

export function Badge({ children, tone = 'primary' }: { children: ReactNode; tone?: 'primary' | 'success' | 'warning' | 'danger' | 'muted' }) {
  return <span className={`badge badge-${tone}`}>{children}</span>
}

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <section className={`card ${className}`}>{children}</section>
}

export function CardHeader({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return (
    <header className="card-header">
      <div><h3>{title}</h3>{description && <p>{description}</p>}</div>
      {action}
    </header>
  )
}

export function SectionTitle({ title, description, eyebrow, action }: { title: string; description?: string; eyebrow?: string; action?: ReactNode }) {
  return (
    <header className="section-title">
      <div><h2>{title}</h2>{description && <p>{description}</p>}</div>
      {(eyebrow || action) && <div className="section-meta">
        {eyebrow && <span className="section-eyebrow">{eyebrow}</span>}
        {action && <div className="section-actions">{action}</div>}
      </div>}
    </header>
  )
}

export function StatusIcon({ status }: { status: 'healthy' | 'warning' | 'disabled' | 'unconfigured' }) {
  if (status === 'healthy') return <CheckCircle2 size={16} />
  if (status === 'warning') return <CircleAlert size={16} />
  if (status === 'disabled') return <CircleOff size={16} />
  return <CircleHelp size={16} />
}

export function Subtabs<T extends string>({ items, value, onChange }: { items: { id: T; label: string }[]; value: T; onChange: (value: T) => void }) {
  return <div className="subtabs">{items.map(item => <button key={item.id} className={value === item.id ? 'active' : ''} onClick={() => onChange(item.id)}>{item.label}</button>)}</div>
}

export function EmptyState({ title, description }: { title: string; description: string }) {
  return <Card className="empty-state"><div className="empty-orb" /><h3>{title}</h3><p>{description}</p></Card>
}
