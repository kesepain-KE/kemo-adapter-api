# Kemo 网关公开 API

本文只说明网关对外提供给 kemo-agent / kemo-graph 的 LLM、Embedding、Rerank、Asset 与
智能体只读状态 API，
不包含 Web 管理端接口。

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

网页或 `restart.py` 发起的重启会先在独立 Python 进程中执行 `start_web.py --preflight`，验证新环境、
前端构建产物、依赖和后端应用装配；预检失败时旧实例继续运行。预检通过后才进入 Drain，旧 PID 和
旧监听端口完全释放后，替换进程按新 `.env` 的 HOST/PORT 启动，并以新的 `instance_id` 通过
`/healthz` 后报告成功。新实例启动失败时会尽力使用旧进程环境恢复服务，状态会明确标记为失败及恢复的
实例 ID。认证配置未改变时，Web 会话只持久化 Cookie Token 的哈希、CSRF Token 和到期时间，平滑重启
不会让两小时内的浏览器会话立即失效；修改 Web Token 或用户名/密码会使旧会话命名空间失效。

## 智能体全局感知接口

`GET /status` 为 kemo-agent 等外部智能体提供只读网关状态快照。它使用独立的
`STATUS_TOKEN`，不得使用模型调用密钥或 Web 管理凭据：

```http
GET /status?date=2026-07-27&ranking_limit=100&log_limit=50
Authorization: Bearer <STATUS_TOKEN>
```

`STATUS_TOKEN` 为空时接口返回 `503`；与 `WEB_TOKEN`、启动模型密钥或热加载模型密钥重复时
同样拒绝启用。环境变量只在启动时读取，修改后必须重启。

查询参数：

| 参数 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `date` | 统计时区的当天 | ISO `YYYY-MM-DD` | Token、调用次数和排行所属日期 |
| `ranking_limit` | `100` | `1..100` | Provider、模型、网关密钥 ID 排行数量 |
| `log_limit` | `50` | `1..100` | 最近、成功和失败日志各自的最大数量 |

响应 `object` 固定为 `kemo.gateway_status`，包含：

- `runtime`：实例阶段、启动时间和活动执行数；
- `version`：本地/远程版本、协议版本和是否需要更新，远程结果最多缓存 5 分钟；
- `registry.providers`、`registered_provider_ids`、`enabled_models`：已注册厂商和实际启用模型；
- `control.highest_priority_system_prompt`、禁用厂商和禁用模型；
- `statistics.summary`、`token_cache_rate`，以及按 Provider、模型、网关密钥 ID 聚合的调用次数、
  成功/失败次数、Token 用量、覆盖率、缓存命中率和平均延迟；
- `logs.recent`、`successful`、`failed`、`last_invocation`：跨日最近调用的脱敏运行日志。

日志仅包含时间、任务、Provider ID、模型、网关密钥 ID、状态、统一错误代码、延迟和规范化
Token。接口禁止返回网关密钥原文、Provider 密钥、请求头密钥、租户、请求正文、请求 ID、
响应 ID、Provider 原始响应或堆栈。响应固定携带 `Cache-Control: no-store`。该路由只支持 GET，
不存在任何写操作。

### kemo-agent 内置拓展

kemo-agent `v0.6.0` 起可通过内置全局拓展 `kemo_gateway_status` 消费本接口。拓展默认未激活；
用户明确提供网关根地址和独立 `STATUS_TOKEN` 后，主智能体调用 `activate` 完成首次验证。
验证成功后，拓展在 kemo-agent 本地保存凭据，并从响应中白名单提取运行、版本、Provider、模型、
调用与 Token 聚合数据，生成 Markdown 摘要、脱敏 JSON 和 PNG 图表。

客户端不会跟随 HTTP 重定向，反向代理或隧道部署必须直接提供最终可访问的网关根地址。
`deactivate` 只删除 kemo-agent 本地配置和产物，不改变本接口或网关运行状态。

## 公开模型命名

所有 LLM、Embedding 和 Rerank 模型统一使用 `<provider_id>-<provider内部模型名>`：例如
`deepseek-deepseek-v4-flash`。`provider_id` 通常与 `providers/<provider_id>/` 目录名一致。
Provider 内部模型名可以继续包含连字符；网关通过注册表保存模型与 Provider 的精确归属，禁止按
任意连字符拆分模型名。旧式 `provider_id/model` 名称不再接受。

## 模型接口

