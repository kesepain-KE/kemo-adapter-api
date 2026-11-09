# 智谱 GLM

此预制仅提供连接默认值，已做离线结构校验，尚未进行真实厂商调用验证。

- 模板 ID：`zhipu`
- 内置适配器：`openai-chat`
- Base URL：`https://open.bigmodel.cn/api/paas/v4`
- 鉴权：`bearer`，密钥在上游的凭据字段填写。
- 官方文档：[智谱 GLM](https://docs.bigmodel.cn/cn/guide/start/quick-start)

将本目录内三个文件打包到 ZIP 根目录，在控制台安装为提供商模板后启用，再创建上游并填写密钥。`catalog: []` 不固定模型；点击“发现模型”读取目录，若平台未开放目录接口则手动登记模型。

版本前缀是 /api/paas/v4。本模板未配置 reasoning_effort_map，不因模型内置思考而声明 reasoning.supported。

模板不声明模型能力；登记 Public Model 与 Deployment 时应依据适配器实际支持范围和真实测试。未配置 `reasoning_effort_map` 的模板不声明 `reasoning.supported`；只有真实厂商验证成功后才能标记 `verified`。

本地计量预选 `glm`；原厂响应用量始终优先。本地词表只作文本估算，具体资源版本与适用边界见 [计量器目录](../../../gateway/tokenizers/README.md)。
