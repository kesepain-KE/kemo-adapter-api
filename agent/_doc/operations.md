# 运行、诊断与维护

[返回操作入口](README.md) · 先完成 [连接与认证](connection.md)

## 运行策略与系统提示词

`PATCH /admin/api/settings` 使用目录 revision，支持：

| 字段 | 含义 |
| --- | --- |
| `enabled` | 是否接受模型业务请求；关闭后仍能通过管理面重新开启 |
| `system_prompt` | 网关最高优先级系统提示词，最长 100000 字符 |
| `max_attempts` | 每次执行最多尝试次数，1–10 |
| `cooldown_seconds` | 失败来源冷却时间，1–3600 秒 |

只提交要改变的字段，发布后影响后续请求，无需重启。此处全局 `enabled` 与提供商、模型、Deployment、调用密钥各自的 `enabled` 不同；按用户希望影响的范围操作。

## 读取调用记录和统计

| 任务 | 读取方式 |
| --- | --- |
| 查看调用摘要 | `GET /admin/api/logs`，分页用 `limit`（1–200）和 `offset` |
| 筛选问题 | `model`、带时区 ISO `start/end`、`task=llm\|embedding\|rerank`、`status=running\|completed\|failed\|cancelled`、`log_type=request\|model-test\|capability-probe` |
| 查看一条调用正文 | `GET /admin/api/logs/{request_id}/{attempt_no}` |
| 总览、用量、费用和排行 | `GET /admin/api/diagnostics/usage?period=today\|24h\|7d\|30d\|all` |
| 请求趋势 | `GET /admin/api/diagnostics/activity?period=week\|month\|26w&granularity=day\|hour&date=YYYY-MM-DD` |
| 当前启动输出 | `GET /admin/api/system/terminal-logs?stream=all\|out\|err&limit=300` |

上表的 `|` 表示可选值，实际请求只填一个值。日志按 Attempt 分别记录重试。统计排除模型测试和能力探测；`today` 以统计时区零点计算，其他有限用量窗口为滚动范围，`all` 为已保留的完整业务历史。未知价格、Token 和延迟保持未知，不把“—”说成免费或零用量。费用按币种区分，成功率分母为已结束 Attempt。

正文可能含用户输入和业务数据，只读取排查需要的记录；报告优先使用状态、错误码、对象 ID 与时间，不复制整段私密正文。启动输出是当前进程的近期只读内存视图，不是命令执行入口。

需要减少日志写盘时，在启动配置中设置 `LOG_DETAIL_MODE`：`full`（默认）保留全部调用详情，`errors` 仅保留失败或取消的详情，`off` 不收集上游原始交互、不保存正文。完整详情先留在内存，调用结束后只写一次；运行中的详情可按需读取。`LOG_DETAIL_MAX_BYTES` 限制单条详情大小。三种模式都保留调用摘要、用量、费用与配额结算，修改模式不会删除已有详情，保存后需要重启。

每日辅助统计默认以 250 毫秒或 128 次更新为一批写入，读取前与正常关闭前刷出；异常退出可能丢失尚未提交的一批。执行恢复、SSE 重放和计费记录仍按原路径持久化。检查辅助统计健康状态时，关注 `healthy`、`dropped_events` 和 `pending_events`，不能将入队视为已经写盘。

## 编辑启动配置

owner 读取 `GET /admin/api/system/startup-config`，返回可编辑的 `yaml`、文件 `expected_revision`、敏感项掩码和环境覆盖信息。它与 `GET /admin/api/system/gateway-config` 的当前已加载配置不同。

```python
import yaml

response = client.get("/admin/api/system/startup-config")
response.raise_for_status()
startup = response.json()
document = yaml.safe_load(startup["yaml"])
document["STATISTICS_TIMEZONE"] = "Asia/Shanghai"  # 使用用户指定的时区
response = client.put("/admin/api/system/startup-config", json={
    "expected_revision": startup["expected_revision"],
    "yaml": yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
})
response.raise_for_status()
assert response.json()["restart_required"] is True
```

`secrets` 仅在用户明确要替换认证项时提供，字段使用启动项名称，例如 `WEB_PASSWORD`。省略保留原值，空字符串清除；不能把掩码写回。未知已有字段会保留，不新增未知项。`KEMO_MASTER_KEY` 不通过此入口编辑。进程环境优先于 YAML，修改被环境覆盖的字段不会改变其实际运行值。

保存文件不会自动重启或改变已加载配置；核对返回的差异和覆盖信息后，按本次维护任务已有授权执行重启。

## 检查与执行重启

owner 使用 `GET /admin/api/system/restart-required` 检查待重启变化，`GET /admin/api/system/restart` 查看当前实例、在途请求与重启阶段。需要重启时：