| 方法与路径 | 用途 | 当前骨架 |
| --- | --- | --- |
| `GET /model/models` | 获取当前密钥真正可调用的 Kemo 模型目录 | 已提供 |
| `GET /model/models/{model}/capabilities` | 获取指定模型的真实能力声明 | 已提供 |
| `GET /v1/models` | 常见智能体框架兼容的模型发现入口 | 已提供 |
| `POST /model/responses` | 创建非流式或 SSE 响应 | 已提供 |
| `GET /model/responses/{response_id}` | 查询运行中或终态响应 | 已提供 |
| `POST /model/responses/{response_id}/cancel` | 幂等取消响应 | 已提供 |
| `GET /model/capabilities?model={model}` | 查询真实模型能力（旧式兼容路径） | 已提供 |
| `POST /model/embeddings` | 为 kemo-graph 批量生成查询或文档向量 | 已提供 |
| `POST /model/rerank` | 为 kemo-graph 对候选文档重排序 | 已提供 |

### 当前密钥的模型发现

智能体接入网关后应先请求 Kemo 原生目录：

```http
GET /model/models
Authorization: Bearer <gateway-key>
```

可使用 `task=llm|embedding|rerank` 过滤任务类型。返回结果是“当前密钥视角”而不是注册表原始
全量数据，会同时应用：

- 当前密钥的 `allowed_models` 白名单；`null` 表示允许全部，空数组表示全部禁止；
- Provider 和模型的全局启停状态；
- `model:invoke`、`embedding:invoke`、`rerank:invoke` 或 `owner` scope；
- Provider 的真实 capabilities 声明。

响应示例：

```json
{
  "protocol_version": "1.0",
  "object": "kemo.model_list",
  "count": 1,
  "data": [
    {
      "id": "deepseek-deepseek-v4-flash",
      "object": "kemo.model",
      "provider_id": "deepseek",
      "provider_model": "deepseek-v4-flash",
      "task": "llm",
      "capabilities_available": true,
      "capabilities_url": "/model/models/deepseek-deepseek-v4-flash/capabilities"
    }
  ]
}
```

如果某个 Provider 的能力声明暂时异常，持有 `model:invoke`/`owner` 的通用调用方仍可在未指定
`task` 时看到该模型，但其 `task` 为 `unknown` 且 `capabilities_available=false`；任务专用密钥不会
看到任务未知的模型。单个 Provider 异常不会使整个目录请求失败。目录与能力响应均携带
`Cache-Control: no-store` 和 `Vary: Authorization`，不同密钥的结果禁止共用缓存。

只支持常见模型列表协议的客户端可请求：

```http
GET /v1/models
Authorization: Bearer <gateway-key>
```

它返回标准的 `object=list` 与 `data[].id/object/created/owned_by` 形状，并应用与 Kemo 原生目录
完全相同的密钥白名单、全局启停和 scope 过滤。网关不提供语义不明确的 `/api/models`、
`/models` 等额外别名。

### 模型能力声明

推荐使用路径式接口：

```http
GET /model/models/deepseek-deepseek-v4-flash/capabilities
Authorization: Bearer <gateway-key>
```

旧客户端可以继续使用 `GET /model/capabilities?model=...`，两者响应完全一致。能力查询同样要求
模型位于当前密钥白名单内，并检查声明任务对应的 scope。响应包含 `task`、输入/输出模态、
流式、推理档位、工具调用、结构化输出，以及 Embedding/Rerank 的任务专属约束；不会返回
Provider 密钥、网关密钥、请求头或其他私有配置。

`POST /model/responses` 的正文使用 Kemo Provider Request。`stream=false` 返回完整
`KemoResponse`；`stream=true` 返回 `text/event-stream`，第一个事件为 `response.created`，且每个
响应只能具有一个统一终态。

### 完整多模态内容与操作

Kemo 1.0 使用同一个 `POST /model/responses` 承载文本对话、视觉、ASR、TTS、语音转换、图片
生成/编辑、视频理解和视频生成。目录中的此类模型仍声明 `task=llm`；具体操作由
`metadata.capability` 指定，并且必须同时满足 `input_modalities`、`output_modalities` 和
`extensions.operations.<操作>.supported=true`。网关核心不会把专用操作统一塞进厂商
`/chat/completions`，具体端点和转换由 Provider 自己选择。

