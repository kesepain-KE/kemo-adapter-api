import type { ConsoleData, ModelInfo, ProviderInfo } from './types'

export const consoleData: ConsoleData = {
  version: '0.3.0',
  protocolVersion: '1.0',
  revision: 'live_demo_7f21',
  gatewayEnabled: true,
  highestPrioritySystemPrompt:
    '你是 Kemo Gateway 的最高权限运行时控制层。保持协议严谨、密钥安全、Usage 准确，禁止静默改变请求语义。',
  disabledProviders: ['custom_lab'],
  disabledModels: ['openai/tts-voice-v1', 'custom_lab/speech-bridge-v2'],
}

export const providers: ProviderInfo[] = [
  { id: 'openai', name: 'OpenAI', monogram: 'OA', color: '#17213c', status: 'healthy', statusLabel: '运行正常', endpoint: 'api.openai.com', models: 6, calls: '7,220', latency: '1.42s', capabilities: ['conversation', 'reasoning', 'vision', 'tools'] },
  { id: 'anthropic', name: 'Anthropic', monogram: 'AN', color: '#d97706', status: 'healthy', statusLabel: '运行正常', endpoint: 'api.anthropic.com', models: 4, calls: '5,108', latency: '2.15s', capabilities: ['conversation', 'reasoning', 'tools'] },
  { id: 'gemini', name: 'Google Gemini', monogram: 'GM', color: '#4f67f6', status: 'healthy', statusLabel: '运行正常', endpoint: 'generativelanguage.googleapis.com', models: 5, calls: '1,994', latency: '1.78s', capabilities: ['vision', 'video', 'conversation'] },
  { id: 'qwen', name: 'Alibaba Qwen', monogram: 'QW', color: '#705bff', status: 'warning', statusLabel: '轻微限流', endpoint: 'dashscope.aliyuncs.com', models: 5, calls: '3,801', latency: '2.83s', capabilities: ['conversation', 'reasoning', 'image'] },
  { id: 'custom', name: 'Custom Adapter', monogram: 'CA', color: '#31b6d7', status: 'unconfigured', statusLabel: '待配置', endpoint: 'custom.internal', models: 2, calls: '392', latency: '3.18s', capabilities: ['conversation', 'structured_output'] },
  { id: 'custom_lab', name: 'Kemo Lab', monogram: 'KL', color: '#94a3b8', status: 'disabled', statusLabel: '已禁用', endpoint: 'lab.internal', models: 2, calls: '0', latency: '—', capabilities: ['speech', 'experimental'] },
]

export const models: ModelInfo[] = [
  { id: 'openai/gpt-5.5', provider: 'OpenAI', enabled: true, calls: '6,240', latency: '1.31s', capabilities: ['conversation', 'reasoning', 'tool_call'] },
  { id: 'anthropic/claude-opus', provider: 'Anthropic', enabled: true, calls: '4,892', latency: '2.15s', capabilities: ['conversation', 'reasoning', 'tool_call'] },
  { id: 'qwen/qwen-max', provider: 'Qwen', enabled: true, calls: '3,716', latency: '2.83s', capabilities: ['conversation', 'reasoning'] },
  { id: 'gemini/gemini-pro-vision', provider: 'Gemini', enabled: true, calls: '1,908', latency: '1.76s', capabilities: ['conversation', 'vision', 'video'] },
  { id: 'openai/tts-voice-v1', provider: 'OpenAI', enabled: false, calls: '0', latency: '—', capabilities: ['speech_generation'] },
  { id: 'custom_lab/speech-bridge-v2', provider: 'Kemo Lab', enabled: false, calls: '0', latency: '—', capabilities: ['speech_to_speech'] },
]
