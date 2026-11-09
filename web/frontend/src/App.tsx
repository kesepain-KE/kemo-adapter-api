import { useEffect, useState } from 'react'
import { HashRouter, Route, Routes } from 'react-router-dom'
import { Sidebar } from './components/Sidebar'
import { TopBar } from './components/TopBar'
import { ToastProvider } from './components/UI'
import Dashboard from './pages/Dashboard'
import Upstreams from './pages/Upstreams'
import Models from './pages/Models'
import Routing from './pages/Routing'
import Keys from './pages/Keys'
import Logs from './pages/Logs'
import Adapters from './pages/Adapters'
import Templates from './pages/Templates'
import Diagnostics from './pages/Diagnostics'
import Settings from './pages/Settings'
import Login from './pages/Login'
import AdvancedPlugins from './pages/AdvancedPlugins'
import { ApiError, ConsoleProvider, useConsole } from './console'
import { useLanguage, t } from './i18n'

function Shell() {
  const { data, error, refresh } = useConsole()
  useLanguage()
  const [compact, setCompact] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)

  useEffect(() => {
    const onResize = () => {
      if (window.innerWidth > 960) setMobileOpen(false)
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const toggleSidebar = () => {
    if (window.innerWidth <= 960) {
      setMobileOpen((value) => !value)
      return
    }
    setCompact((value) => !value)
  }

  if (error instanceof ApiError && error.status === 401) return <Login />
  if (!data) return <div className="empty"><p>{error?.message || t('正在加载')}</p><button className="btn" onClick={() => { void refresh().catch(() => {}) }}>{t('重试')}</button></div>
  return (
    <div className={`app${compact ? ' compact' : ''}`}>
      <Sidebar
        compact={compact}
        onToggle={toggleSidebar}
        mobileOpen={mobileOpen}
        onNavigate={() => setMobileOpen(false)}
      />
      <div className="main">
        <TopBar onToggleSidebar={toggleSidebar} />
        <main className="canvas">
          {error && <div className="hint-bar error-bar" role="alert">{error.message}</div>}
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/upstreams" element={<Upstreams />} />
            <Route path="/models" element={<Models />} />
            <Route path="/advanced-plugins" element={<AdvancedPlugins />} />
            <Route path="/routing" element={<Routing />} />
            <Route path="/keys" element={<Keys />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/adapters" element={<Adapters />} />
            <Route path="/templates" element={<Templates />} />
            <Route path="/diagnostics" element={<Diagnostics />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<Dashboard />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}

export default function App() {
  return (
    <HashRouter>
      <ToastProvider>
        <ConsoleProvider><Shell /></ConsoleProvider>
      </ToastProvider>
    </HashRouter>
  )
}