| `metadata.capability` | 必需输入 | 必需输出 |
| --- | --- | --- |
| `conversation` | 可含 text/image/audio/video/file | text 或 tool_call |
| `vision` | text + image | text |
| `image_generation` | text | image |
| `image_edit` | text + image | image |
| `audio_transcription` | audio | text |
| `speech_generation` | text | audio |
| `speech_to_speech` | audio，可附 text | audio |
| `video_understanding` | video，可附 text | text |
| `video_generation` | text，可附参考媒体 | video |

普通文件既可作为 `conversation` 输入，也可作为伴随产物输出；请求文件产物时在
`output.modalities` 中加入 `file`，并提供可选的 `output.file.filename/mime_type`。当前没有
独立的 `file_generation` 操作，Provider 不得自行发明公开操作名。

实时双向音频会话不是上述请求/响应合同；未增加独立实时会话协议前不得伪装支持。

内联图片示例：

```json
{
  "type": "message",
  "role": "user",
  "content": [
    {"type": "text", "text": "描述这张图片"},
    {
      "type": "image",
      "mime_type": "image/png",
      "detail": "auto",
      "source": {
        "kind": "inline_base64",
        "data": "<base64>"
      }
    }
  ]
}
```

远程媒体使用 `source: {"kind": "url", "uri": "https://..."}`；完整 Data URL 使用
`source: {"kind": "data_url", "uri": "data:image/...;base64,..."}`。音频、视频和普通文件分别
使用 `type=audio|video|file`。内联媒体最多 1 MiB，并校验 Base64、Data URL、MIME 与文件头；
大型媒体必须先上传 `/assets`，再在内容块中使用 `asset_id`。外部 URL 只允许受控 HTTPS，并在
进入 Provider 前执行 URL、DNS 和 IP SSRF 检查。客户端不得提交本地文件路径。

Provider 生成图片、音频、视频或文件后，必须通过 `RequestContext.assets.store_output()` 登记，
然后在 assistant MessageItem 中返回 `asset_id`、真实 `mime_type` 和 `checksum_sha256`。流式生成
先发送一次 `output_media.completed`，其中的完整 Item 必须与随后统一终态中的 Item 完全一致。
单个 SSE `data` 上限为 1 MiB；媒体正文不得通过自定义大分片绕过 Asset。请求 JSON 本身默认
上限为 2 MiB，不包含独立上传的 Asset 字节。

幂等范围是 `(tenant_id, request_id)`。相同 ID 和相同正文必须复用逻辑响应；相同 ID 和不同
正文返回 `409 IDEMPOTENCY_CONFLICT`。

### SSE 心跳、断线续传与持久化边界

`stream=true` 时，网关会在发出 `200 text/event-stream` 响应头之前完成请求、幂等键和
`Last-Event-ID` 校验。续传游标不存在、不属于当前响应或已经超过保留期时，接口直接返回 JSON
格式的 `409 STREAM_RESUME_CONFLICT`，不会先返回成功的 SSE 响应再中途断开。

每个 Kemo SSE 协议事件都有稳定的 `event_id` 和递增 `sequence`。客户端应保存最后一个已完整处理
事件的 `event_id`；断线后使用完全相同的 Authorization、请求正文、`request_id`、
`Idempotency-Key`，并通过 `Last-Event-ID` 回传该值。网关从它的下一个事件开始重放，避免重复消费。

流空闲时默认每 15 秒发送一次 SSE 注释心跳：

```text
: kemo-heartbeat

```

心跳不是 Kemo 协议事件，没有 `event_id`，不推进 `sequence`，客户端只需忽略。SSE 响应同时携带
`Cache-Control: no-cache, no-transform`、`X-Accel-Buffering: no` 和
`X-Kemo-Heartbeat-Seconds`；反向代理仍应关闭流式响应缓冲，并把空闲超时设置为大于心跳间隔。

幂等记录、统一终态和已发出的 SSE 事件持久化在网关本地 SQLite WAL 数据库
`storage/executions/executions.sqlite3`，默认保留 24 小时。客户端断开不会取消同一网关进程中的
Provider 执行，相同请求可以在保留期内重连或查询终态。网关进程重启时，上次尚未结束的执行会被
确定性终结为 `status=incomplete`、`incomplete_details.reason=gateway_restarted`，并追加
`response.incomplete` 终态事件。

持久化只保证已经提交到本地数据库的事件和响应能够重放，不代表上游推理可以跨网关进程继续。
进程退出时尚未落盘的上游输出无法恢复，也不会在重启后偷偷重新请求 Provider。

### 超时、容量与安全重试

