# 网关架构

## 依赖方向

```text
Kemo Agent
    │ Kemo HTTP / SSE
    ▼
api ──► core.executor ──► core.provider_contract.ProviderPackage
                                ▲
                                │ 仅统一对象
                    providers/<provider_id>/provider.py
                       │       │       │       │
                    client  protocol  usage  errors/streaming/probe
                       │
                       ▼
                    厂商 API
```

Provider 包只能依赖 `core.models` 和 `core.provider_contract`，核心不得反向导入某个具体厂商。
管理端模型测试同样遵循此边界：核心调用 `package.probe()`，真实测试协议保留在厂商目录。

## 热插拔清单（无需重启）

以下配置变化通过 `LiveConfigManager` 在每轮 HTTP 请求中自动检测文件指纹（mtime + size），
即时生效，不中断在途请求。修改可通过编辑 JSON 文件或通过 Web 管理端 REST API 触发。

| 能力 | 配置文件 | 代码验证位置 | 生效时机 | 修改方式 |
|------|----------|-------------|----------|----------|
| 对外 API 启停 | `api/runtime.json` → `gateway_api.enabled` | `middleware.py:_resolve_principal()` 检查，`service.py:update_gateway()` 写入 | 下一请求 | 编辑 JSON 或 `PUT /admin/api/runtime/gateway` |
| API 密钥增删改 | `api/keys.json` → `keys` 对象 | `live_config.py:_load()` 解析，`middleware.py` 逐 Token HMAC 匹配 | 下一请求 | 编辑 JSON 或 Secret Manager 原子替换 |
| 禁用/启用 Provider | `core/live_control.json` → `disabled_providers` | `registry.py:resolve()` 检查黑名单 | 下一请求 | 编辑 JSON 或 `PUT /admin/api/runtime/control` |
| 禁用/启用模型 | `core/live_control.json` → `disabled_models` | `registry.py:resolve()` 检查黑名单 | 下一请求 | 同上 |
| 最高权限系统提示词 | `core/live_control.json` → `highest_priority_system_prompt` | `executor.py:make_context()` 注入到 RequestContext | 下一请求 | 编辑 JSON 或 `PUT /admin/api/runtime/control` |
| 厂商 API 地址/超时 | `providers/<id>/config.json` | `live_config.py:_load()` 读取并深合并，`package.reload_config()` 原子替换 Client | 下一请求 | 编辑 JSON 或 `PUT /admin/api/runtime/providers/{provider_id}`；已有密钥池不提交 api_key |
| 厂商密钥池 | `providers/<id>/secrets.json` → `api_keys` | 统一密钥路由与热配置 | 下一请求 | 按密钥配方原子修改 JSON，或使用网页 Provider 密钥池 |

### 热插拔实现细节

- **运行时配置按 revision 控制**：文件指纹（mtime + size）跳过无变化重载。写入损坏或 Schema 无效时拒绝该版本并继续使用最后一个有效快照（`live_config.py:_load()` 异常保护）。
- **配置文件应使用原子写入**：先写临时文件再 `os.replace()`，避免部分写入（`service.py:_atomic_json()`）。
- **Provider API 配置热更新**必须采用新 Client 接收新请求、旧 Client 排空在途请求的方式（`template/provider/provider.py:reload_config()`），不能在轮换 Key 或 Endpoint 时中断进行中的流。
- **内建对已创建执行的无损保护**：`registry.resolve_registered()` 绕过禁用检查，已在运行中的 LLM 响应和检索请求不受 `disabled_providers`/`disabled_models` 影响。正在使用旧 API Key 的 Provider 请求仍由旧 Client 完成。

### 生效条件

以上所有热插拔仅影响新请求。已创建的执行记录（包括运行中的 LLM 流、Embedding/Rerank 任务）继续使用变更前的配置直到完成。

## ❌ 需要重启的变更

| 类别 | 原因 | 代码位置 |
|------|------|----------|
| 新增 Provider 厂商包 | `providers/` 只在启动时 `pkgutil.iter_modules` 扫描一次 | `registry.py:discover()` |
| 新增模型注册 | 模型在 `discover()` → `register()` 中注册，启动后不再调用 | `registry.py:register()` |
| Provider Python 代码 | protocol/streaming/usage/errors/capabilities 等包内文件 | `providers/<id>/` |
| 核心/API/Web 源码 | core/、api/、web/ 目录代码 | — |
| 统计存储与缓存源码 | storage/ 的 Python 模块 | `statistics.py`、`read_cache.py` |
| 环境变量（.env） | `Settings.from_env()` 只启动时调用一次 | `config.py` |
| 依赖 | `requirements.txt` 变更 | — |
| 协议版本 | `X-Kemo-Protocol-Version` 硬校验为 `"1.0"` | `routes/responses.py`、`routes/retrieval.py` |

