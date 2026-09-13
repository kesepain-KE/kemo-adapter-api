# 智能体操纵 Kemo 网关索引

本文件是自动化智能体修改网关时的第一入口。先读取本页，再读取 `ADD_DIY/README.md` 和任务
对应手册。不得仅凭通用 OpenAI 兼容经验、旧对话或厂商宣传页创建 Provider。

**不知道先读哪份文件时，直接打开 [按任务执行的配方](ADD_DIY/tasks.md)。**
先填写 [操作任务卡](template/examples/task-card.md)，只选择本次目标，不要求一次读完全部手册。
创建厂商、增加模型、更新能力、给单个模型加多模态、修改密钥分别有独立步骤与完成判据。

本引导已按网关 **0.8.0 / Kemo 1.0** 核对；后续操作仍以目标项目 `version.json` 与源码为准。
整包发布走 [发布配方](ADD_DIY/release.md)，测试统一从 [测试入口](tests/README.md) 选择，
不要沿用旧的 `tests/test_*.py` 扁平路径。

## 小模型最短执行流程

只按下面四步做，不要猜：

1. **先判断目标**：新增厂商、增加模型、修改上游密钥、修改网关调用 Token，四者不要混做。
2. **只读对应目录**：先看现有文件和测试，再改目标文件；不要用模板覆盖已有 Provider。
3. **只改允许的位置**：按下表执行；上游厂商密钥写 `secrets.json`，网关调用 Token 写 `api/keys.json`。不要把它们和 `.env` 中的 Web/状态凭据混用。
4. **验证并报告**：按 `ADD_DIY/verification.md` 的任务矩阵执行测试；最后说明离线验证、真实测试、是否热更新或待重启。只有修改前端或发布整包时才必须前端构建。

| 目标 | 只改这些文件 | 重启 | 绝对不要做 |
| --- | --- | --- | --- |
| 新增 Provider | 复制 `template/provider/` 到 `providers/<id>/`，再改模板列出的文件 | 是 | 不改 `core/`，不改其他 Provider |
| 增加模型 | `provider.py` 的模型集合、`capabilities.py`、`manifest.json`，以及协议映射和测试 | 是 | 不新建 Provider 目录，不猜能力 |
| 更新模型能力/思考档位 | 指定模型的 capabilities、manifest、协议映射与测试 | 是 | 不只改能力开关，不改其他模型 |
| 单个模型增加多模态 | 指定模型的能力及该 Provider 的媒体链路 | 是 | 不整厂商开启模态，不修改核心校验来放行 |
| 修改上游密钥 | `providers/<id>/secrets.json` 的 `api_keys` 数组 | 否，热更新 | 不写 `.env`、代码、日志或 Markdown |
| 修改网关调用 Token | `api/keys.json` | 否，热更新 | 不改 Provider 的 `secrets.json` |
| 修改 Base URL/请求头 | `providers/<id>/config.json` | 否，热更新 | 不把密钥放进 `config.json` |
| 修改环境变量 | `.env` | 是 | 不报告“已热更新” |

只有用户明确要求“首次单 Token 快速启动”或应急恢复时，才可取消 `.env.example` 中
`GATEWAY_API_KEY` 的注释并写入 `.env`；它不是 Provider 上游密钥，也不是日常热更新方案。

### 三个可直接照做的配方

**创建 Provider**：复制模板 → 改 `provider.py`、`protocol.py`、`streaming.py`、`usage.py`、
`errors.py`、`capabilities.py`、`probe.py`、`manifest.json`、`config.json`、`test_contract.py` →
让 `provider.models`、`MODEL_CAPABILITIES`、`manifest.models` 三处模型键完全相同 → 跑契约测试。

**增加模型**：不复制目录 → 在同一个 Provider 的上述三处增加完整模型名
`<provider_id>-<原始模型名>` → 同步请求映射、能力声明、推理档位、工具/多模态测试 → 代码或清单变化后重启。

**修改密钥**：只编辑 `providers/<id>/secrets.json`：

```json
{"api_keys":[{"key_id":"primary","api_key":"上游密钥","enabled":true}]}
```

至少保留一个**启用**密钥。需要停用整个 Provider 时，使用 Provider 启停开关，不要把密钥池全部设为禁用。

## 操作索引

