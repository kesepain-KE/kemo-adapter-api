# 0.7.6 稳定性补丁说明

这是 Kemo Provider Gateway 的小版本稳定性更新。协议版本仍为 `1.0`，不新增公开 API，不改变现有模型、密钥池或热更新配置格式。

## 主要变化

- Provider 工具参数在执行前必须解析为完整 JSON 对象，并通过请求工具 Schema 校验。
- 非法、缺失、空字符串、非对象或深层超限参数不会执行，统一以有限诊断和 `response.incomplete` 表示。
- 流式 `tool_call.completed` 事件会等统一终态通过校验后再发布。
- 并行工具调用按批次原子处理。同批任意调用非法时，不提前发布或执行其他调用。
- Schema 校验增加递归深度、节点总数和数组项数限制，避免异常输入造成递归崩溃或无界扫描。
- 保留既有多密钥故障转移、热配置失败回滚、公网鉴权和诊断脱敏规则。

## 兼容边界

- Kemo Protocol 仍为 `1.0`。
- Provider 包仍负责厂商协议转换，核心只负责统一合同、校验、事件和持久化。
- 已经产生有效流式输出的请求不会因为参数问题被盲目重放。
- 公开响应、日志和诊断不得包含 Provider 密钥、Bearer Token、Cookie 或原始敏感响应。

## 发布验证

```powershell
D:\Anaconda3\envs\kemo\python.exe -m pytest -q
D:\Anaconda3\envs\kemo\python.exe -m compileall -q update.py update core api providers web template tests
```

Windows 和 Linux CI 都会单独执行 Provider 工具参数、传输稳定性、Provider 边界和能力校验测试。
