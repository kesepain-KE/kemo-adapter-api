import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { adminApi, AdminApiError, type AdminConsoleData, type RestartStatus } from './adminApi'

interface AdminContextValue {
  csrfToken: string
  data: AdminConsoleData
  restart: RestartStatus | null
  isOwner: boolean
  refreshing: boolean
  error: string
  clearError: () => void
  refresh: () => Promise<void>
  disconnect: () => void
  saveRuntime: (enabled: boolean, prompt: string, disabledProviders: string[], disabledModels: string[]) => Promise<void>
  saveControl: (prompt: string, disabledProviders: string[], disabledModels: string[]) => Promise<void>
  saveProvider: (providerId: string, config: Record<string, unknown>, apiKey?: string) => Promise<void>
  requestRestart: (reason: string, force?: boolean) => Promise<string>
}

interface AdminSessionValue {
  booting: boolean
  data: AdminConsoleData | null
  error: string
  connect: (csrfToken?: string, allowEmpty?: boolean) => Promise<boolean>
}

const AdminContext = createContext<AdminContextValue | null>(null)
const AdminSessionContext = createContext<AdminSessionValue | null>(null)

export function AdminProvider({ children }: { children: ReactNode }) {
  const [csrfToken, setCsrfToken] = useState('')
  const [data, setData] = useState<AdminConsoleData | null>(null)
  const [restart, setRestart] = useState<RestartStatus | null>(null)
  const [isOwner, setIsOwner] = useState(false)
  const [booting, setBooting] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const csrfTokenRef = useRef(csrfToken)
  csrfTokenRef.current = csrfToken

  const disconnect = useCallback(() => {
    void adminApi.logout().catch(() => undefined)
    setCsrfToken('')
    setData(null)
    setRestart(null)
    setIsOwner(false)
    setError('')
  }, [])

  const loadRestart = useCallback(async (candidate: string) => {
    try {
      const value = await adminApi.restartStatus(candidate)
      setRestart(value)
      setIsOwner(true)
    } catch (reason) {
      if (reason instanceof AdminApiError && reason.status === 403) {
        setRestart(null)
        setIsOwner(false)
        return
      }
      throw reason
    }
  }, [])

  const connect = useCallback(async (candidate = '', allowEmpty = false) => {
    const normalized = candidate.trim()
    if (!normalized && !allowEmpty) {
      setError('管理会话尚未建立。')
      return false
    }
    setBooting(true)
    setError('')
    try {
      const consoleData = await adminApi.console(normalized)
      setCsrfToken(normalized)
      csrfTokenRef.current = normalized
      setData(consoleData)
      setIsOwner(consoleData.permissions.can_restart)
      if (consoleData.permissions.can_restart) {
        try { await loadRestart(normalized) } catch { setRestart(null) }
      } else {
        setRestart(null)
      }
      return true
    } catch (reason) {
      setData(null)
      setError(normalized ? (reason instanceof Error ? reason.message : '无法连接管理 API') : '')
      return false
    } finally {
      setBooting(false)
    }
  }, [loadRestart])

  useEffect(() => {
    void adminApi.webAuthSession()
      .then(session => connect(session.csrf_token, true))
      .catch(() => connect('', true))
    // 仅在首次挂载时通过 HttpOnly Cookie 恢复服务端会话。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const refresh = useCallback(async () => {
    const currentToken = csrfTokenRef.current
    setRefreshing(true)
    try {
      const consoleData = await adminApi.console(currentToken)
      setData(consoleData)
      setError(consoleData.live_config_error ? '运行时配置有误，当前使用最后一个有效版本。' : '')
      if (isOwner) {
        try { setRestart(await adminApi.restartStatus(currentToken)) } catch { /* 重启切换端口时保留上次状态 */ }
      }
    } catch (reason) {
      if (reason instanceof AdminApiError && reason.status === 401) disconnect()
      else setError(reason instanceof Error ? reason.message : '刷新失败')
    } finally {
      setRefreshing(false)
    }
  }, [disconnect, isOwner])

  useEffect(() => {
    if (!data) return
    const timer = window.setInterval(() => { void refresh() }, restart && !['idle', 'succeeded', 'failed'].includes(restart.restart.phase) ? 1000 : 15000)
    return () => window.clearInterval(timer)
  }, [data, refresh, restart])

  const saveRuntime = async (enabled: boolean, prompt: string, disabledProviders: string[], disabledModels: string[]) => {
    if (!data) return
    setError('')
    try {
      const gateway = await adminApi.gateway(csrfTokenRef.current, data.revision, enabled)
      const control = await adminApi.control(csrfTokenRef.current, gateway.revision, prompt, disabledProviders, disabledModels)
      setData(current => current ? {
        ...current,
        revision: control.revision,
        gateway_api: { ...current.gateway_api, enabled },
        highest_priority_system_prompt: prompt,
        disabled_providers: disabledProviders,
        disabled_models: disabledModels,
      } : current)
    } catch (reason) {
      // 第一个写入可能已经成功。重新读取服务端状态，避免保留过期 revision。
      try { setData(await adminApi.console(csrfTokenRef.current)) } catch { /* 保留原始写入错误 */ }
      throw reason
    }
  }

  const saveControl = async (prompt: string, disabledProviders: string[], disabledModels: string[]) => {
    if (!data) return
    setError('')
    const result = await adminApi.control(csrfTokenRef.current, data.revision, prompt, disabledProviders, disabledModels)
    setData(current => current ? {
      ...current,
      revision: result.revision,
      highest_priority_system_prompt: prompt,
      disabled_providers: disabledProviders,
      disabled_models: disabledModels,
    } : current)
  }

  const saveProvider = async (providerId: string, config: Record<string, unknown>, apiKey?: string) => {
    if (!data) return
    setError('')
    await adminApi.provider(csrfTokenRef.current, providerId, data.revision, config, apiKey)
    setData(await adminApi.console(csrfTokenRef.current))
  }

  const requestRestart = async (reason: string, force = false) => {
    const queued = await adminApi.restart(csrfTokenRef.current, reason, force)
    setRestart(current => current ? {
      ...current,
      restart: { ...current.restart, request_id: queued.request_id, phase: 'queued', message: '重启请求已排队' },
    } : current)
    return queued.request_id
  }

  const session = useMemo<AdminSessionValue>(() => ({ booting, data, error, connect }), [booting, data, error, connect])
  const value = data ? {
    csrfToken,
    data,
    restart,
    isOwner,
    refreshing,
    error,
    clearError: () => setError(''),
    refresh,
    disconnect,
    saveRuntime,
    saveControl,
    saveProvider,
    requestRestart,
  } : null

  return <AdminSessionContext.Provider value={session}><AdminContext.Provider value={value}>{children}</AdminContext.Provider></AdminSessionContext.Provider>
}

export function useAdminSession() {
  const value = useContext(AdminSessionContext)
  if (!value) throw new Error('AdminProvider missing')
  return value
}

export function useAdmin() {
  const value = useContext(AdminContext)
  if (!value) throw new Error('管理端尚未认证')
  return value
}