| 目标 | 必读文件 | 主要操作范围 |
| --- | --- | --- |
| 创建新厂商或增加模型 | `ADD_DIY/README.md`、`ADD_DIY/provider-package.md`、`ADD_DIY/verification.md` | `providers/<provider_id>/` |
| 修改厂商协议、流、工具、Usage、错误或探测 | `ADD_DIY/provider-package.md`、`ADD_DIY/architecture.md` | 对应 Provider 包 |
| 使用厂商模板 | `template/README.md`、`template/provider/README.md` | 只复制到新 Provider，不覆盖现有实现 |
| 热更新厂商 API 配置或密钥 | `ADD_DIY/keys-and-secrets.md`、`ADD_DIY/architecture.md` | Provider 的 `config.json` / `secrets.json` |
| 热更新网关调用密钥或模型白名单 | `ADD_DIY/keys-and-secrets.md` | `api/keys.json` |
| 热更新 API 启停、最高提示词、禁用模型/厂商 | `ADD_DIY/architecture.md` | `api/runtime.json` / `core/live_control.json` |
| 修改环境变量 | `ADD_DIY/keys-and-secrets.md` | `.env`，必须重启 |
| 修改公开 LLM/Embedding/Rerank API | `api.md`、`ADD_DIY/architecture.md`、`ADD_DIY/verification.md` | `api/`、`core/models.py` |
| 增加图片/音频/视频/文件或媒体生成能力 | `api.md`、`ADD_DIY/provider-package.md`、`ADD_DIY/verification.md` | 使用既有 Kemo 多模态操作与 Asset 合同，只在目标 Provider 内实现厂商转换 |
| 增加既有九种操作之外的新任务或实时会话 | `api.md`、`ADD_DIY/provider-package.md`、`ADD_DIY/architecture.md`、`ADD_DIY/verification.md` | 先扩展核心公开协议，禁止用厂商专用旁路伪装支持 |
| 接入全局只读状态感知 | `api.md` | `GET /status` 与独立 `STATUS_TOKEN` |
| 修改执行、恢复或平滑重启 | `ADD_DIY/architecture.md`、`ADD_DIY/verification.md` | `core/`、`restart.py` |
| 修改管理网页 | `web/README.md` | `web/`，不得写入公开 `api.md` |
| 发布前验证 | `ADD_DIY/verification.md` | 测试、构建、版本和敏感信息检查 |

## Provider 创建的硬约束

1. 新厂商只从 `template/provider/` 复制；`providers/_template` 不存在，也不得重新创建。
2. 文件夹名、`ProviderPackage.provider_id`、`manifest.json.provider_id` 必须完全一致。
3. 公开模型名固定为 `<provider_id>-<厂商原始模型名>`。例如厂商 `deepseek`、上游
   `deepseek-v4-flash` 对外为 `deepseek-deepseek-v4-flash`。
4. `provider.models`、`MODEL_CAPABILITIES` 与 `manifest.json.models` 的键集合必须一致。
5. 当前核心任务为 `llm`、`embedding`、`rerank`；Kemo `llm` 响应合同已原生承载 conversation、
   vision、image_generation、image_edit、audio_transcription、speech_generation、
   speech_to_speech、video_understanding、video_generation 九种操作。能力声明、输入/输出模态与
   `extensions.operations` 必须同时成立；实时双向会话等合同外任务仍需先扩展核心。
6. 每个厂商通过自己的 `probe.py` 实现真实可达性探测；核心不猜厂商协议。
7. 每个 LLM 模型必须显式声明推理能力。不支持时使用 `supported=false, efforts=[]`；支持时只列出
   已经验证并在 `protocol.py` 中映射的 Kemo 五档 `minimal|low|medium|high|max`。`xhigh` 只作为
   旧客户端兼容输入或厂商映射别名，不作为模板默认档位。只有思考开关而
   没有强度档位时使用 `supported=true, efforts=[]`，不得臆造或盲透传档位。
   优先完成五档的可信映射；部分档位尚未验证时只公开已验证集合并报告缺口。
   统一口径见 `ADD_DIY/model-maintenance.md`，不能为凑齐档位伪造上游能力。
8. 未经真实验证的流式、工具、并行工具、推理、结构化输出和多模态能力必须声明为不支持。
9. `providers/*` 默认不进入 Git。最终报告必须说明新厂商是部署端本地包还是经用户授权后随仓库发布。

## 架构与安全边界

