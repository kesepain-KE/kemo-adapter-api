# Anthropic

此预制仅提供连接默认值，已做离线结构校验，尚未进行真实厂商调用验证。

- 模板 ID：`anthropic`
- 内置适配器：`anthropic-messages`
- Base URL：`https://api.anthropic.com`
- 鉴权：`api-key`，密钥在上游的凭据字段填写。
- 官方文档：[Anthropic](https://platform.claude.com/docs/en/api/messages)

将本目录内三个文件打包到 ZIP 根目录，在控制台安装为提供商模板后启用，再创建上游并填写密钥。`catalog: []` 不固定模型；点击“发现模型”读取目录，若平台未开放目录接口则手动登记模型。

使用原生 Messages 协议。运行时自动添加 anthropic-version: 2023-06-01；本适配器未开放结构化输出，当前运行时也不接受该适配器声明 reasoning.supported。

模板不声明模型能力；登记 Public Model 与 Deployment 时应依据适配器实际支持范围和真实测试。未配置 `reasoning_effort_map` 的模板不声明 `reasoning.supported`；只有真实厂商验证成功后才能标记 `verified`。

本地计量预选 `claude-legacy`；原厂响应用量始终优先。本地词表只作文本估算，具体资源版本与适用边界见 [计量器目录](../../../gateway/tokenizers/README.md)。
