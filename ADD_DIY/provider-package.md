# 创建或修改 Provider Package

Provider 是厂商差异的唯一归属。网关核心只认识 `core.models` 和
`core.provider_contract.ProviderPackage`，不得知道具体厂商的请求字段、流事件、Token 规则或
错误正文。

## 1. 写代码前的判定

先确认任务属于以下哪一种：

1. 修改现有厂商：读取目标目录全部源码和测试，只做增量修改，禁止重新复制模板覆盖；
2. 给现有厂商增加模型：确认上游模型名、任务和能力，再同步模型集合、能力与 manifest；
3. 创建新厂商：复制 `template/provider/` 后完整替换占位符；
4. 增加现有九种多模态操作之外的新任务或实时会话：停止创建 Provider，先向用户说明需要扩展
   公开协议。

当前核心正式支持 `llm`、`embedding`、`rerank`。Kemo `llm` 合同通过
`metadata.capability` 区分 conversation、vision、image_generation、image_edit、
audio_transcription、speech_generation、speech_to_speech、video_understanding 和
video_generation；这些操作共享 `/model/responses`，但 Provider 必须按厂商真实端点分别转换，
不能全部硬塞到 `/chat/completions`，也不能在网关核心写厂商专用旁路。

任何真实厂商调用都可能产生费用。除非用户已明确授权目标环境和测试费用，否则只允许读取
用户提供的公开文档、本地代码和脱敏 Fixture；不得自行尝试密钥、枚举模型或创建云端资源。

## 2. 新厂商创建流程

1. 确认稳定 `provider_id`。文件夹名、`provider_id` 和 manifest 必须完全一致；
2. 复制 `template/provider/` 到 `providers/<provider_id>/`，不要使用已删除的
   `providers/_template`；
3. 删除复制出的 `__pycache__`、`.pyc`，将必要 `.example` 文件改为真实文件名；
4. 替换所有 `Example`、`example`、`.invalid`、`vendor_` 和运行路径中的 TODO；
5. 依据厂商官方协议实现传输、协议、流、Usage、错误、能力和探测；
6. 使用脱敏真实响应制作 Golden Fixture，启用能力前先通过对应测试；
7. 按 `verification.md` 完整验证并重启网关；新增目录不会被运行中进程自动发现。

不要默认请求 `GET /v1/models`。只有厂商明确提供且用户授权时才能调用模型列表接口；否则使用
用户指定模型和权威文档。模型价格无法从权威来源确认时标记“未知”，不得猜测。

## 3. 命名与模型映射

- `provider_id` 使用小写字母、数字、下划线，不能以下划线开头，建议以小写字母开头；
- 公开模型名固定为 `<provider_id>-<厂商原始模型名>`；
- 示例：厂商 `deepseek` 的上游 `deepseek-v4-flash` 暴露为
  `deepseek-deepseek-v4-flash`；
- 厂商原始模型名允许包含多个连字符。只能移除自身完整的 `<provider_id>-` 前缀一次，不能
  按任意连字符拆分；
- `provider.models`、`MODEL_CAPABILITIES`、`manifest.json.models` 必须使用相同的完整网关
  模型名；斜杠格式 `provider/model` 已废弃。

## 4. 文件职责

| 文件 | 唯一职责 |
| --- | --- |
| `__init__.py` | 只暴露 `create_provider(settings)` |
| `provider.py` | Provider Facade、模块编排、Client 原子切换 |
| `client.py` | 厂商鉴权、HTTP/SDK、连接池、超时和取消 |
| `protocol.py` | Kemo 请求到厂商 DTO、非流式厂商响应到统一结果 |
| `streaming.py` | 厂商流到无信封 `ProviderEvent` |
| `usage.py` | 厂商 Token、缓存、推理、图片、音视频计量语义 |
| `errors.py` | 厂商异常到脱敏 `ErrorObject` |
| `capabilities.py` | 经过真实验证的模型任务、模态、限制和操作 |
| `probe.py` | 厂商自己的低成本真实可达性测试 |
| `manifest.json` | 非敏感静态目录；必须与运行时代码一致，但当前不替代代码注册 |
| `config.json` | 可热更新的非敏感 API 配置 |
| `secrets.json` | 可热更新的厂商密钥，Git 忽略 |
| `requirements.txt` | 可选厂商依赖；部署端必须显式安装，不会自动安装 |

