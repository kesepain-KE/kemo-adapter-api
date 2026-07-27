export type PageId = 'dashboard' | 'providers' | 'models' | 'keys' | 'logs' | 'call-logs' | 'settings'

export type ProviderStatus = 'healthy' | 'warning' | 'disabled' | 'unconfigured'

export interface ProviderInfo {
  id: string
  name: string
  monogram: string
  color: string
  status: ProviderStatus
  statusLabel: string
  endpoint: string
  models: number
  calls: string
  latency: string
  capabilities: string[]
}

export interface ModelInfo {
  id: string
  provider: string
  enabled: boolean
  calls: string
  latency: string
  capabilities: string[]
}

export interface ConsoleData {
  version: string
  protocolVersion: string
  revision: string
  gatewayEnabled: boolean
  highestPrioritySystemPrompt: string
  disabledProviders: string[]
  disabledModels: string[]
}
