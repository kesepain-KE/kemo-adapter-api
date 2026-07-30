export interface AdminProvider {
  provider_id: string
  models: string[]
  [key: string]: unknown
}

export interface AdminConsoleData {
  version: string
  protocol_version: string
  base_url: string
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

export interface RestartRequiredStatus {
  required: boolean
  message: string
  changed_groups: Array<'environment' | 'startup' | 'backend' | 'frontend' | 'providers' | 'dependencies' | 'version'>
  changed_files: string[]
  checked_at: string
}

export interface VersionInfo {
  version: string
  protocol_version: string | null
  build: string | null
  notes: string | null
}

export interface VersionCheck {
  status: 'update_available' | 'up_to_date' | 'local_newer' | 'unavailable'
  update_available: boolean | null
  local: VersionInfo | null
  remote: VersionInfo | null
  source: string
  checked_at: string
  message: string
}

export interface StatisticsMetrics {
  calls: number
  successes: number
  failures: number
  cancellations: number
  incompletes: number
  running: number
  replay_count: number
  success_rate: number | null
  average_latency_ms: number | null
  tokens: Record<'input_tokens' | 'cached_input_tokens' | 'output_tokens' | 'reasoning_tokens' | 'visible_output_tokens' | 'total_tokens', number | null>
  token_coverage: Record<string, number>
  cache_hit_rate: number | null
  cache_eligible_samples: number
}

export interface DailyStatistics extends StatisticsMetrics {
  date: string
  timezone: string
  storage: {
    healthy: boolean
    dropped_events: number
    last_error: string | null
    timezone: string
  }
}

export interface HourlyStatistics {
  date: string
  timezone: string
  items: Array<{
    hour: number
    label: string
    calls: number
    successes: number
    failures: number
    success_rate: number | null
    input_tokens: number | null
    input_token_samples: number
    cached_input_tokens: number | null
    cached_input_token_samples: number
    output_tokens: number | null
    output_token_samples: number
    total_tokens: number | null
    token_samples: number
    cache_hit_rate: number | null
    cache_eligible_samples: number
  }>
}

export interface StatisticsSeries {
  from: string
  to: string
  timezone: string
  summary: StatisticsMetrics
  items: DailyStatistics[]
}

export interface StatisticsRanking {
  date: string
  dimension: 'provider' | 'model' | 'gateway_key'
  items: Array<StatisticsMetrics & { id: string }>
}

export type InvocationOutcome = 'all' | 'success' | 'failure'

export interface InvocationLogItem {
  started_at: string
  finished_at: string | null
  task: string
  provider_id: string
  model: string
  provider_model: string
  gateway_key_id: string | null
  status: 'running' | 'completed' | 'requires_action' | 'failed' | 'incomplete' | 'cancelled'
  error_code: string | null
  error_type: string | null
  error_message: string | null
  latency_ms: number | null
  duration_ms?: number | null
  tokens: Record<'input_tokens' | 'cached_input_tokens' | 'output_tokens' | 'reasoning_tokens' | 'visible_output_tokens' | 'total_tokens', number | null>
  usage: {
    mode: string | null
    exact: boolean | null
  }
}

export interface InvocationLogs {
  outcome: InvocationOutcome
  date: string | null
  hour?: number | null
  page?: number
  page_size?: number
  pages?: number
  total?: number
  items: InvocationLogItem[]
}

export interface ModelCapabilityDeclaration {
  model: string
  task: 'llm' | 'embedding' | 'rerank'
  input_modalities: string[]
  output_modalities: string[]
  streaming: boolean
  reasoning: {
    supported: boolean
    efforts: string[]
    summary: boolean
    persisted_state: boolean
  }
  tools: {
    function_calling: boolean
    parallel_calls: boolean
    multimodal_results: boolean
  }
  structured_output: boolean
  embedding: Record<string, unknown> | null
  rerank: Record<string, unknown> | null
  metadata: Record<string, unknown>
  extensions: Record<string, unknown>
}

export interface ProviderCapabilities {
  provider_id: string
  models: ModelCapabilityDeclaration[]
  errors: Array<{ model: string; error: string }>
}

export interface ModelProbeResult {
  model: string
  task: 'llm' | 'embedding' | 'rerank'
  reachable: boolean
  status: string
  latency_ms: number
  error_code: string | null
  tested_at: string
}

export interface GatewayApiKey {
  id?: string
  name: string
  masked_token: string
  source: 'runtime' | 'environment'
  created_at: string | null
  last_used_at: string | null
  allowed_models: string[] | null
  model_policy: 'allow_all' | 'allow_list' | 'deny_all'
  writable: boolean
  usage: {
    calls: number
    successes: number
    total_tokens: number | null
  }
}

export interface GatewayKeyModel {
  id: string
  provider_id: string
  provider_model: string
  enabled: boolean
}

export interface GatewayApiKeys {
  revision: string
  items: GatewayApiKey[]
  models: GatewayKeyModel[]
}

export interface KeyModelPolicyResult extends UpdateResult {
  allowed_models: string[] | null
  model_policy: GatewayApiKey['model_policy']
}

export interface WebAuthMethods {
  token_required: boolean
  password_required: boolean
  configuration_valid: boolean
  session_ttl_seconds: number
}

export interface WebAuthSession {
  expires_at: string
  expires_in: number
  next_step: 'password' | 'complete'
  csrf_token: string | null
}

export interface WebSessionStatus {
  authenticated: true
  expires_at: string
  csrf_token: string
}

export class AdminApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase()
  const response = await fetch(`/admin/api${path}`, {
    ...init,
    credentials: 'same-origin',
    headers: {
      ...(token && !['GET', 'HEAD', 'OPTIONS'].includes(method) ? { 'X-CSRF-Token': token } : {}),
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    let message = `管理 API 请求失败 (${response.status})`
    try {
      const body = await response.json() as { detail?: string; error?: { message?: string } }
      if (body.detail) message = body.detail
      else if (body.error?.message) message = body.error.message
    } catch {
      // 非 JSON 错误页仅使用状态码，避免把响应正文显示到管理页。
    }
    throw new AdminApiError(response.status, message)
  }
  return response.json() as Promise<T>
}

export const adminApi = {
  webAuthMethods: () => request<WebAuthMethods>('/auth/methods', ''),
  webAuthSession: () => request<WebSessionStatus>('/auth/session', ''),
  authenticateWebToken: (token: string) => request<WebAuthSession>('/auth/token', '', {
    method: 'POST',
    body: JSON.stringify({ token }),
  }),
  authenticateWebPassword: (username: string, password: string) =>
    request<WebAuthSession>('/auth/password', '', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ status: 'logged_out' }>('/auth/logout', '', { method: 'POST' }),
  console: (token: string) => request<AdminConsoleData>('/console', token),
  gatewayApiKeys: (token: string) => request<GatewayApiKeys>('/keys', token),
  revealGatewayApiKey: (token: string, keyId: string) =>
    request<{ token: string }>(`/keys/${encodeURIComponent(keyId)}/reveal`, token, {
      method: 'POST',
    }),
  updateKeyModelPolicy: (
    token: string,
    keyId: string,
    expectedRevision: string,
    allowedModels: string[] | null,
  ) => request<KeyModelPolicyResult>(`/keys/${encodeURIComponent(keyId)}/model-policy`, token, {
    method: 'PUT',
    body: JSON.stringify({
      expected_revision: expectedRevision,
      allowed_models: allowedModels,
    }),
  }),
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
  restartRequired: (token: string) => request<RestartRequiredStatus>('/system/restart-required', token),
  versionCheck: (token: string) => request<VersionCheck>('/system/version-check', token),
  restart: (token: string, reason: string, force = false) =>
    request<{ request_id: string; status: 'queued'; force: boolean }>('/system/restart', token, {
      method: 'POST',
      body: JSON.stringify({ reason, force }),
    }),
  dailyStatistics: (token: string, date?: string) =>
    request<DailyStatistics>(`/statistics/daily${date ? `?date=${encodeURIComponent(date)}` : ''}`, token),
  hourlyStatistics: (token: string, date?: string) =>
    request<HourlyStatistics>(`/statistics/hourly${date ? `?date=${encodeURIComponent(date)}` : ''}`, token),
  statisticsSeries: (token: string, from: string, to: string) =>
    request<StatisticsSeries>(`/statistics/series?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`, token),
  statisticsRanking: (
    token: string,
    date: string | undefined,
    dimension: 'provider' | 'model' | 'gateway_key',
  ) => request<StatisticsRanking>(
    `/statistics/rankings?${date ? `date=${encodeURIComponent(date)}&` : ''}dimension=${dimension}`,
    token,
  ),
  invocationLogs: (
    token: string,
    outcome: InvocationOutcome = 'all',
    page = 1,
    pageSize = 20,
    date?: string,
    hour?: number | null,
  ) => {
    const params = new URLSearchParams({
      outcome,
      page: String(page),
      page_size: String(pageSize),
    })
    if (date) params.set('date', date)
    if (hour !== undefined && hour !== null) params.set('hour', String(hour))
    return request<InvocationLogs>(`/statistics/invocations?${params.toString()}`, token)
  },
  providerCapabilities: (token: string, providerId: string) =>
    request<ProviderCapabilities>(`/providers/${encodeURIComponent(providerId)}/capabilities`, token),
  probeModel: (token: string, model: string) =>
    request<ModelProbeResult>(`/models/${encodeURIComponent(model)}/probe`, token, { method: 'POST' }),
}
