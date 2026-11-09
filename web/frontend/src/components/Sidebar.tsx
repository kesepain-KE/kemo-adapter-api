import { NavLink } from 'react-router-dom'
import {
  Activity,
  Boxes,
  FileStack,
  GitBranch,
  KeyRound,
  LayoutDashboard,
  PanelLeft,
  Plug,
  ScrollText,
  Server,
  Settings,
} from 'lucide-react'
import { useConsole } from '../console'
import { t } from '../i18n'

type Item = { to: string; label: string; icon: typeof Server; badge?: string }
const logo = new URL('../../../../llm-adapter-kemo.ico', import.meta.url).href

const primary: Item[] = [
  { to: '/', label: '总览', icon: LayoutDashboard },
  { to: '/upstreams', label: '上游服务', icon: Server },
  { to: '/models', label: '模型管理', icon: Boxes },
  { to: '/routing', label: '路由策略', icon: GitBranch },
  { to: '/keys', label: '下游密钥', icon: KeyRound },
  { to: '/logs', label: '调用记录', icon: ScrollText },
]

const manage: Item[] = [
  { to: '/adapters', label: '协议适配器', icon: Plug },
  { to: '/templates', label: '提供商模板', icon: FileStack },
  { to: '/diagnostics', label: '运行诊断', icon: Activity },
  { to: '/settings', label: '设置', icon: Settings },
]

export function Sidebar({
  compact,
  onToggle,
  mobileOpen,
  onNavigate,
}: {
  compact: boolean
  onToggle: () => void
  mobileOpen: boolean
  onNavigate: () => void
}) {
  const { data } = useConsole()
  const renderItem = (item: Item) => {
    const Icon = item.icon
    return (
      <NavLink
        key={item.to}
        to={item.to}
        end={item.to === '/'}
        title={compact ? t(item.label) : undefined}
        className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
        onClick={onNavigate}
      >
        <Icon size={16} strokeWidth={1.8} />
        <span className="label">{t(item.label)}</span>
        {item.badge ? <span className="nav-badge">{item.badge}</span> : null}
      </NavLink>
    )
  }

  return (
    <aside className={`sidebar${compact ? ' compact' : ''}${mobileOpen ? ' open' : ''}`}>
      <div className="brand">
        <button type="button" className="brand-icon" onClick={onToggle} title={t('折叠侧栏')} aria-label={t('折叠侧栏')}>
          <img src={logo} alt="Kemo Gateway" width="36" height="36" />
        </button>
        <div className="brand-info">
          <span className="brand-name">Kemo Gateway</span>
          <span className="brand-version">v{data!.version.version} <span>· Kemo 2.0</span></span>
        </div>
        <button type="button" className="brand-collapse" onClick={onToggle} title={t('折叠侧栏')} aria-label={t('折叠侧栏')}>
          <PanelLeft size={15} strokeWidth={1.8} />
        </button>
      </div>

      <nav className="nav">
        {primary.map(renderItem)}
      </nav>

      <div className="sidebar-sep" />

      <div className="nav-group">
        {manage.map(renderItem)}
      </div>

    </aside>
  )
}
