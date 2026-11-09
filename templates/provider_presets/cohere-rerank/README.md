# Cohere Rerank

此预制仅提供连接默认值，已做离线结构校验，尚未进行真实厂商调用验证。

- 模板 ID：`cohere-rerank`
- 内置适配器：`cohere-rerank`
- Base URL：`https://api.cohere.com/v2`
- 鉴权：`bearer`，密钥在上游的凭据字段填写。
- 官方文档：[Cohere Rerank](https://docs.cohere.com/v2/reference/rerank)

将本目录内三个文件打包到 ZIP 根目录，在控制台安装为提供商模板后启用，再创建上游并填写密钥。`catalog: []` 不固定模型。当前适配器未提供模型发现接口，发现操作返回空目录；请手动登记实际模型 ID。

适配器追加 /rerank，故 Base URL 使用官方 v2 版本前缀，并按官方接口使用 Bearer 鉴权，不沿用旧表的 api-key。只提供 rerank；计费 search units 不冒充 token。

模板不声明模型能力；登记 Public Model 与 Deployment 时应依据适配器实际支持范围和真实测试。未配置 `reasoning_effort_map` 的模板不声明 `reasoning.supported`；只有真实厂商验证成功后才能标记 `verified`。
