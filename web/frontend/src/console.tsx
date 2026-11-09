import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { t } from './i18n'

export type Entity = Record<string, any>
export interface ConsoleData {
  revision: string; key_revision: string
  version: Entity; runtime: Entity; health: Record<string, number>; startup: Entity
  credential_health: Record<string, Record<string, Entity>>
  upstreams: Entity[]; models: Entity[]; deployments: Entity[]; keys: Entity[]
  adapters: Entity[]; templates: Entity[]; attempts: Entity[]; settings: Entity
  model_vendors: Record<string, string>
  tokenizers: { id: string; label: string }[]
  retired_generations: number
  authentication: { required: boolean }
  permissions: { can_restart: boolean; can_manage_keys: boolean; can_manage_extensions: boolean }
}

let csrfToken = ''
export function setCsrf(value: string) { csrfToken = value }
export class ApiError extends Error {
  constructor(public status: number, message: string, public field = '', public code = '') { super(message) }
}
export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (csrfToken) headers.set('X-CSRF-Token', csrfToken)
  const response = await fetch(`/admin/api${path}`, { ...init, headers, credentials: 'same-origin' })
  const result = await response.json()
  if (!response.ok) {
    const detail = result.detail ?? result.error?.message
    throw new ApiError(response.status,
      typeof detail === 'string' ? detail : detail?.message || t('请求失败') + ` (${response.status})`,
      detail?.field || '', detail?.code || '')
  }
  return result
}

interface ConsoleState {
  data: ConsoleData | null; error: Error | null
  refresh: () => Promise<void>
  mutate: (path: string, method: string, value?: Entity, key?: boolean, expectedRevision?: string) => Promise<any>
}
const Context = createContext<ConsoleState>(null!)
export function ConsoleProvider({ children }: { children: ReactNode }) {
  const [data, setData] = useState<ConsoleData | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const refresh = useCallback(async () => {
    try {
      const next = await api<ConsoleData>('/console')
      if (next.authentication.required && !csrfToken) {
        const session = await api('/auth/session')
        setCsrf(session.csrf_token)
      }
      setData(next); setError(null)
    } catch (cause) {
      setError(cause as Error)
      throw cause
    }
  }, [])
  useEffect(() => {
    void refresh().catch(() => {})
    const timer = window.setInterval(() => {
      if (!document.hidden) void refresh().catch(() => {})
    }, 15000)
    return () => window.clearInterval(timer)
  }, [refresh])
  const mutate = async (path: string, method: string, value: Entity = {}, key = false, expectedRevision?: string) => {
    if (!data) throw new Error(t('正在加载'))
    const result = await api(path, { method, body: JSON.stringify({
      expected_revision: expectedRevision ?? (key ? data.key_revision : data.revision), value,
    }) })
    // A successful write may contain the only copy of a newly created key.
    // refresh already exposes its error through the console state.
    await refresh().catch(() => {})
    return result
  }
  return <Context.Provider value={{ data, error, refresh, mutate }}>{children}</Context.Provider>
}
export const useConsole = () => useContext(Context)

export function useAction() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function run<T,>(action: () => Promise<T>): Promise<{ result: T } | undefined> {
    setBusy(true); setError('')
    try { return { result: await action() } }
    catch (cause) { setError((cause as Error).message); return undefined }
    finally { setBusy(false) }
  }
  return { busy, error, run, clearError: () => setError('') }
}
