# 模板目录

本目录存放 Kemo 网关可复制的创建模板。模板只描述核心契约和厂商边界，
厂商协议、计量、错误和流解析必须留在复制后的 Provider 目录内。

第一次操作从 [任务导航](../ADD_DIY/tasks.md) 开始，不要直接将本目录复制到运行中环境。
默认模板只公开文本非流式能力；流式解析示例仍可离线测试，但不是目标上游支持的证据。
额度未知时使用空对象，不提供可被误认为真实限制的示例数字。

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
| `examples/` | 任务卡、九种操作配对声明/请求、单模型增量修改、模拟推理映射 | 仅用于教学和离线校验，不是第二个 Provider 模板 |

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

每个 LLM 模型必须显式填写 `reasoning`。默认不支持；确认支持后优先完成
`minimal|low|medium|high|max` 五档映射，允许经验证的折叠。不强制虚构档位：
只验证部分时只公开对应集合，只有开关且无强度映射时使用 `supported=true, efforts=[]`。
`xhigh` 不是模板第六档。完整口径以 [模型维护](../ADD_DIY/model-maintenance.md) 为准。

`provider/catalog_contract.py` 是可随包复制的离线一致性检查，`test_contract.py` 会调用它。
模板测试命令见 [examples/README.md](examples/README.md)；检测通过不能替代目标厂商的真实测试。

源码版发布时按 [发布配方](../ADD_DIY/release.md) 同步版本、说明与测试；
Provider 私有模型清单不必跟随网关应用版本修改，更不能因此开启未验证能力。
密钥显示复用网关后端的前五后三掩码；模板或 Client 使用的仍是本地真实凭据，不能把 UI 掩码写回 secrets.json。

错误映射也要保守：普通 HTTP 403 是 `PERMISSION_DENIED`，不能触发密钥切换；只有厂商文档明确
证明 403 表示当前密钥失效时，才用 `from_http_status(403, key_failure=True)` 标记密钥级错误。
