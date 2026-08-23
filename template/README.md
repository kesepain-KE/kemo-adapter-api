# 模板目录

本目录存放 Kemo 网关可复制的创建模板。模板只描述核心契约和厂商边界，
厂商协议、计量、错误和流解析必须留在复制后的 Provider 目录内。

## 小模型最短清单

- 新厂商：复制 `template/provider/` 到 `providers/<provider_id>/`，不要覆盖已有目录。
- 新模型：不复制模板；同步 Provider 模型集合、`capabilities.py`、`manifest.json`、协议映射和测试。
- 改上游密钥：只改 Provider 目录的 `secrets.json`，使用 `api_keys` 数组，至少保留一项启用密钥。
- 改网关调用 Token：改 `api/keys.json`，不要和上游密钥混用。
- `config.json`/`secrets.json` 热更新；Python、manifest、依赖和新增目录需要重启。
- 能力没有真实测试证据就填 `false`；完成后运行契约测试并报告风险。

## 模板清单

| 目录 | 用途 | 参考实现 |
|------|------|---------|
| `provider/` | 创建新的厂商 Provider 包（含自有可达性探测器） | 本目录即权威骨架 |

## 使用方式

复制 `template/provider/` 到 `providers/<provider_id>/`，删除缓存，按需去掉 `.example` 后缀，
再根据厂商真实协议和脱敏 Fixture 完成实现。不要在 `providers/` 下维护第二份模板，也不要用
模板覆盖现有 Provider。完整流程见 `ADD_DIY/provider-package.md` 和
`ADD_DIY/verification.md`。

复制模板后不得仅做字符串替换。尤其要重新实现并验证厂商的媒体来源转换、错误正文解析、
任务端点和能力声明；“OpenAI-compatible”不等于自动支持图片、音频、工具、推理或流式。完整
多模态实现必须使用 `RequestContext.assets` 读取输入或登记输出，并逐项声明
`extensions.operations`，不能向公开响应泄露本地路径。

Provider 上游密钥必须显式保存在复制后目录的 `secrets.json` 中，标准格式固定为
`api_keys` 数组；不得转存到 `.env`、`PROVIDER_SETTINGS_JSON`、`config.json` 或源码。旧
`api_key` 字段只用于读取迁移兼容，不应出现在新建 Provider 的持久化文件中。密钥池可以
通过管理端追加或删除，但任何 Provider 至少必须保留一个上游密钥，不能删除到空池。

每个 LLM 模型还必须显式填写 `reasoning`。模板默认不支持推理；一旦确认支持，必须面向
kemo-agent 暴露 `minimal|low|medium|high|max` 五个逻辑档位并逐项映射。厂商档位较少或
只有开关时允许折叠映射，但必须在能力扩展中公开，不能把五个名称盲目原样透传。`xhigh`
不是模板默认的第六档；只有在 Provider 明确把兼容输入映射到厂商真实参数时才允许出现。

错误映射也要保守：普通 HTTP 403 是 `PERMISSION_DENIED`，不能触发密钥切换；只有厂商文档明确
证明 403 表示当前密钥失效时，才用 `from_http_status(403, key_failure=True)` 标记密钥级错误。
