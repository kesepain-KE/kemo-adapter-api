# Kemo 网关公开 API

本文只说明网关对外提供给 kemo-agent / kemo-graph 的 LLM、Embedding、Rerank 与 Asset API，
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

## 公开模型命名

所有 LLM、Embedding 和 Rerank 模型统一使用 `<provider_id>-<provider内部模型名>`：例如
`deepseek-deepseek-v4-flash`。`provider_id` 通常与 `providers/<provider_id>/` 目录名一致。
Provider 内部模型名可以继续包含连字符；网关通过注册表保存模型与 Provider 的精确归属，禁止按
任意连字符拆分模型名。旧式 `provider_id/model` 名称不再接受。

## 模型接口

| 方法与路径 | 用途 | 当前骨架 |
| --- | --- | --- |
| `POST /model/responses` | 创建非流式或 SSE 响应 | 已提供 |
| `GET /model/responses/{response_id}` | 查询运行中或终态响应 | 已提供 |
| `POST /model/responses/{response_id}/cancel` | 幂等取消响应 | 已提供 |
| `GET /model/capabilities?model={model}` | 查询真实模型能力 | 已提供 |
| `POST /model/embeddings` | 为 kemo-graph 批量生成查询或文档向量 | 已提供 |
| `POST /model/rerank` | 为 kemo-graph 对候选文档重排序 | 已提供 |

`POST /model/responses` 的正文使用 Kemo Provider Request。`stream=false` 返回完整
`KemoResponse`；`stream=true` 返回 `text/event-stream`，第一个事件为 `response.created`，且每个
响应只能具有一个统一终态。

幂等范围是 `(tenant_id, request_id)`。相同 ID 和相同正文必须复用逻辑响应；相同 ID 和不同
正文返回 `409 IDEMPOTENCY_CONFLICT`。

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
