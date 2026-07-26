export interface AdminProvider {
  provider_id: string
  models: string[]
  [key: string]: unknown
}

export interface AdminConsoleData {
  version: string
  protocol_version: string
  authentication: {
    required: boolean
  }
  permissions: {
    can_restart: boolean
  }
  revision: string
  gateway_api: Record<string, unknown>
  highest_priority_system_prompt: string
  disabled_providers: string[]
  disabled_models: string[]
  providers: AdminProvider[]
  provider_configs: Record<string, Record<string, unknown>>
  live_config_error: string | null
  runtime: {
    instance_id: string
    phase: 'starting' | 'running' | 'draining' | 'stopping'
    active_executions: number
    started_at: string
    drain_reason: string | null
  }
}

interface UpdateResult {
  revision: string
  status: string
}

export interface RestartStatus {
  gateway: {
    instance_id: string
    phase: 'starting' | 'running' | 'draining' | 'stopping'
    active_executions: number
    started_at: string
    drain_reason: string | null
  }
  restart: {
    request_id: string | null
    phase: string
    message: string
    updated_at: string | null
    active_executions?: number | null
    new_instance_id?: string | null
  }
}

export class AdminApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/admin/api${path}`, {
    ...init,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    let message = `管理 API 请求失败 (${response.status})`
    try {
      const body = await response.json() as { detail?: string }
      if (body.detail) message = body.detail
    } catch {
      // 非 JSON 错误页仅使用状态码，避免把响应正文显示到管理页。
    }
    throw new AdminApiError(response.status, message)
  }
  return response.json() as Promise<T>
}

export const adminApi = {
  console: (token: string) => request<AdminConsoleData>('/console', token),
  gateway: (token: string, expectedRevision: string, enabled: boolean) =>
    request<UpdateResult>('/runtime/gateway', token, {
      method: 'PUT',
      body: JSON.stringify({ expected_revision: expectedRevision, enabled }),
    }),
  control: (
    token: string,
    expectedRevision: string,
    highestPrioritySystemPrompt: string,
    disabledProviders: string[],
    disabledModels: string[],
  ) => request<UpdateResult>('/runtime/control', token, {
    method: 'PUT',
    body: JSON.stringify({
      expected_revision: expectedRevision,
      highest_priority_system_prompt: highestPrioritySystemPrompt,
      disabled_providers: disabledProviders,
      disabled_models: disabledModels,
    }),
  }),
  provider: (
    token: string,
    providerId: string,
    expectedRevision: string,
    config: Record<string, unknown>,
    apiKey?: string,
  ) => request<UpdateResult>(`/runtime/providers/${encodeURIComponent(providerId)}`, token, {
    method: 'PUT',
    body: JSON.stringify({
      expected_revision: expectedRevision,
      config,
      ...(apiKey ? { api_key: apiKey } : {}),
    }),
  }),
  restartStatus: (token: string) => request<RestartStatus>('/system/restart', token),
  restart: (token: string, reason: string, force = false) =>
    request<{ request_id: string; status: 'queued'; force: boolean }>('/system/restart', token, {
      method: 'POST',
      body: JSON.stringify({ reason, force }),
    }),
}
