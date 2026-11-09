# Command Code Provider

此预制提供连接默认值。2026-11-09 使用本模板的入口、Bearer、Profile 与 `deepseek/deepseek-v4.1-flash` 完成短文本非流式及流式调用，两次均得到完整文本与真实 usage，状态标记为 verified。验证不覆盖其他模型、推理档位或工具能力。分类为编码工具服务；现有使用 `x-api-key` 的连接保持原设置。

- 模板 ID：`command-code`
- 内置适配器：`openai-chat`
- Base URL：`https://api.commandcode.ai/provider/v1`
- 鉴权：`bearer`，密钥在上游的凭据字段填写。
- 官方文档：[Command Code Provider](https://commandcode.ai/provider)

将本目录内三个文件打包到 ZIP 根目录，在控制台安装为提供商模板后启用，再创建上游并填写密钥。`catalog: []` 不固定模型；点击“发现模型”读取目录，若平台未开放目录接口则手动登记模型。

需开通 Provider 访问权限；具体套餐和额度以账户控制台为准。

Profile 配置 `reasoning_effort_map`，以 `low / medium / high / xhigh / max` 为候选发送 `reasoning_effort`。候选值用于探测，不代表每个模型都支持；只有响应包含推理 Token 或推理输出才记录响应确认，拒绝与未知档位不会自动开放。已有上游不随模板更新覆盖，需合并 Profile 后重新探测；保留原有密钥和其他连接字段。