- LLM、Embedding 和 Rerank 都受核心执行时限保护，默认 900 秒；超时错误码为
  `GATEWAY_TIMEOUT` 且 `retryable=true`。LLM 在统一响应或 SSE 终态中返回失败，Embedding/Rerank
  使用 HTTP 504。这里的 `retryable` 是错误分类提示，不代表已形成的 LLM 失败终态会在同一 ID 下重新执行。
- 单进程默认最多同时执行 64 个模型请求。容量已满或网关处于 Drain 时返回 HTTP 503、
  `GATEWAY_OVERLOADED` 或 `GATEWAY_DRAINING`，并携带 `Retry-After: 5`。
- 单个流式响应默认最多持久化 200000 个事件，防止异常 Provider 无限占用内存和磁盘。
- 建连、读取或 HTTP 级瞬时失败可以有限重试，并优先遵守 `retry_after_ms` 或 HTTP
  `Retry-After`。不确定请求是否已经到达网关时，必须复用原来的 `request_id`、幂等键和完全相同的
  正文，不能生成新 ID，否则无法利用幂等边界避免重复调用上游。Embedding/Rerank 的 HTTP 504
  也按这一幂等规则处理。
- 已经形成并持久化的 LLM `response.failed` 终态不是传输失败：相同 `request_id` 只会重放原失败，
  不会再次调用 Provider。若业务层决定重新执行，必须创建新的逻辑请求和新 ID；这可能产生新计费，
  不应由底层传输代码自动决定。
- HTTP 错误正文中的显式 `retryable=true/false` 优先于状态码默认分类；显式为 `false` 时不得重试。

这些默认值分别由 `SSE_HEARTBEAT_SECONDS`、`EXECUTION_RETENTION_HOURS`、
`MODEL_EXECUTION_TIMEOUT_SECONDS`、`MAX_CONCURRENT_EXECUTIONS` 和
`MAX_SSE_EVENTS_PER_RESPONSE` 配置；它们属于启动环境变量，修改后必须重启网关。

## kemo-graph 检索模型接口

Embedding 与 Rerank 是独立同步任务，不使用 LLM `input/output` Item，也不通过
`/model/responses` 执行。两者均要求：

- `X-Kemo-Protocol-Version: 1.0`；
- `Idempotency-Key` 与正文 `request_id` 完全相同；
- `model:invoke` 或对应的 `embedding:invoke` / `rerank:invoke` scope；
- Provider/模型启停、Drain、热配置和统一错误边界与 LLM 模型一致。

### `POST /model/embeddings`

请求：

```json
{
  "protocol_version": "1.0",
  "request_id": "embed_req_01",
  "model": "vendor-embedding-model",
  "input_type": "document",
  "inputs": [
    {"id": "node-1", "text": "知识图谱节点文本", "metadata": {}},
    {"id": "node-2", "text": "另一段文本", "metadata": {}}
  ],
  "dimensions": 3,
  "normalize": true,
  "provider_options": {},
  "metadata": {},
  "extensions": {}
}
```

`input_type` 只能为 `query` 或 `document`。同一请求的 `inputs[].id` 必须唯一；顺序和 ID 会在
响应中原样稳定映射。`dimensions`、`normalize`、批量大小和单项 Token 限制必须先通过模型
capabilities 校验，不能静默降维、截断或改变归一化策略。

成功响应：

```json
{
  "protocol_version": "1.0",
  "object": "kemo.embedding_list",
  "request_id": "embed_req_01",
  "model": "vendor-embedding-model",
  "model_version": "2026-07",
  "vector_space_id": "vendor-embedding-model@2026-07:3:normalized",
  "dimensions": 3,
  "data": [
    {"id": "node-1", "index": 0, "vector": [0.01, -0.02, 0.03]},
    {"id": "node-2", "index": 1, "vector": [0.03, 0.04, -0.01]}
  ],
  "usage": {
    "input_tokens": 18,
    "total_tokens": 18,
    "measurement": {
      "mode": "provider",
      "exact": true,
      "exact_fields": ["input_tokens", "total_tokens"],
      "estimated_fields": []
    },
    "media": {},
    "stages": [],
    "provider_raw": {}
  },
  "provider_response_id": "upstream-id",
  "metadata": {},
  "extensions": {}
}
```

真实 `vector` 长度必须等于 `dimensions`，并且所有值必须为有限数。kemo-graph 必须把
`vector_space_id` 与向量共同保存，禁止混合不同模型版本、维度或归一化策略的向量。