**环境变量永远属于必须重启的启动配置**，不得把环境变量误报为已热加载。

`providers/*` 默认被 Git 忽略，是部署端加载区；复制新 Provider 后必须在完成报告中明确它是否
会随仓库发布。除非用户授权，不得为了推送某个厂商而扩大 `.gitignore` 的跟踪范围。

## Provider 探测边界

```text
POST /admin/api/models/{model}/probe
  → 管理鉴权 / Registry / Drain / 活动执行计数
  → ProviderPackage.probe(model, context)
  → providers/<id>/probe.py 的真实低成本调用
  → ProviderProbeResult
  → 统一可达性响应
```

探测不注入业务最高权限系统提示词，也不进入普通业务调用统计。未实现探测器必须明确返回
`PROBE_UNSUPPORTED`；核心不得把未知任务默认当成 LLM、Embedding 或 Rerank。

## Usage 边界

不同厂商的 cached input、reasoning output、流式累计值、图片单位和音视频时长可能具有完全不同
的语义。因此 `providers/<provider_id>/usage.py` 必须先完成：

1. 原始字段含义解释；
2. 累计值与增量值去重；
3. 厂商权威 total 选择；
4. Token 与媒体单位标准化；
5. `exact_fields`、`estimated_fields` 和 `measurement.mode` 标注；
6. `provider_raw` 白名单、脱敏和大小限制。

`core/usage.py` 只聚合标准化 StageUsage，不得重新解释厂商字段。成本统计应使用未来独立的
Billing 对象，不得塞入 Token 字段。

## 流式边界

```text
厂商原始事件
  → provider streaming.py
  → ProviderEvent（无 sequence/event_id）
  → core EventAssembler
  → core SQLiteExecutionStore（事务写入执行和事件）
  → SSE 客户端或断线重放
```

厂商包负责工具参数跨 chunk 拼接、厂商 Usage 去重和厂商终态识别；核心负责公共 SSE 信封、
严格 sequence、稳定 event_id、唯一终态和持久化恢复。

## 生产状态

生产实现需要分别持久化：

- Execution：幂等键、内部状态、Provider response id 和最终响应；
- Event log：按 response_id 原子追加的事件；
- Asset：授权主体、元数据、校验和、TTL 和 Blob 引用。

当前 `api/server.py` 使用 `SQLiteExecutionStore` 保存执行与事件；`InMemoryExecutionStore`
仅供开发与契约测试，不能用于多进程或重启恢复。维护 Provider 时不要替换执行存储，也不要删除运行数据库。

## 统计读缓存不是模型缓存

`storage/statistics.py` 负责调用开始、终态、用量和统计查询；`storage/read_cache.py` 只缓存查询结果。
默认 128 项、8 MiB JSON 载荷、固定 30 秒 TTL，读命中不续期，统计写入前后清空缓存。
同一进程的相同并发查询复用结果，返回独立对象，写入取消时等待工作线程结束再释放锁。
多进程直接改库的可见性受 TTL 限制；缓存不保证跨进程同步，不用于鉴权或保存待落盘事件。
这与 `usage.cached_input_tokens` 和界面中的 Token 缓存率是两件事，不能互相计入统计。
详细限制与验证入口见 [统计存储](../storage/README.md)。

## 测试依赖方向

`python -m tests` → `tests/runner.py` → `tests/suites.py` 声明的功能测试组。
共享构造器位于 `tests/support/`，不得从另一个 `test_*.py` 导入。
本地未发布厂商和临时项目真实重启通过显式套件单独执行、单独报告；不要以默认通过代表全部可选项通过。

## 重启状态机

`restart.py` 只协调由 `start_web.py` 启动的单实例网关。默认流程为：预检新配置、进入 Drain、
等待活动执行归零、停止旧实例、按新 `.env` 的 HOST/PORT 启动实例、验证 `/healthz`。
端口变更后必须在新地址验证，不能只检查旧端口。Drain 期间拒绝新的模型执行（LLM、Embedding、Rerank），
已有 Response 的查询和取消、管理端以及健康检查仍可用。新实例失败时尽力恢复旧环境，不保证所有外部故障都可恢复。

重启请求和状态保存在被 Git 忽略的 `core/runtime/`，只记录 PID、实例 ID、阶段和脱敏原因，
不得记录环境变量值或密钥。`POST /admin/api/system/restart` 只允许 `owner` scope；普通
`admin:web` 只能管理运行时配置，不能重启进程。持久化记录不意味着正在进行的厂商网络连接
能跨进程继续，因此默认 Drain 超时会撤销重启；强制重启可能中断请求，必须明确授权，
不能为了让新模型马上出现就跳过 Drain。
