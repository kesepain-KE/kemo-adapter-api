# 腾讯混元

此预制仅提供连接默认值，已做离线结构校验，尚未进行真实厂商调用验证。

- 模板 ID：`hunyuan`
- 内置适配器：`openai-chat`
- Base URL：`https://api.hunyuan.cloud.tencent.com/v1`
- 鉴权：`bearer`，密钥在上游的凭据字段填写。
- 官方文档：[腾讯混元](https://cloud.tencent.com/document/product/1729)

将本目录内三个文件打包到 ZIP 根目录，在控制台安装为提供商模板后启用，再创建上游并填写密钥。`catalog: []` 不固定模型；点击“发现模型”读取目录，若平台未开放目录接口则手动登记模型。

该端点主要面向存量接入；新账户应确认访问权限，并评估 TokenHub 模板。

模板不声明模型能力；登记 Public Model 与 Deployment 时应依据适配器实际支持范围和真实测试。未配置 `reasoning_effort_map` 的模板不声明 `reasoning.supported`；只有真实厂商验证成功后才能标记 `verified`。

本地计量预选 `hunyuan`；原厂响应用量始终优先。本地词表只作文本估算，具体资源版本与适用边界见 [计量器目录](../../../gateway/tokenizers/README.md)。
