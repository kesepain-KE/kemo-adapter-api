# Provider 与网关变更验证清单

本清单是智能体报告“创建成功”或“可以发布”前必须完成的最低验证。不能执行的项目必须明确
报告原因和剩余风险，不能用静态阅读代替真实结果。

## 先按任务选验证，不要盲跑真实 API

| 本次任务 | 必须验证 | 不代表完成 |
| --- | --- | --- |
| 创建 Provider | 新包契约、目录一致性、对应任务与错误映射、编译 | 模板 FakeClient 通过不等于上游可调用 |
| 增加模型 | 三处集合、新模型映射、旧模型回归、scope/白名单可见性 | 只在 manifest 加一行 |
| 修改模型能力/推理 | 声明同步、每项新增能力的正反例、每档映射、关闭与优先级 | 能力 API 返回 true |
| 单模型多模态 | 九操作中目标操作的请求/响应、Asset、未声明模态拒绝、旧模型不变 | 文本可达性探测通过 |
| 修改上游 Key | JSON 格式、保留其他项、至少一个启用项、热加载状态 | 保存成功或池中某把 Key 可用 |
| 修改调用 Token/白名单 | 身份和 scopes 不扩大、目录过滤、允许/拒绝用例 | owner 页面看得到模型 |
| 修改本仓库模板/引导文档 | 模板门禁、教学样例、文档链接与 JSON 示例、相关核心回归 | 文档篇幅变长 |
| 修改统计读缓存 | TTL、LRU 容量、参数隔离、写入失效、并发、取消、跨实例落盘读取 | 命中率高或只跑一次查询 |
| 修改密钥预览 | 后端前五后三、短值全隐藏、结构化内容不预览、鉴权与 no-store、前端构建 | 前端拿到完整值后再截取 |
| 修改 Kemo 公开协议 | 两端共享 Fixture、固定摘要、有效/无效正反例、对应领域回归 | 只让网关或 Agent 单侧测试通过 |
| 整包发布/核心改动 | 全量测试、编译、前端生产构建、Git 检查 | 只跑某一个模板测试 |

只改 Provider/文档不要求修改或构建 UI；发布整包仍必须构建前端。
任何需要但未执行的真实测试必须在报告中注明，不能用离线样例替代。

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
  `supported=false, efforts=[]`；优先验证五档，只公开有证据的集合；只有开关无强度映射可用空档位；
- 每个公开档位必须在 `protocol.py` 中逐档映射到厂商真实参数；厂商档位较少或只有开关时
  经验证的折叠通过 `reasoning_policy` 标注；每档均有 Fixture，非法档位会被拒绝，不能直接盲透传
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

修改 `core/models.py`、公开请求/响应、能力声明、Asset、Usage、工具、多模态、Embedding、Rerank
或 SSE 线路字段时，先执行 Kemo 1.0 共享契约门禁：

```powershell
python -m tests --suite kemo-contract -q
python -m tests.contracts.kemo_v1 --peer-root E:\code\kemo-agent -q
```

第二条命令中的路径是示例，必须指向实际 kemo-agent 仓库。两边的 `wire.json`、`manifest.json`
和 `fixture_loader.py` 固定摘要要同步；不能通过删除无效用例、放宽 Schema 或仅改一侧 Fixture
消除失败。此门禁只验证离线协议兼容，不替代真实代理、断网恢复或 Provider Golden Fixture。

修改模板/操作配方时先在项目根目录执行（Windows/Linux 相同）：

```sh
python -m tests --suite templates -q
python -m tests --suite protocol --suite providers --suite runtime -q
python -m compileall -q template
git diff --check
```

`catalog_contract.validate_catalog()` 会定位缺失模型、manifest 字段漂移和推理档位映射缺口，
但不会验证厂商协议本身。应保留复制后 test_contract.py 的通用断言，另补真实脱敏 Fixture。

整包发布或核心改动时，在项目根目录执行：

```powershell
python -m compileall -q core api storage web/backend template tests update setup.py start_web.py restart.py
python -m tests -q
pnpm --dir web/frontend run build
python -m tests --suite restart-e2e -q
git diff --check
```

使用网关实际部署的 Python 环境；若 `python` 不是目标解释器，先定位并替换为对应解释器绝对
路径，不能为了通过检查临时安装到另一个环境。

统一入口及分组说明见 [tests/README.md](../tests/README.md)。默认运行源码与模板测试，
不自动扫描本机 `providers/`；若本次改了实际厂商，另外运行其契约文件，或显式选择
`python -m tests --suite local-providers -q`。真实进程替换测试使用 `--suite restart-e2e`，
需要已构建前端，在临时项目中运行，不重启用户正在使用的网关。
完整发布步骤及版本来源见 [发布配方](release.md)。Windows 实测不能代替 Linux 实测；
尚未运行远程 CI 时明确报告“Linux 待 CI”，不能写成双平台已经全部通过。

新 Provider 还必须执行自己的 `test_contract.py` 和脱敏 Golden Fixture。真实探测仅在用户授权
费用和目标环境后执行。验证后检查 `git diff --check`，并确认 `git status --short` 中没有密钥、
运行数据库、日志、缓存或意外新增目录。

## 8. 完成报告

最终报告必须包含：修改范围、支持的真实模型与任务、探测结果、测试数量、是否需要重启、仍未
验证的能力，以及 Provider 是否会随 Git 推送。不得包含任何完整密钥或敏感响应。
