# Google Gemini (AI Studio)

此预制仅提供连接默认值，已做离线结构校验，尚未进行真实厂商调用验证。

- 模板 ID：`gemini`
- 内置适配器：`gemini`
- Base URL：`https://generativelanguage.googleapis.com/v1beta`
- 鉴权：`api-key`，密钥在上游的凭据字段填写。
- 官方文档：[Google Gemini (AI Studio)](https://ai.google.dev/gemini-api/docs/quickstart)

将本目录内三个文件打包到 ZIP 根目录，在控制台安装为提供商模板后启用，再创建上游并填写密钥。`catalog: []` 不固定模型；点击“发现模型”读取目录，若平台未开放目录接口则手动登记模型。

使用原生 Gemini 协议，运行时通过 x-goog-api-key 发送密钥。图片须先转换为 data: URL；不能直接传远程图片 URL。

模板不声明模型能力；登记 Public Model 与 Deployment 时应依据适配器实际支持范围和真实测试。未配置 `reasoning_effort_map` 的模板不声明 `reasoning.supported`；只有真实厂商验证成功后才能标记 `verified`。

本地计量预选 `gemini`；原厂响应用量始终优先。本地词表只作文本估算，具体资源版本与适用边界见 [计量器目录](../../../gateway/tokenizers/README.md)。
