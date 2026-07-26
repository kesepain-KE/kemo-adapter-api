# Kemo 网关公开 API

本文只说明网关对外提供给 kemo-agent 的 LLM 与 Asset API，不包含 Web 管理端接口。

协议版本：`1.0`

## 通用请求头

```http
Authorization: Bearer <gateway-key>
X-Kemo-Protocol-Version: 1.0
X-Request-ID: <request_id>
Idempotency-Key: <request_id>
Accept: application/json | text/event-stream
Last-Event-ID: <event_id>   # 仅 SSE 恢复
```

Token 解析得到可信的 tenant、subject 和 scopes；正文中的 `metadata.user` 只用于关联，不能决定
资源授权。

## 运行时控制与重启边界

网关 API 配置/调用密钥、厂商 API 配置/密钥、最高权限系统提示词以及 Provider/模型启停可在
不重启的情况下更新，并从后续新请求开始生效。所有环境变量、源代码、协议、依赖和其他配置
修改都必须重启。运行时控制文件不是对外 HTTP API，不向调用方暴露。

## 模型接口

| 方法与路径 | 用途 | 当前骨架 |
| --- | --- | --- |
| `POST /model/responses` | 创建非流式或 SSE 响应 | 已提供 |
| `GET /model/responses/{response_id}` | 查询运行中或终态响应 | 已提供 |
| `POST /model/responses/{response_id}/cancel` | 幂等取消响应 | 已提供 |
| `GET /model/capabilities?model={model}` | 查询真实模型能力 | 已提供 |

`POST /model/responses` 的正文使用 Kemo Provider Request。`stream=false` 返回完整
`KemoResponse`；`stream=true` 返回 `text/event-stream`，第一个事件为 `response.created`，且每个
响应只能具有一个统一终态。

幂等范围是 `(tenant_id, request_id)`。相同 ID 和相同正文必须复用逻辑响应；相同 ID 和不同
正文返回 `409 IDEMPOTENCY_CONFLICT`。

## Asset 接口

| 方法与路径 | 用途 | 当前骨架 |
| --- | --- | --- |
| `POST /assets` | 流式上传多模态输入 | 待实现 |
| `GET /assets/{asset_id}` | 查询 Asset 状态和元数据 | 待实现 |
| `GET /assets/{asset_id}/content` | 鉴权下载，音视频支持 Range | 待实现 |
| `DELETE /assets/{asset_id}` | 删除当前主体可控的临时 Asset | 待实现 |

网关不得通过公开 API 暴露本地文件路径。生成图片、音频和视频必须返回稳定 `asset_id`、真实
MIME 和 SHA-256，并通过认证下载接口取得内容。

## 统一错误

```json
{
  "protocol_version": "1.0",
  "request_id": "req_1",
  "error": {
    "type": "provider_rate_limit",
    "code": "RATE_LIMITED",
    "message": "Provider returned HTTP 429.",
    "retryable": true,
    "retry_after_ms": 3000,
    "provider_status": 429,
    "details": {}
  }
}
```

错误内容必须脱敏，不能包含密钥、完整上游错误体、内部堆栈、Provider State 明文或长期签名
URL。完整字段、Item、Content Block、Usage 和 SSE 事件合同以协议模型及联调基线为准。