1. 厂商差异必须留在 `providers/<provider_id>/`，核心不得按厂商名称写分支。
2. 厂商 Token、缓存、推理和媒体计量先在该包 `usage.py` 中解释；核心不得猜测或补零。
3. Provider 只能输出统一 Provider 对象，不能生成 HTTP Response、SSE 字节、sequence 或
   event_id，也不能自行执行 kemo-agent 工具。
4. `.env`、真实 API Key、Bearer Token、签名 URL、Provider State 和原始错误正文不得写入
   源码、文档、Fixture、日志、终端摘要或 Git。
   网关管理端虽然提供 owner + 会话 + 同源 + CSRF 保护的短时查看接口，但智能体不得为了确认密钥而
   调用该接口、截屏或回显结果；修改密钥时只操作目标配置并报告脱敏 `key_id`。
5. 租户、主体和资源权限只能来自认证 Principal，不能相信正文 `metadata.user`。
6. 未知 `provider_options` 必须拒绝；不得透传任意 URL、Header 或密钥。
7. `api.md` 只记录网关对外 API；Web 管理接口记录在开发目录的 Web 后端 API 文档。
8. 只有 `ADD_DIY/architecture.md` 热插拔清单中的配置无需重启。环境变量、Python、模型注册、
   manifest、依赖和网页构建变化都必须重启。
9. 未获授权不得调用付费厂商接口、创建资源、撤销密钥或扩大 scopes。
10. Kemo 媒体块必须按 `source.kind` 解析；不得自行发明 `source.media_type` 等不存在的字段，也
    不得在媒体为空或无法解析时向上游发送空 Data URL。
11. 同一 Provider 的 `api_keys` 会按健康状态故障转移；路由器在发起上游尝试前预留下一游标位置，
    并发请求尽量按开始顺序分摊，但不承诺严格均匀。只有明确的密钥级错误才切换；普通模型权限不足或
    参数错误的 403 不算密钥错误。本次请求尚未产生流式输出时可切换，所有候选密钥失败后才向智能体
    返回最终错误；A 的中间错误不得先发给智能体。
12. Provider 的 HTTP 错误必须保留 `ErrorObject.provider_status`；不能只把状态码放进 `details`，否则
    网关无法可靠识别 401/402/403/429 并切换密钥。
13. 为 Provider 增加密钥池时，取消操作必须路由回产生该 `provider_response_id` 的同一 Client；
    不能仅按当前轮询指针选择密钥。厂商专用 TTS、ASR、图片或实时旁路如果没有接入同一个密钥路由器，
    必须在能力说明和最终报告中明确“该专用路径尚不支持密钥故障转移”，不得宣称整个 Provider 已完整支持。

14. Provider 工具调用必须先完成 JSON 对象解析和请求 Schema 校验，再进入 `tool_call.completed` 执行边界；同一响应中的并行调用按批次原子发布，任一调用非法时统一返回 `response.incomplete`，不得先泄露或执行同批其他调用。

15. 网关统计读缓存位于 `storage/read_cache.py`，不是 Provider 的 Token 缓存计量。
    修改厂商能力、密钥或 Usage 不需要改此缓存，不得把密钥或鉴权结果放进去。
    写入失效、TTL、容量及取消边界见 [统计存储](storage/README.md)。
16. 管理界面凭据掩码统一由后端生成，显示前五后三；短值完全隐藏。掩码不能用于请求鉴权，
    不可把掩码写回 secrets.json，更不能为了做预览让前端先取得完整密钥。

## 标准完成条件

- 新 Provider 运行路径没有模板占位符；提交内容没有运行缓存、抓包或真实密钥残留；
- 模型命名、manifest、能力和运行时模型集合一致；
- 普通文本、流式、工具、Usage、错误和 Provider 自有探测均有对应测试；
- 按任务验证矩阵执行测试、编译与必要构建；整包发布需全量测试和前端构建，
  已有 skipped 和未执行项目必须解释；`git diff --check` 通过；
- 新厂商通过自己的脱敏 Golden Fixture，真实探测仅在用户授权后执行；
- 工具参数、深层 Schema、并行调用和流式终态安全测试必须在 CI 中单独可定位；
- 最终报告说明真实支持能力、未知能力、是否需要重启、是否随 Git 发布，且不回显任何密钥。

具体流程从 [ADD_DIY/README.md](ADD_DIY/README.md) 开始。