## 5. Provider Facade 契约

所有 Provider 必须实现：

- `provider_id` 和非空 `models`；
- `capabilities(model)`；
- `probe(model, context)`，或明确保留默认 `PROBE_UNSUPPORTED`；
- 对应任务执行方法：LLM 使用 `execute/stream`，Embedding 使用 `embed`，Rerank 使用
  `rerank`；
- `reload_config(settings)` 和 `close()`。

`reload_config()` 必须先完整构造新 Client，验证通过后再一次性替换引用。构造失败不能破坏旧
Client；旧 Client 要允许在途请求排空，不能因轮换 Key 主动关闭正在执行的流。

Provider 只能返回 `ProviderResult`、`ProviderEvent`、`ProviderEmbeddingResult`、
`ProviderRerankResult`、`ProviderProbeResult`、`Usage` 和统一错误。不得返回 FastAPI/HTTP
对象，不得生成 SSE 字节、sequence 或 event_id。

## 6. 能力声明

能力声明采用保守原则：没有真实测试证据就声明不支持。尤其不能因为厂商网页写着“兼容
OpenAI”就默认支持工具、并行工具、推理、JSON Schema、图片或音频。

- LLM：验证非流式、流式、最大输出、停止原因、工具、并行工具、推理和结构化输出；
- Embedding：验证 query/document、维度、批量限制、截断和归一化；
- Rerank：验证候选上限、`top_n`、原始 index、分数方向和返回文档行为；
- `metadata.upstream_model` 必须是厂商真实模型名；
- `extensions.probe` 应说明是否支持、探测方式和是否可能计费。

### 推理能力与思考档位

每个 LLM 模型都必须在 `capabilities.py` 和 `manifest.json` 中显式声明 `reasoning`，但不强制
模型支持推理。模板默认值是：

```python
ReasoningCapabilities(
    supported=False,
    efforts=[],
    summary=False,
    persisted_state=False,
)
```

只有获得厂商文档和真实 Fixture 证明后才能设为 `supported=True`。一旦支持推理，为兼容
kemo-agent，`efforts` 必须填写 `minimal|low|medium|high|max` 五个 Kemo 逻辑档位；`none`
表示关闭，不放入能力列表。厂商实际档位较少时允许显式折叠，例如 `minimal→low`、
`max→high`；只有开关或固定推理时允许五档都映射到厂商默认行为，但必须使用
`extensions.reasoning_policy.mode=provider_default` 标明，不得伪装成五种真实强度。

在 `protocol.py` 中必须建立 Kemo 档位到厂商真实参数的显式映射，即使两边字符串碰巧相同也要
逐项写清楚。请求启用推理后，核心会拒绝能力列表之外的档位；旧客户端通过
`provider_options.reasoning_effort` 传入时，Provider 也必须执行同一检查，不能绕过能力声明。
统一字段 `KemoRequest.reasoning` 始终优先。

五个 Kemo 逻辑档位都要有脱敏请求 Fixture；还要覆盖一个非法档位并确认返回
`REASONING_EFFORT_UNSUPPORTED` 或 Provider 的统一 `VALIDATION_ERROR`。厂商响应中的推理正文、
摘要、可回放状态和 `reasoning_tokens` 分别映射，不能因为支持其中一项就自动声明其余能力。

## 7. 工具调用与多轮

Provider 只翻译工具，不执行工具。必须覆盖：

1. 非流式单工具和并行工具；
2. 流式名称/参数跨 chunk 拼接；
3. 每个调用具有稳定且独立的 `item_id`、`call_id`；
4. 完整参数到齐后恰好发送一次 `TOOL_COMPLETED`；
5. 最终 `ProviderResult.output` 保留全部 `tool_call`，终态为 `requires_action`；
6. 工具结果通过原始 `call_id` 回传，不能混入另一调用；
7. 非法 JSON 保留有限长度的 `arguments_raw` 和脱敏解析错误，不能静默丢失；
8. 厂商要求回放 reasoning/provider state 时由该 Provider 自己保存和恢复，不能污染核心。

### LLM 多模态内容

