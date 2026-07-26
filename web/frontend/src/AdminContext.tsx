import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { adminApi, AdminApiError, type AdminConsoleData, type RestartStatus } from './adminApi'

const TOKEN_KEY = 'kemo_admin_token'

interface AdminContextValue {
  token: string
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
  connect: (token: string) => Promise<boolean>
}

const AdminContext = createContext<AdminContextValue | null>(null)
const AdminSessionContext = createContext<AdminSessionValue | null>(null)

export function AdminProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState(() => sessionStorage.getItem(TOKEN_KEY) ?? '')
  const [data, setData] = useState<AdminConsoleData | null>(null)
  const [restart, setRestart] = useState<RestartStatus | null>(null)
  const [isOwner, setIsOwner] = useState(false)
  const [booting, setBooting] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const tokenRef = useRef(token)
  tokenRef.current = token

  const disconnect = useCallback(() => {
    sessionStorage.removeItem(TOKEN_KEY)
    setToken('')
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

  const connect = useCallback(async (candidate: string, allowEmpty = false) => {
    const normalized = candidate.trim()
    if (!normalized && !allowEmpty) {
      setError('请输入具有 admin:web 或 owner 权限的管理密钥。')
      return false
    }
    setBooting(true)
    setError('')
    try {
      const consoleData = await adminApi.console(normalized)
      setToken(normalized)
      tokenRef.current = normalized
      if (normalized) sessionStorage.setItem(TOKEN_KEY, normalized)
      else sessionStorage.removeItem(TOKEN_KEY)
      setData(consoleData)
      setIsOwner(consoleData.permissions.can_restart)
      if (consoleData.permissions.can_restart) {
        try { await loadRestart(normalized) } catch { setRestart(null) }
      } else {
        setRestart(null)
      }
      return true
    } catch (reason) {
      sessionStorage.removeItem(TOKEN_KEY)
      setData(null)
      setError(normalized ? (reason instanceof Error ? reason.message : '无法连接管理 API') : '')
      return false
    } finally {
      setBooting(false)
    }
  }, [loadRestart])

  useEffect(() => {
    void connect(token, true)
    // 仅在首次挂载时恢复会话级凭据。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const refresh = useCallback(async () => {
    const currentToken = tokenRef.current
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
      const gateway = await adminApi.gateway(tokenRef.current, data.revision, enabled)
      const control = await adminApi.control(tokenRef.current, gateway.revision, prompt, disabledProviders, disabledModels)
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
      try { setData(await adminApi.console(tokenRef.current)) } catch { /* 保留原始写入错误 */ }
      throw reason
    }
  }

  const saveControl = async (prompt: string, disabledProviders: string[], disabledModels: string[]) => {
    if (!data) return
    setError('')
    const result = await adminApi.control(tokenRef.current, data.revision, prompt, disabledProviders, disabledModels)
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
    const result = await adminApi.provider(tokenRef.current, providerId, data.revision, config, apiKey)
    setData(current => current ? {
      ...current,
      revision: result.revision,
      provider_configs: { ...current.provider_configs, [providerId]: config },
    } : current)
  }

  const requestRestart = async (reason: string, force = false) => {
    const queued = await adminApi.restart(tokenRef.current, reason, force)
    setRestart(current => current ? {
      ...current,
      restart: { ...current.restart, request_id: queued.request_id, phase: 'queued', message: '重启请求已排队' },
    } : current)
    return queued.request_id
  }

  const session = useMemo<AdminSessionValue>(() => ({ booting, data, error, connect }), [booting, data, error, connect])
  const value = data ? {
    token,
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
