# 网关架构

## 依赖方向

```text
Kemo Agent
    │ Kemo HTTP / SSE
    ▼
api ──► core.executor ──► core.ProviderPackage contract
                                ▲
                                │ 仅统一对象
                    providers/<provider_id>/provider.py
                       │       │       │       │
                    client  protocol  usage  errors/streaming
                       │
                       ▼
                    厂商 API
```

Provider 包只能依赖 `core.models` 和 `core.provider_contract`，核心不得反向导入某个具体厂商。

## 热更新边界

只有以下四类运行时控制允许不重启：

| 内容 | 文件 | 生效范围 |
| --- | --- | --- |
| 网关 API 配置与调用密钥 | `api/runtime.json`、`api/keys.json` | 后续新请求 |
| 厂商 API Endpoint、密钥、超时等 | `providers/<id>/config.json`、`secrets.json` | 后续新请求 |
| 网关最高权限系统提示词 | `core/live_control.json` | 后续新请求 |
| 禁用/启用 Provider 和模型 | `core/live_control.json` | 后续新请求 |

运行时配置按内容生成 revision。系统只接受完整有效的 UTF-8 JSON；写入损坏或 Schema 无效时
拒绝该版本并继续使用最后一个有效快照。配置文件应使用“写临时文件后原子替换”的方式更新。

Provider API 配置切换必须采用新 Client 接收新请求、旧 Client 排空在途请求的方式，不能在
轮换 Key 或 Endpoint 时中断正在进行的流。

Provider 代码、协议、模型映射代码、Usage 算法、核心/API/Web 源码、依赖和环境变量等其他
变更全部需要重启。新增 Provider 目录也需要重启后才会被发现。

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
  → core ExecutionStore（生产需持久化实现）
  → SSE 客户端或断线重放
```

厂商包负责工具参数跨 chunk 拼接、厂商 Usage 去重和厂商终态识别；核心负责公共 SSE 信封、
严格 sequence、稳定 event_id、唯一终态和持久化恢复。

## 生产状态

生产实现需要分别持久化：

- Execution：幂等键、内部状态、Provider response id 和最终响应；
- Event log：按 response_id 原子追加的事件；
- Asset：授权主体、元数据、校验和、TTL 和 Blob 引用。

当前 `InMemoryExecutionStore` 仅供开发与契约测试，不能用于多进程或重启恢复。

## 重启状态机

`restart.py` 只协调由 `start_web.py` 启动的单实例网关。默认流程为：启动前检查、进入 Drain、
等待活动执行归零、停止旧实例、同端口启动新实例、验证 `/healthz`。Drain 期间只拒绝新的
`POST /model/responses`；已有 Response 的查询和取消、管理端以及健康检查仍可用。

重启请求和状态保存在被 Git 忽略的 `core/runtime/`，只记录 PID、实例 ID、阶段和脱敏原因，
不得记录环境变量值或密钥。`POST /admin/api/system/restart` 只允许 `owner` scope；普通
`admin:web` 只能管理运行时配置，不能重启进程。当前内存执行存储无法跨进程恢复，因此默认
Drain 超时会撤销重启；`force=true` 或 CLI `--force` 可能中断仍在执行的请求，必须显式使用。
