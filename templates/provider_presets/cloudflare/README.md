# Cloudflare Workers AI

此预制仅提供连接默认值，已做离线结构校验，尚未进行真实厂商调用验证。

- 模板 ID：`cloudflare`
- 内置适配器：`cloudflare-workers-ai`
- Base URL：`https://api.cloudflare.com/client/v4/accounts/{account_id}/ai`
- 鉴权：`bearer`，密钥在上游的凭据字段填写。
- 官方文档：[Cloudflare Workers AI](https://developers.cloudflare.com/workers-ai/get-started/rest-api/)

将本目录内三个文件打包到 ZIP 根目录，在控制台安装为提供商模板后启用，再创建上游并填写密钥。`catalog: []` 不固定模型。当前适配器未提供模型发现接口，发现操作返回空目录；请手动登记实际模型 ID。

应用模板后必须替换 Base URL 中的 {account_id}。适配器追加 /run/<编码后的模型 ID>，因此 Base URL 必须以 /accounts/{account_id}/ai 结束，不加 /v1。本适配器当前仅开放文本非流式生成，不开放流式、工具和结构化输出。

模板不声明模型能力；登记 Public Model 与 Deployment 时应依据适配器实际支持范围和真实测试。未配置 `reasoning_effort_map` 的模板不声明 `reasoning.supported`；只有真实厂商验证成功后才能标记 `verified`。
