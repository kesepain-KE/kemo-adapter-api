import type { GatewayConfigView } from '../adminApi'
import { secretPreview } from './secretPreview'

type ConfigItem = GatewayConfigView['groups'][number]['items'][number]

const labels: Record<string, string> = {
  GATEWAY_BASE_URL: '对外地址', API_DOCS_ENABLED: 'API 文档',
  WEB_COOKIE_SECURE: '安全 Cookie', WEB_ALLOWED_HOSTS: '访问白名单',
  MAX_CONCURRENT_EXECUTIONS: '并发上限', MODEL_EXECUTION_TIMEOUT_SECONDS: '请求超时',
  SSE_HEARTBEAT_SECONDS: '流式心跳', EXECUTION_RETENTION_HOURS: '记录保留',
  MAX_SSE_EVENTS_PER_RESPONSE: '单次事件上限', REQUEST_JSON_MAX_BYTES: '请求正文',
  DEFAULT_ASSET_TTL_HOURS: '资源保留', ASSET_IMAGE_MAX_BYTES: '图片上限',
  ASSET_AUDIO_MAX_BYTES: '音频上限', ASSET_VIDEO_MAX_BYTES: '视频上限',
  ASSET_FILE_MAX_BYTES: '文件上限', WEB_TOKEN: '登录令牌', STATUS_TOKEN: '状态接口令牌',
  GATEWAY_API_KEY: '网关密钥', PROVIDER_SETTINGS: '厂商私有配置',
}

export function configLabel(item: ConfigItem): string {
  return labels[item.name] ?? item.label
}

export function configDisplay(item: ConfigItem): { value: string; unit?: string; tone?: string } {
  if (item.sensitive || item.value === '***') return { value: secretPreview(item.value), tone: 'masked' }
  if (item.name === 'API_DOCS_ENABLED') return { value: item.value === 'true' ? '已开启' : '已关闭', tone: item.value === 'true' ? 'enabled' : 'muted' }
  if (item.name === 'WEB_COOKIE_SECURE') return { value: ({ auto: '自动', true: '仅 HTTPS', false: '不强制 HTTPS' })[item.value] ?? item.value }
  if (!item.value) return { value: item.name === 'WEB_ALLOWED_HOSTS' ? '不限制主机' : item.name === 'GATEWAY_BASE_URL' ? '跟随访问地址' : '未设置', tone: 'muted' }
  const number = Number(item.value)
  if (Number.isFinite(number) && number > 0) {
    if (item.name.endsWith('_BYTES')) {
      const scale = number >= 1024 ** 3 ? 3 : number >= 1024 ** 2 ? 2 : number >= 1024 ? 1 : 0
      return { value: (number / 1024 ** scale).toLocaleString('zh-CN', { maximumFractionDigits: 2 }), unit: ['B', 'KiB', 'MiB', 'GiB'][scale] }
    }
    if (item.name.endsWith('_SECONDS')) return { value: number.toLocaleString('zh-CN', { maximumFractionDigits: 3 }), unit: '秒' }
    if (item.name.endsWith('_HOURS')) return { value: number.toLocaleString('zh-CN'), unit: '小时' }
    if (item.name === 'MAX_CONCURRENT_EXECUTIONS') return { value: number.toLocaleString('zh-CN'), unit: '个' }
    if (item.name === 'MAX_SSE_EVENTS_PER_RESPONSE') return { value: number.toLocaleString('zh-CN'), unit: '条' }
  }
  return { value: item.value }
}
