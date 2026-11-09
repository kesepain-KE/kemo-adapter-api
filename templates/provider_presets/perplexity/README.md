# Perplexity

此预制仅提供连接默认值，已做离线结构校验，尚未进行真实厂商调用验证。

- 模板 ID：`perplexity`
- 内置适配器：`perplexity-agent`
- Base URL：`https://api.perplexity.ai`
- 鉴权：`bearer`，密钥在上游的凭据字段填写。
- 官方文档：[Perplexity](https://docs.perplexity.ai/api-reference/gateway-chat-completions-post)

将本目录内三个文件打包到 ZIP 根目录，在控制台安装为提供商模板后启用，再创建上游并填写密钥。`catalog: []` 不固定模型。当前适配器未提供模型发现接口，发现操作返回空目录；请手动登记实际模型 ID。

使用 perplexity-agent 已实现的 Sonar /chat/completions 路径，不使用 Agent / Responses 端点。citation_tokens 的计费口径与实际账号是否继续支持此兼容路径均须用真实响应确认。

模板不声明模型能力；登记 Public Model 与 Deployment 时应依据适配器实际支持范围和真实测试。未配置 `reasoning_effort_map` 的模板不声明 `reasoning.supported`；只有真实厂商验证成功后才能标记 `verified`。