### `POST /model/rerank`

请求：

```json
{
  "protocol_version": "1.0",
  "request_id": "rerank_req_01",
  "model": "vendor-rerank-model",
  "query": "用户正在查询什么？",
  "documents": [
    {"id": "doc-1", "text": "候选文档一", "metadata": {"node_id": "n1"}},
    {"id": "doc-2", "text": "候选文档二", "metadata": {"node_id": "n2"}}
  ],
  "top_n": 2,
  "return_documents": false,
  "provider_options": {},
  "metadata": {},
  "extensions": {}
}
```

成功响应：

```json
{
  "protocol_version": "1.0",
  "object": "kemo.rerank",
  "request_id": "rerank_req_01",
  "model": "vendor-rerank-model",
  "model_version": "2026-07",
  "score_semantics": "higher_is_more_relevant",
  "results": [
    {
      "rank": 1,
      "document_id": "doc-2",
      "index": 1,
      "relevance_score": 0.91,
      "document": null
    }
  ],
  "usage": {
    "input_tokens": 31,
    "total_tokens": 31,
    "measurement": {
      "mode": "provider",
      "exact": true,
      "exact_fields": ["input_tokens", "total_tokens"],
      "estimated_fields": []
    },
    "media": {},
    "stages": [],
    "provider_raw": {}
  },
  "provider_response_id": "upstream-id",
  "metadata": {},
  "extensions": {}
}
```

网关统一按 `higher_is_more_relevant` 降序返回并生成从 1 开始的 `rank`。分数只允许在同一模型
及版本的一次排序语境内比较，不得假设不同厂商或不同模型的分数经过统一校准。

### 检索模型 capabilities

`GET /model/capabilities` 增加 `task: embedding|rerank`。Embedding 模型必须声明输入类型、默认/
支持维度、批量上限、单项 Token 上限和归一化语义；Rerank 模型必须声明候选文档上限、查询/
文档 Token 上限、是否支持返回原文以及分数语义。`task=llm` 的现有响应保持兼容。

## Asset 接口

| 方法与路径 | 用途 | 状态 |
| --- | --- | --- |
| `POST /assets` | 流式上传多模态输入 | 已提供 |
| `GET /assets/{asset_id}` | 查询 Asset 状态和元数据 | 已提供 |
| `GET /assets/{asset_id}/content` | 鉴权下载，支持单段 bytes Range | 已提供 |
| `DELETE /assets/{asset_id}` | 删除当前主体可控的临时 Asset | 已提供 |

上传使用 `multipart/form-data`，字段 `file` 为媒体字节，`metadata` 为 JSON 字符串；请求必须
携带 `Authorization`、`X-Kemo-Protocol-Version: 1.0` 和稳定 `Idempotency-Key`，可携带
`X-Content-SHA256`。相同主体、相同幂等键和相同内容复用同一 Asset；同键不同内容返回
`409 IDEMPOTENCY_CONFLICT`。

Asset 按 tenant + subject 隔离，上传/删除要求 `asset:write`，查询/下载要求 `asset:read`，owner
可执行两者。跨主体统一按不可见处理，不泄露资源是否存在。内容写入时执行大小、SHA-256、MIME
和基础魔数校验；默认保留 24 小时，过期或删除后返回 410，后台会回收内容字节。公开响应永远
不返回网关本地路径。

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

常见的网关级错误：

| 错误码 | 出现位置 | 是否可自动重试 | 说明 |
| --- | --- | --- | --- |
| `GATEWAY_TIMEOUT` | LLM 失败终态；Embedding/Rerank HTTP 504 | LLM 同 ID 否；Embedding/Rerank 可按幂等重试 | 达到核心执行时限 |
| `GATEWAY_OVERLOADED` | HTTP 503 | 是 | 已达到单进程并发上限，遵守 `Retry-After` |
| `GATEWAY_DRAINING` | HTTP 503 | 是 | 网关正在排空并准备重启，遵守 `Retry-After` |
| `STREAM_RESUME_CONFLICT` | HTTP 409 JSON | 否 | SSE 游标不存在、过期或不属于当前响应 |
| `IDEMPOTENCY_CONFLICT` | HTTP 409 JSON | 否 | 相同 request ID 对应了不同请求正文 |

HTTP 408、425、429、500、502、503、504 在没有更具体声明时默认标记为可重试；任何错误中的显式
`retryable` 值优先。认证、授权、协议校验和正文校验错误不得盲目重试。
