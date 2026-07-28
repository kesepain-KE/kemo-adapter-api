# Provider Package 模板

提供创建新厂商 Provider 包的保守骨架。本目录是唯一 Provider 模板源，不依赖部署端是否存在
某个测试厂商。

## 使用方式

```bash
# 复制本目录为 providers/<provider_id>/
cp -r template/provider/ providers/deepseek_v2/
```

PowerShell：

```powershell
Copy-Item -Recurse template/provider providers/deepseek_v2
```

复制后立即删除 `__pycache__` 和 `.pyc`。将 `manifest.json.example`、`config.json.example`、
`secrets.json.example` 改为对应 JSON 文件；需要额外 SDK 时才创建 `requirements.txt`。将
`test_contract.py` 必须替换为目标厂商的脱敏 Golden Fixture。所有运行路径
中的 `Example`、`example`、`.invalid`、`vendor_`、TODO 和 `NotImplementedError` 必须清除。

文件夹名、`provider_id` 和 manifest 中的 ID 必须一致。当前核心正式支持 `llm`、
`embedding`、`rerank`，其中 Kemo `llm` 合同已承载 conversation、vision、图片生成/编辑、ASR、
TTS、语音转换、视频理解和视频生成九种操作。实时会话或合同外任务仍必须先扩展核心公开协议。

公开模型名固定为 `<provider_id>-<厂商内部模型名>`，例如
`deepseek-deepseek-v4-flash`。厂商内部模型名可含更多连字符；协议映射只能移除自身完整的
`<provider_id>-` 前缀，不能按任意连字符拆分。斜杠格式 `provider_id/model` 不受支持。

## 协议转换原理

一个 Provider 包的本质是**翻译器**，把厂商私有协议转成网关的统一事件：

```
厂商 HTTP API
    ↓  client.py（鉴权、HTTP 请求）
    ↓  probe.py（厂商自有最小真实探测）
    ↓  protocol.py（请求参数翻译）
    ↓  streaming.py（SSE → ProviderEvent）
    ↓  usage.py（计费单位标准化）
    ↓  errors.py（错误码 → ErrorObject）
    ▼
ProviderEvent / ProviderResult  ← 网关认识的唯一语言
    ↓  core/event_assembler.py（加 SSE 信封、sequence）
    ▼
Kemo SSE 事件（输出给 kemo-agent）
```

每层只做一件事，不得越界。

## 目录职责

| 文件 | 只负责 |
| --- | --- |
| `__init__.py` | 暴露 `create_provider(settings)` 工厂函数 |
| `provider.py` | 对网关的 Facade，编排本目录所有模块 |
| `probe.py` | 厂商自有的最小真实调用与可达性判定 |
| `client.py` | 厂商鉴权、HTTP/SDK、超时和取消 |
| `protocol.py` | 厂商 DTO、请求映射和非流式响应映射 |
| `media.py` | 解析真实 Kemo 媒体来源；由协议层决定厂商支持的来源与转换 |
| `streaming.py` | 厂商流解析为 `ProviderEvent` |
| `usage.py` | 厂商计量语义转换为统一 `Usage` |
| `errors.py` | 厂商错误转换为统一 `ErrorObject` |
| `capabilities.py` | 真实模型能力和限制 |
| `manifest.json` | 静态模型目录；必须与代码一致，当前不替代运行时代码注册 |
| `config.json` | 可热更新的 Endpoint、超时等 API 配置 |
| `secrets.json` | 可热更新的厂商密钥，不上传 Git |
| `test_contract.py` | 脱敏 Golden Fixture 和 Provider 契约回归测试 |

## 实现路径

从以下方向依次填充：

### 0. 可达性探测

每个 Provider 必须通过 `provider.py` 的 `probe()` 调用本目录 `probe.py`，不得要求网关核心
猜测厂商协议。探测应覆盖鉴权、模型路由和一次最小真实执行，并返回统一
`ProviderProbeResult`。LLM 模板默认要求只回复 `OK`；Embedding 和 Rerank 必须替换为各自
真实且低成本的测试输入。图片、音频、视频等模型应使用本厂商真正支持的最小媒体和端点，
不能继续复用文本探测。未实现时保留核心默认的
`PROBE_UNSUPPORTED`，禁止伪造可达。

探测属于管理操作，不注入业务最高权限系统提示词，也不进入正常业务调用统计；厂商仍可能
对真实探测收取少量费用。

### 1. 入站方向（Kemo → 厂商 API）

```python
# protocol.py
KemoRequest.input[]          → 厂商 messages[]
KemoRequest.tools[]          → 厂商 tools[]
KemoRequest.generation       → 厂商 max_tokens, temperature, ...
KemoRequest.reasoning        → 厂商 thinking/reasoning_effort 参数
```

