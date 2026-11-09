# OpenRouter

此预制仅提供连接默认值，已做离线结构校验，尚未进行真实厂商调用验证。

- 模板 ID：`openrouter`
- 内置适配器：`openai-chat`
- Base URL：`https://openrouter.ai/api/v1`
- 鉴权：`bearer`，密钥在上游的凭据字段填写。
- 官方文档：[OpenRouter](https://openrouter.ai/docs/api_reference/overview)

将本目录内三个文件打包到 ZIP 根目录，在控制台安装为提供商模板后启用，再创建上游并填写密钥。`catalog: []` 不固定模型；点击“发现模型”读取目录，若平台未开放目录接口则手动登记模型。

可在创建上游后填写 HTTP-Referer / X-Title；模板不填虚构站点，也不默认配置 reasoning_effort_map，具体参数以实际路由模型为准。

模板不声明模型能力；登记 Public Model 与 Deployment 时应依据适配器实际支持范围和真实测试。未配置 `reasoning_effort_map` 的模板不声明 `reasoning.supported`；只有真实厂商验证成功后才能标记 `verified`。
