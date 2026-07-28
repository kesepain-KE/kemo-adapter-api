# 智能体操纵 Kemo 网关索引

本文件是自动化智能体修改网关时的第一入口。先读取本页，再读取 `ADD_DIY/README.md` 和任务
对应手册。不得仅凭通用 OpenAI 兼容经验、旧对话或厂商宣传页创建 Provider。

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
   已经验证并在 `protocol.py` 中映射的 `minimal|low|medium|high|xhigh|max` 档位。只有思考开关而
   没有强度档位时使用 `supported=true, efforts=[]`，不得臆造或盲透传档位。
8. 未经真实验证的流式、工具、并行工具、推理、结构化输出和多模态能力必须声明为不支持。
9. `providers/*` 默认不进入 Git。最终报告必须说明新厂商是部署端本地包还是经用户授权后随仓库发布。

## 架构与安全边界

1. 厂商差异必须留在 `providers/<provider_id>/`，核心不得按厂商名称写分支。
2. 厂商 Token、缓存、推理和媒体计量先在该包 `usage.py` 中解释；核心不得猜测或补零。
3. Provider 只能输出统一 Provider 对象，不能生成 HTTP Response、SSE 字节、sequence 或
   event_id，也不能自行执行 kemo-agent 工具。
4. `.env`、真实 API Key、Bearer Token、签名 URL、Provider State 和原始错误正文不得写入
   源码、文档、Fixture、日志、终端摘要或 Git。
5. 租户、主体和资源权限只能来自认证 Principal，不能相信正文 `metadata.user`。
6. 未知 `provider_options` 必须拒绝；不得透传任意 URL、Header 或密钥。
7. `api.md` 只记录网关对外 API；Web 管理接口记录在开发目录的 Web 后端 API 文档。
8. 只有 `ADD_DIY/architecture.md` 热插拔清单中的配置无需重启。环境变量、Python、模型注册、
   manifest、依赖和网页构建变化都必须重启。
9. 未获授权不得调用付费厂商接口、创建资源、撤销密钥或扩大 scopes。
10. Kemo 媒体块必须按 `source.kind` 解析；不得自行发明 `source.media_type` 等不存在的字段，也
    不得在媒体为空或无法解析时向上游发送空 Data URL。

## 标准完成条件

- 没有模板占位符、缓存、抓包或真实密钥残留；
- 模型命名、manifest、能力和运行时模型集合一致；
- 普通文本、流式、工具、Usage、错误和 Provider 自有探测均有对应测试；
- Python 完整编译、全部测试无 skipped、前端生产构建通过、`git diff --check` 通过；
- 新厂商通过自己的脱敏 Golden Fixture，真实探测仅在用户授权后执行；
- 最终报告说明真实支持能力、未知能力、是否需要重启、是否随 Git 发布，且不回显任何密钥。

具体流程从 [ADD_DIY/README.md](ADD_DIY/README.md) 开始。