每个 LLM 模型必须在 `capabilities.py` 与 `manifest.json` 中显式填写 `reasoning`，但不要求所有
模型都支持思考。默认使用 `supported=False, efforts=[]`。一旦声明支持推理，为兼容
kemo-agent，`efforts` 必须暴露 `minimal|low|medium|high|max` 五个 Kemo 逻辑档位。
厂商只有三档、两档或开关时，可以把多个逻辑档位折叠到同一真实值或厂商默认值，但必须在
`extensions.reasoning_effort_map` 与 `extensions.reasoning_policy` 中公开；不能把不存在的值
原样发送给上游。`none` 表示关闭，不属于能力档位；厂商的 `xhigh` 可作为 Kemo `max` 的
上游映射值。

必须在本文件夹的 `protocol.py` 中逐档映射到厂商真实字段和值，不能直接假定名称相同。
`KemoRequest.reasoning` 优先；兼容旧客户端的 `provider_options.reasoning_effort` 也必须经过同一
能力列表检查。五个逻辑档位和至少一个非法档位都要加入脱敏 Fixture。

如果声明 LLM 多模态，必须读取 Kemo 的真实媒体结构：内容块使用 `mime_type`，媒体来源使用
`source.kind`，并按来源读取 `source.uri` 或 `source.data`。不得读取不存在的
`source.media_type`，不得把空媒体转换成 `data:<mime>;base64,` 后发给上游。完整结构、来源限制和
验收用例见 `ADD_DIY/provider-package.md` 与 `ADD_DIY/verification.md`。

大型输入通过 `asset_id` 引用。调用 `parse_media_block(..., assets=context.assets)` 后，只能在
Provider 内部读取返回的 `asset_path`，不得把本地路径放入上游提示词、日志或公开响应。生成的
图片、音频、视频和文件必须先调用 `store_output_media()` 或
`context.assets.store_output()`，再返回包含 `asset_id`、真实 `mime_type` 和
`checksum_sha256` 的 assistant MessageItem。

### 2. 出站方向（厂商 API → ProviderResult/ProviderEvent）

```python
# protocol.py (非流式)
厂商 choices[].message       → ProviderResult.output[]
厂商 usage                   → Usage

# streaming.py (流式)
厂商 SSE delta               → ProviderEvent(TEXT_DELTA / REASONING...)
厂商工具参数 delta            → ProviderEvent(TOOL_ARGUMENTS_DELTA)
完整工具调用                  → ProviderEvent(TOOL_COMPLETED)
媒体 Asset 完成                → ProviderEvent(MEDIA_COMPLETED)
厂商 finish_reason           → ProviderEvent(COMPLETED / FAILED)
```

工具调用必须同时满足：

1. 每个并行工具调用拥有独立 `item_id` 和厂商 `call_id`；
2. 流式参数片段使用 `TOOL_ARGUMENTS_DELTA`，完整 JSON 到齐后必须发送一次
   `TOOL_COMPLETED`；
3. 终态 `ProviderResult.output` 仍要包含全部 `tool_call` item，状态为
   `requires_action`；
4. Provider 只翻译工具调用，绝不能自行执行工具；
5. 参数 JSON 无效时保留有限长度的 `arguments_raw` 和脱敏 `parse_error`，不能静默丢失。

媒体流还必须满足：`MEDIA_COMPLETED.item` 是完整 assistant MessageItem，同一 Item 只完成一次，
并原样保留在最终 `ProviderResult.output`；大型媒体只通过 Asset 下载，不通过 SSE 传正文。

### 3. 错误映射

```python
# errors.py
HTTP 401 → AUTHENTICATION_ERROR
HTTP 429 → RATE_LIMITED (retryable=True)
HTTP 500 → PROVIDER_UNAVAILABLE (retryable=True)
```

## 强制边界

1. 不得从 `api` 导入任何代码。
2. 不得生成 SSE 字节、`event_id` 或 `sequence`。
3. 不得把厂商原始 Usage 交给核心猜测。
4. 不得返回未经脱敏的错误体、Headers、签名 URL 或 Provider State。
5. 所有 `provider_options` 必须在本包内按白名单解析，禁止透传任意 Header、URL 或密钥。
6. 参数校验失败必须抛出携带 `ErrorObject` 的 `ProviderException`，禁止把字符串直接传给
   `ProviderException`。
7. `RequestContext.gateway_system_prompt` 必须映射到厂商可用的最高指令层，不得从请求
   `extensions` 接受同名内容，也不得降级为 user message。
8. 统一协议字段优先；为旧客户端兼容而消费的 `provider_options` 不得再次覆盖统一字段。
9. 模型测试协议必须封装在本包 `probe.py`，核心只消费 `ProviderProbeResult`。
10. `ErrorObject` 不能直接抛出；Client 与协议层必须抛出 `ProviderException(ErrorObject)`，并让
    非 JSON 或形状异常的错误正文保留其真实 HTTP 状态，不能被二次解析异常覆盖。

## 发布前检查

必须逐项完成 [ADD_DIY/verification.md](../../ADD_DIY/verification.md)。至少确认模型三处键集合一致、能力声明有真实证据、
未知选项被拒绝、工具流只有一个终态、Usage 不补零、错误不泄密、Provider 自有探测可用，
并执行完整后端测试和前端构建。

`config.json` 和 `secrets.json` 更新无需重启。任何 Python、manifest、依赖或协议
代码变化都必须重启。