```python
response = client.post("/admin/api/system/restart", json={
    "force": False,
    "reason": "应用用户要求的启动配置",
})
response.raise_for_status()
request_id = response.json()["request_id"]
```

接受结果为 202，表示已提交，不表示已经启动成功。查询重启状态直到该 `request_id` 的 `phase` 为 `succeeded`，并核对 `/healthz` 为 `running`、实例已更换；连接断开可能是进程替换过程，先查状态，不重复提交。`force` 默认为 false，排空在途请求；不要因短暂等待就强制终止。

网关已停止时 HTTP 无法启动它，需要在实际部署目录使用对应 Python 环境执行 `python start_web.py`。版本检查 `GET /admin/api/system/version-check` 会查询远程信息，只在用户需要检查更新时调用；不等于已安装更新。

## 安装和启停扩展

从快照区分 `protocol_adapters`（代码适配器）与 `provider_templates`（提供商模板）。使用用户选择的 ZIP 包，owner 发送 multipart：

```python
with open(package_path, "rb") as package:
    response = client.post(
        f"/admin/api/extensions/{kind}/install",
        data={"expected_revision": state["revision"]},
        files={"file": (package_path.name, package, "application/zip")},
    )
response.raise_for_status()
extension_id = response.json()["id"]
```

`package_path` 为 `pathlib.Path`，`kind` 为上述两者之一。安装结果保持停用；重新读取快照，然后 `PATCH /admin/api/extensions/{kind}/{extension_id}`，正文 `{expected_revision, value: {"enabled": true}}` 才启用。停用使用 false；被现有连接引用的扩展不能直接停用，先按用户意图处理引用。

代码适配器具有运行用户的权限，只安装用户选择并信任的代码。模板提供连接默认值，启用模板不会自动创建提供商或模型。

### Worker 诊断与并发

`GET /admin/api/extensions/protocol_adapters/{extension_id}/logs` 读取当前启用 worker 的近期脱敏日志；控制台“协议适配器 → Worker 日志”使用同一入口。缓冲最多 200 行，每行最多 8192 字符，放在内存中，worker 或网关重启后不保留。编程异常含 traceback；报告先保留类型、位置与统一错误码，不复制用户业务正文。

同一个 worker 可同时处理多个请求。取消只作用于指定请求，不关闭其他在途流；停用或替换配置时，旧一代继续服务已有请求，释放后才关闭对应 worker。参数错误、适配器编程错误不主动重试；上游超时或可重试错误沿用路由策略。上游可能已经执行后才返回网络错误，错误分类不承诺绝不重复计费。

编写适配器使用 [可运行的协议适配器示例](../../templates/protocol_adapter/README.md)。当前 worker 支持文本、Embedding 与 Rerank，不开放跨进程 Asset 访问，含图片、音频、视频、文件能力的扩展清单会被拒绝。

### 升级旧连接

2.0.0 协议层重构在启动读取目录前显式将 `auth_type` 转为 `auth_scheme`，一个事务完成并记录转换日志及审计。`bearer/none` 保持原值；旧 Anthropic `api-key` 转为 `anthropic-key`，旧 Gemini 转为 `gemini-query`；普通 Chat 的旧 `api-key` 继续表示 `x-api-key`，不会擅自改为 Azure 的 `api-key` 头。密文、凭据 ID、模型与路由原样保留；转换后重启不重复处理。

升级前以 SQLite 一致性备份保存 `data/`，同时保留对应源码、扩展、启动配置与主密钥。需要回退时先停止网关，再恢复同一批次的源码、数据库和主密钥；新格式数据库不能直接交给旧代码。恢复备份会回到备份时的数据，应先另存升级后的数据，再由用户决定是否恢复。不要删除运行库来规避字段转换。

## 导出、备份与恢复

`GET /admin/api/catalog.yaml` 导出脱敏目录，适合核对与归档，不含可恢复的密钥原文，不能作为完整备份。当前没有将此导出直接上传并恢复所有对象的管理命令。

备份需保存启动配置、`data/`、`extensions/` 及对应主密钥材料。运行中 SQLite 使用一致性备份方式，不直接复制一个可能仍有 WAL 写入的数据库文件。Windows 默认主密钥由当前用户 DPAPI 保护；迁移用户或系统需保持可恢复的主密钥。恢复前核对目标实例和备份范围，在停止写入后使用一致的数据库及主密钥恢复，验证目录、权限和历史，不尝试用脱敏导出还原密文。

## 每次交付核对

重新读取修改对象及关联项，确认实际生效方式。修改模型或调用权限后，使用客户端自己的调用身份读取模型目录和能力；需要真实生成测试时按用户授权执行。向用户说明修改结果和仍待生效的启动项，需要重启时遵循本手册的重启流程。
