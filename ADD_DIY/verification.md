# Provider 与网关变更验证清单

本清单是智能体报告“创建成功”或“可以发布”前必须完成的最低验证。不能执行的项目必须明确
报告原因和剩余风险，不能用静态阅读代替真实结果。

## 1. 目录与占位符

新 Provider 最少包含：

```text
providers/<provider_id>/
├─ __init__.py
├─ provider.py
├─ client.py
├─ protocol.py
├─ streaming.py          # 声明支持流式时必需
├─ usage.py
├─ errors.py
├─ capabilities.py
├─ probe.py
├─ manifest.json
├─ config.json
├─ secrets.json          # 本地存在、Git 忽略，不得读取后回显
└─ test_contract.py      # 必须替换为目标厂商脱敏 Fixture
```

发布前搜索并清除复制残留：`Example`、`example-`、`api.example.invalid`、`vendor_`、未处理的
`TODO` 和 `NotImplementedError`。注释中明确保留的后续任务除外，但运行路径不能仍依赖占位符。
删除 Provider 目录中的 `__pycache__`、`.pyc`、临时响应和抓包文件。

## 2. 身份与模型路由

- 文件夹名、`ProviderPackage.provider_id`、`manifest.json.provider_id` 完全一致；
- `provider_id` 使用小写字母、数字、下划线，不能以下划线开头；建议以小写字母开头；
- 每个公开模型名严格为 `<provider_id>-<上游模型名>`，只移除一次完整厂商前缀；
- `provider.models`、`MODEL_CAPABILITIES` 和 `manifest.json.models` 的键集合完全一致；
- 不存在重复模型、斜杠模型名、空模型名或未经用户确认的模型。

## 3. 能力与任务

- `ModelCapabilities.task` 与实际合同一致，只能声明核心当前支持的 `llm|embedding|rerank`；
- 多模态 Provider 必须逐项声明 `extensions.operations.<name>.supported`，并与输入/输出模态一致；
  TTS、ASR、图片/视频生成等可使用既有 Kemo 操作，但必须映射到厂商真实端点，不能统一发送到
  `/chat/completions`；实时会话等合同外任务不得伪装成现有操作；
- 流式、工具、并行工具、推理、结构化输出和多模态均以真实测试结果声明；
- 未验证能力必须是 `false` 或不声明，不能按厂商宣传页推断；
- 每个 LLM 模型在 `capabilities.py` 与 `manifest.json` 的 `reasoning` 声明一致；不支持时
  `supported=false, efforts=[]`，支持时必须暴露 `minimal|low|medium|high|max` 五个逻辑档位；
- 五个逻辑档位必须在 `protocol.py` 中逐档映射到厂商真实参数；厂商档位较少或只有开关时
  显式折叠并通过 `reasoning_policy` 标注；每档均有 Fixture，非法档位会被拒绝，不能直接盲透传
  `provider_options.reasoning_effort`；
- Embedding 维度、输入类型、批量上限和归一化语义真实；
- Rerank 的 index、`top_n`、分数方向和返回原文行为真实；
- `probe.py` 执行低成本真实调用；未实现时返回 `PROBE_UNSUPPORTED`，不能伪造可达。

## 4. 请求、工具和流

- 未知 `provider_options` 被拒绝，不能透传任意 URL、Header 或鉴权字段；
- 最高权限提示词只从 `RequestContext.gateway_system_prompt` 获取；
- 非流式和流式普通文本各有脱敏 Golden Fixture；
- 工具调用覆盖单工具、并行工具、参数分片和非法 JSON；
- 流式参数完整后产生一次 `TOOL_COMPLETED`，整个流只有一个终态；
- ProviderEvent 不包含 SSE 信封、sequence 或 event_id；
- 取消、超时和厂商断流能产生统一脱敏错误或取消终态。

声明多模态时还必须覆盖：

- Asset 输入通过 `context.assets.resolve()` 读取且跨 subject 不可见；不得把本地路径写入公开
  响应或上游提示词；
- 使用 Kemo 客户端真实结构测试 `source.kind=url|data_url|inline_base64`，字段位于
  `source.uri/source.data`，MIME 位于内容块 `mime_type`；
- URL、Data URL、Base64、图片 `detail` 和厂商实际支持的音频格式转换后字段完全正确；
- 空 Base64、空 URI、不支持的 `object_store/provider_file_id` 和错误 MIME 明确返回
  `VALIDATION_ERROR`，不能发送空 Data URL；
- 文本模型拒绝图片/音频，媒体模型拒绝未声明的输入/输出模态；
- 图片、音频、视频生成结果通过 `context.assets.store_output()` 登记，响应包含
  `asset_id/mime_type/checksum_sha256` 并能经鉴权接口下载；
- 多模态响应能被真实 Kemo 响应模型反序列化，流式 `output_media.completed` 只有一次，且
  Item 与统一终态完全一致；
- 测试 Fixture 不得使用 Provider 自己臆造、但真实 Kemo 客户端不会产生的字段结构。

## 5. Usage 与错误

- Token、缓存 Token、推理 Token、媒体单位的包含关系来自厂商权威文档或响应验证；
- 缺失值使用 `None`，不能用 `0` 冒充；累计流 Usage 不重复相加；
- `measurement.mode`、`exact_fields` 和 `estimated_fields` 与证据一致；
- 错误映射至少覆盖鉴权、限流、超时、上游 5xx、无效请求和响应格式错误；
- HTTP 错误测试覆盖空正文、非 JSON、`error` 字符串和 `error` 对象；真实状态不得被 JSON
  解析错误、`AttributeError` 或 `TypeError` 覆盖；
- Client 只能抛出 `ProviderException(ErrorObject)`，不能直接抛出 `ErrorObject`；
- 错误只保留脱敏 request id、状态和有限详情，不包含响应正文、Headers 或密钥。

## 6. 配置热更新

- `config.json` 只放非敏感 Base URL、超时和允许的默认 Header；
- `secrets.json` 只放厂商密钥并保持 Git 忽略；
- `reload_config()` 先构造并验证新 Client，再原子切换；失败时旧 Client 继续服务；
- 新请求使用新配置，在途请求不被关闭；Python、manifest、依赖和新增模型仍需重启。

## 7. 必须执行的验证

在项目根目录执行：

```powershell
python -m compileall core api web/backend template/provider
python -m pytest -q
Set-Location web/frontend
npm run build
```

使用网关实际部署的 Python 环境；若 `python` 不是目标解释器，先定位并替换为对应解释器绝对
路径，不能为了通过检查临时安装到另一个环境。

新 Provider 还必须执行自己的 `test_contract.py` 和脱敏 Golden Fixture。真实探测仅在用户授权
费用和目标环境后执行。验证后检查 `git diff --check`，并确认 `git status --short` 中没有密钥、
运行数据库、日志、缓存或意外新增目录。

## 8. 完成报告

最终报告必须包含：修改范围、支持的真实模型与任务、探测结果、测试数量、是否需要重启、仍未
验证的能力，以及 Provider 是否会随 Git 推送。不得包含任何完整密钥或敏感响应。