只有 `ModelCapabilities.input_modalities` 或 `output_modalities` 明确声明且经过真实验证的模态
才能进入转换逻辑。Kemo 图片、音频等媒体块采用以下统一来源结构：

```json
{
  "type": "image",
  "mime_type": "image/png",
  "detail": "auto",
  "source": {
    "kind": "inline_base64",
    "data": "<base64>"
  }
}
```

`source.kind` 当前可能出现：

- `url`、`data_url`、`object_store`：地址放在 `source.uri`；
- `inline_base64`：编码内容放在 `source.data`；
- `provider_file_id`：厂商与文件标识分别放在 `source.provider`、`source.file_id`。

大型或持久媒体优先使用：

```json
{
  "type": "video",
  "asset_id": "asset_...",
  "mime_type": "video/mp4",
  "checksum_sha256": "..."
}
```

Provider 在协议映射时调用 `parse_media_block(..., assets=context.assets)`，得到经过当前 tenant 与
subject 授权的 `asset_path`；该路径仅供 Provider 进程内部读取，绝不能写入厂商可见文本、响应、
日志或公开 API。生成媒体必须调用模板 `store_output_media()` 或等价的
`context.assets.store_output()`，再返回带 `asset_id/mime_type/checksum_sha256` 的 assistant
MessageItem。流式媒体发送一次 `MEDIA_COMPLETED`，最终 `ProviderResult.output` 必须保留完全相同
的 Item。

Provider 只能转换上游端点真实接受的来源类型。不能把 `object_store` 当成公网 URL，不能把
其他厂商的 `provider_file_id` 直接透传，也不能自行读取客户端本地路径。`mime_type` 位于媒体
内容块，不是 `source.media_type`；图片清晰度位于内容块的 `detail`。媒体字段缺失、Base64 为空、
Data URL 类型不符或来源不受上游支持时，必须抛出脱敏 `VALIDATION_ERROR`，禁止构造空媒体请求。

工具结果中的媒体必须同时满足 `tools.multimodal_results=true` 和厂商真实协议；否则明确拒绝，
不能只抽取文字后伪装成完整结果。多模态响应应映射回 assistant `message.content[]` 的对应媒体
块，保留真实 MIME、Asset、checksum 和可验证的 transcript 等字段。

## 8. Usage 与错误

`usage.py` 是唯一允许理解厂商计量的地方。必须确定输入、缓存输入、输出、推理和 total 的包含
关系；流式字段是累计值还是增量值；图片、音频、视频按 Token、秒、张还是像素计费。缺失值
保持 `None`，不能填 `0`，不能用字符数冒充精确 Token。

`errors.py` 至少区分鉴权、限流、超时、厂商不可用、用户参数错误和厂商响应格式错误。只保留
脱敏 request id、HTTP 状态、retry-after 和有限异常类型；不得返回厂商响应正文、Headers、
密钥或签名 URL。只能 `raise ProviderException(error_object)`；`ErrorObject` 是数据模型，不是
异常类，不能直接 `raise`，`ProviderException` 也不能接收字符串。HTTP Client 必须兼容空正文、
非 JSON 正文以及 `error` 为字符串或对象的响应，不能让解析异常覆盖真实 HTTP 状态。

## 9. Provider 自有探测

管理端统一调用 `ProviderPackage.probe()`。每个厂商的 `probe.py` 决定最小真实输入、输出额度、
异步轮询、超时和取消。网关只计时并统一返回，不猜测任务协议。

探测应覆盖真实鉴权、模型路由和最小执行，隔离业务最高权限系统提示词，并尽量降低费用。
探测属于管理操作，不进入正常业务调用统计。真实厂商错误仍通过该包的 `errors.py` 脱敏。

## 10. 配置与重启

- `config.json`：Base URL、超时、代理、非敏感默认 Header；
- `secrets.json`：API Key 等敏感值；
- 两者修改可热更新，只影响新请求；
- Provider Python、manifest、依赖、模型注册和目录结构变化必须重启；
- 厂商依赖不会因存在 `requirements.txt` 自动安装，部署前必须显式安装并记录版本范围。

Provider 权威接口是 `core/provider_contract.py`。模板用于复制结构，绝不能复制一份核心契约到
厂商目录后自行修改。最终完成条件以 [verification.md](verification.md) 为准。
