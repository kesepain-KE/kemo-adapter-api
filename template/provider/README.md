# Provider Package 模板

提供创建新 LLM 厂商 Provider 包的最小骨架。
参考实现见 `providers/deepseek/`（本地部署态，由 `create_provider` 生成）。

## 使用方式

```bash
# 复制本目录为 providers/<provider_id>/
cp -r template/provider/ providers/deepseek-v2/
```

然后去掉文件名中的 `.example` 后缀，实现所有 TODO 注释。

## 协议转换原理

一个 Provider 包的本质是**翻译器**，把厂商私有协议转成网关的统一事件：

```
厂商 HTTP API
    ↓  client.py（鉴权、HTTP 请求）
    ↓  protocol.py（请求参数翻译）
    ↓  streaming.py（SSE → ProviderEvent）
    ↓  usage.py（计费单位标准化）
    ↓  errors.py（错误码 → ErrorObject）
    ▼
ProviderEvent / ProviderResult  ← 网关认识的唯一语言
    ↓  api/assembler.py（加 SSE 信封、sequence）
    ▼
Kemo SSE 事件（输出给 kemo-agent）
```

每层只做一件事，不得越界。

## 目录职责

| 文件 | 只负责 |
| --- | --- |
| `__init__.py` | 暴露 `create_provider(settings)` 工厂函数 |
| `provider.py` | 对网关的 Facade，编排本目录所有模块 |
| `client.py` | 厂商鉴权、HTTP/SDK、超时和取消 |
| `protocol.py` | 厂商 DTO、请求映射和非流式响应映射 |
| `streaming.py` | 厂商流解析为 `ProviderEvent` |
| `usage.py` | 厂商计量语义转换为统一 `Usage` |
| `errors.py` | 厂商错误转换为统一 `ErrorObject` |
| `capabilities.py` | 真实模型能力和限制 |
| `manifest.json` | 静态模型目录 |
| `config.json` | 可热更新的 Endpoint、超时等 API 配置 |
| `secrets.json` | 可热更新的厂商密钥，不上传 Git |

## 实现路径

从三个方向依次填充：

### 1. 入站方向（Kemo → 厂商 API）

```python
# protocol.py
KemoRequest.input[]          → 厂商 messages[]
KemoRequest.tools[]          → 厂商 tools[]
KemoRequest.generation       → 厂商 max_tokens, temperature, ...
KemoRequest.reasoning        → 厂商 thinking/reasoning_effort 参数
```

### 2. 出站方向（厂商 API → ProviderResult/ProviderEvent）

```python
# protocol.py (非流式)
厂商 choices[].message       → ProviderResult.output[]
厂商 usage                   → Usage

# streaming.py (流式)
厂商 SSE delta               → ProviderEvent(TEXT_DELTA / REASONING...)
厂商 finish_reason           → ProviderEvent(COMPLETED / FAILED)
```

### 3. 错误映射

```python
# errors.py
HTTP 401 → AUTHENTICATION_ERROR
HTTP 429 → RATE_LIMITED (retryable=True)
HTTP 500 → PROVIDER_UNAVAILABLE (retryable=True)
```

## 强制边界

1. 不得从 `api` 导入任何代码。
2. 不得生成 SSE 字节、`event_id` 或 `sequence`。
3. 不得把厂商原始 Usage 交给核心猜测。
4. 不得返回未经脱敏的错误体、Headers、签名 URL 或 Provider State。
5. 所有 `provider_options` 必须在本包内按白名单解析，禁止透传任意 Header、URL 或密钥。

`config.json` 和 `secrets.json` 更新无需重启。任何 Python、manifest、依赖或协议
代码变化都必须重启。
