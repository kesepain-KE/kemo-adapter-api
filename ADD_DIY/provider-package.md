# 创建或修改 Provider Package

## 创建流程

### 强制前置验证（写任何代码前必须完成）

1. **获取用户提供的 API 信息** — API Key、base_url、厂商名称；
2. **读取操作手册索引** — 读取 `agent_control.md` 确认入口文档；
3. **辨别用户需求** — 是创建新厂商、修改现有厂商、还是添加模型；
4. **读取对应操作手册** — 创建新厂商读本文件（`ADD_DIY/provider-package.md`）；
5. **检查厂商是否可访问** — 用真实 API Key 请求 `GET /v1/models` 或厂商健康端点；
6. **获得厂商模型列表** — 从步骤 5 的响应中提取所有可用的上游模型名；
7. **对所有模型进行能力范围测试** — 对每个模型发一次最小对话请求，确认支持的特性（流式、工具、思考模式、多模态、JSON Output 等）；
8. **向用户报告检测到的模型配置** — 展示模型名、能力、定价，等待用户确认；
9. **经过用户允许后创建厂商目录** — 复制模板并实现代码；
10. **检查创建是否成功** — 验证目录完整性、语法、基本导入。

### 实现步骤

1. 将 `template/provider/` 复制为 `providers/<provider_id>/`；
2. `provider_id` 使用小写字母、数字和下划线，并保持长期稳定；
3. 将模板类名和示例模型替换为步骤 7 检测到的真实厂商信息；
4. 按职责实现 `client.py`、`protocol.py`、`streaming.py`、`usage.py`、`errors.py` 和
   `capabilities.py`；
5. 在包的 `__init__.py` 中只暴露 `create_provider(settings)`；
6. 增加厂商 Golden Fixture 和公共 Provider 契约测试；
7. 重启网关加载新代码。新增 Provider、协议、Usage、模型能力、任何 Python 代码的变化都必须重启。

## 无需重启的厂商 API 配置

- `config.json`：Endpoint、超时、代理、非敏感默认 Header 等；
- `secrets.json`：API Key 等敏感配置，必须被 Git 忽略；
- 配置变化只影响新请求，在途请求继续使用旧 Client 直到完成；
- 修改 `provider.py`、`protocol.py`、`usage.py` 等任何 Python 文件仍然必须重启。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `provider.py` | 对网关的 Facade，编排本包模块 |
| `client.py` | 鉴权、HTTP/SDK、超时、连接池、查询和取消 |
| `protocol.py` | 厂商 DTO、请求映射、非流式响应映射 |
| `streaming.py` | 厂商流到无信封 `ProviderEvent` |
| `usage.py` | 厂商 Token、缓存、推理和媒体计量语义 |
| `errors.py` | HTTP/SDK 错误、重试和脱敏 |
| `capabilities.py` | 模型、操作和参数限制 |
| `manifest.json` | 非敏感静态模型目录 |
| `config.json` | 无需重启的厂商 API 非敏感配置 |
| `secrets.json` | 无需重启的厂商 API 密钥，不上传 Git |

## 禁止事项

- 不得从 `api` 导入代码；
- 不得在核心添加厂商专用字段或厂商名称分支；
- 不得透传任意 Header、Endpoint 或 API Key；
- 不得用 0 表示厂商没有返回的 Usage；
- 不得自行执行 kemo-agent 工具；
- 不得把厂商原始错误体和密钥写入统一错误。

Provider Package 的权威接口是 `core/provider_contract.py`，可复制模板但不得复制并修改这份契约。
