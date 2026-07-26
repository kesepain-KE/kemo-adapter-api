# 智能体操纵网关索引

本文件是自动化智能体修改 Kemo 网关时的第一入口。具体操作规程放在 `ADD_DIY/`，本页只定义
导航、权限边界和完成标准。

## 操作索引

| 目标 | 必读文件 | 主要操作范围 |
| --- | --- | --- |
| 创建新厂商 | `ADD_DIY/provider-package.md` | `providers/<provider_id>/` |
| 修改厂商协议、模型或 Usage | `ADD_DIY/provider-package.md` | 对应 Provider 包 |
| 热更新厂商 API 配置/密钥 | `ADD_DIY/keys-and-secrets.md` | Provider 的 config/secrets |
| 热更新网关 API 配置/密钥 | `ADD_DIY/keys-and-secrets.md` | `api/runtime.json` / `api/keys.json` |
| 热更新最高权限提示词、禁用模型/厂商 | `ADD_DIY/architecture.md` | `core/live_control.json` |
| 修改环境变量 | `ADD_DIY/keys-and-secrets.md` | `.env`，必须重启 |
| 修改公开 LLM/Embedding/Rerank API | `api.md`、`ADD_DIY/verification.md` | `api/`、`core/models.py` |
| 修改网关执行与恢复 | `ADD_DIY/architecture.md` | `core/` |
| 修改管理网页 | `web/README.md` | `web/`，不得污染 `api.md` |
| 平滑重启网关 | `ADD_DIY/architecture.md` | `restart.py` / Web owner API |
| 发布前验证 | `ADD_DIY/verification.md` | `tests/`、`version.json` |

## 强制边界

1. 厂商差异必须留在 `providers/<provider_id>/`，核心不得按厂商名称写分支。
2. 厂商原始 Token/媒体计量必须先在该包的 `usage.py` 中解释，再交给核心聚合。
3. Provider 包只能输出对应任务的 `ProviderResult`、`ProviderEvent`、`ProviderEmbeddingResult`、
   `ProviderRerankResult`、`Usage` 和统一错误，不能生成 SSE sequence、event_id 或 HTTP Response。
4. `.env`、真实 API Key、Bearer Token、签名 URL 和 Provider State 明文不得写入源码、文档、
   测试快照、日志或 Git。
5. 智能体不得通过正文 `metadata.user` 决定租户或资源授权，必须使用认证后的 Principal。
6. `api.md` 只记录网关对外提供的 LLM/Embedding/Rerank/Asset API，不记录 Web 管理端内部接口。
7. 修改协议、Provider 契约或公开 API 后必须同步测试、`api.md` 和 `version.json`。
8. 只有厂商 API 配置、网关 API 配置、最高权限系统提示词、模型/厂商启停允许无需重启；
   其他任何改动都必须重启。环境变量永远属于必须重启的启动配置。

## 标准完成条件

- Python 源码可完整编译；
- 测试全部通过且没有 skipped；
- 新厂商通过 Provider 契约测试和自己的 Golden Fixture；
- 未知 `provider_options` 被拒绝；
- 错误和 Usage 已脱敏；
- 没有新增未说明的顶层目录；
- 变更密钥时不在终端或最终报告中回显密钥值。

操作细节从 [ADD_DIY/README.md](ADD_DIY/README.md) 开始。
