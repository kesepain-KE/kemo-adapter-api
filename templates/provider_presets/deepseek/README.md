# 深度求索

此预制提供连接默认值。2026-11-09 使用本模板的官方入口、Bearer、Profile 与 `deepseek-flash` 完成短文本非流式及流式调用，两次均得到完整文本与真实 usage，状态标记为 verified。验证仅覆盖该模型这两种基础请求，不代表所有模型或推理、工具、媒体能力都已验证。

- 模板 ID：`deepseek`
- 内置适配器：`openai-chat`
- Base URL：`https://api.deepseek.com/v1`
- 鉴权：`bearer`，密钥在上游的凭据字段填写。
- 官方文档：[深度求索](https://api-docs.deepseek.com/zh-cn/)

将本目录内三个文件打包到 ZIP 根目录，在控制台安装为提供商模板后启用，再创建上游并填写密钥。`catalog: []` 不固定模型；点击“发现模型”读取目录，若平台未开放目录接口则手动登记模型。

使用 /v1 Chat 兼容端点。Profile 配置 `thinking_mode: true`，显式推理开关转换为 `thinking.type: enabled / disabled`；推理档位按原厂提供的 `low / high / max` 映射为 `reasoning_effort`。未指定推理配置时沿用上游默认行为。

模板配置探测候选，不直接声明模型支持。网页登记时逐档检查推理 Token 或推理输出，并读取原厂目录的 `effort.supported_levels`。检查记录区分响应确认、原厂声明、拒绝和未知；参数仅被接受不会自动算作验证成功。已有上游的 Profile 不随模板更新覆盖，需要合并上述字段后重新探测或按原厂目录声明能力。

本地计量预选 `deepseek`；原厂响应用量始终优先。本地词表只作文本估算，具体资源版本与适用边界见 [计量器目录](../../../gateway/tokenizers/README.md)。
