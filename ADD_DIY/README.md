# ADD_DIY 智能体操作入口

本目录是自动化智能体创建 Provider、修改协议、轮换密钥和验证发布结果的权威操作手册。
开始任何写操作前，先读根目录 `agent_control.md`，再按下表读取对应文件；不得仅凭旧对话或
通用 OpenAI 兼容经验修改网关。

## 任务路由

| 用户目标 | 必读文件 | 允许修改的主要范围 |
| --- | --- | --- |
| 创建厂商或增加模型 | `provider-package.md`、`verification.md` | `providers/<provider_id>/` |
| 修改厂商请求、响应、流或工具调用 | `provider-package.md`、`architecture.md`、`verification.md` | 目标 Provider 包 |
| 修改 Token、缓存、推理或媒体计量 | `provider-package.md`、`architecture.md` | 目标 Provider 的 `usage.py` |
| 修改厂商 Base URL、Header 或密钥 | `keys-and-secrets.md`、`architecture.md` | 目标 Provider 的配置文件 |
| 修改网关调用密钥或模型白名单 | `keys-and-secrets.md` | `api/keys.json` |
| 修改最高系统提示词或禁用策略 | `architecture.md` | `core/live_control.json` |
| 修改 `.env` | `keys-and-secrets.md`、`architecture.md` | `.env`，修改后必须重启 |
| 修改核心或公开协议 | `architecture.md`、`verification.md`、`api.md` | `core/`、`api/`、协议文档 |

## 固定执行顺序

1. 确认目标环境、用户授权范围和是否允许产生厂商费用；
2. 读取当前源码契约，不能用文档代替源码，也不能用模板覆盖用户已有实现；
3. 对现有 Provider 做增量修改；新 Provider 才复制 `template/provider/`；
4. 厂商差异全部留在自己的目录，核心不得出现厂商名称分支；
5. 先使用脱敏 Fixture 和契约测试，再在获得授权后执行最小真实调用；
6. 按 `verification.md` 完成检查；任何失败都不能报告“已完成”；
7. 明确告诉用户哪些变化热更新、哪些变化必须重启。

## 当前核心边界

- 当前公开任务正式支持 `llm`、`embedding`、`rerank`。`POST /model/responses` 的严格 Kemo
  合同已承载对话、视觉、图片生成/编辑、ASR、TTS、语音转换、视频理解和视频生成；实时双向
  会话以及合同外的新操作仍需先扩展核心。
- Kemo 媒体输入统一使用 image/audio/video/file Content Block；小对象可使用受控
  `source.kind`，大型对象必须先上传 `/assets` 并传 `asset_id`。Provider 通过
  `RequestContext.assets` 读取输入或登记输出，不能猜字段、静默丢弃、发送空媒体或公开本地路径。
- 模型可达性由每个厂商包的 `probe.py` 实现。核心不会替未知任务猜测测试协议。
- 完整网关模型名固定为 `<provider_id>-<厂商原始模型名>`；斜杠格式已废弃。
- `providers/*` 默认被 Git 忽略，属于部署端热加载内容。需要随仓库发布某个 Provider 时，
  必须先获得用户明确同意，再单独调整 `.gitignore`；不得悄悄改变发布范围。

## 绝对禁止

- 不得把真实 Token、厂商密钥、Authorization Header、原始错误体或签名 URL 写入输出、日志、
  Fixture、Markdown 或 Git；
- 不得猜测厂商能力、Token 规则、价格、模型列表或错误语义；未知字段保持未知；
- 不得自行调用付费接口、创建云端资源、撤销密钥或扩大 scopes，除非用户明确授权；
- 不得把 Provider 的 SSE 字节、sequence、event_id 或 HTTP Response 交给核心；
- 不得在测试通过前删除旧 Provider、旧密钥或用户已有配置。

发布前完成标准见 [verification.md](verification.md)。
