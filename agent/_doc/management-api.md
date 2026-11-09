# 管理操作速查

新增适配器、模板、来源分类与版本替换的完整步骤见 [资源手册](resources.md)；图片模型见 [图片手册](images.md)。

| 资源管理 | 方法与路径 | 请求与结果 |
| --- | --- | --- |
| 查看来源和运行版本 | `GET /admin/api/console` | `adapters/templates[].source` 为 builtin/user；`resources.running` 为运行版本，`restart_required` 表示文件已更新但未重启 |
| 安装用户包 | `POST /admin/api/extensions/{kind}/install` | owner；multipart `expected_revision/file`；可选 `replace=true`，替换前需停用同 ID；kind 为 protocol_adapters/provider_templates |
| 启停用户资源 | `PATCH /admin/api/extensions/{kind}/{id}` | owner；`{expected_revision,value:{enabled}}`；被引用时停用需先处理引用 |
| 用户适配器日志 | `GET /admin/api/extensions/protocol_adapters/{id}/logs` | 当前 worker 的脱敏日志 |
| 检查内置资源更新 | `POST /admin/api/resources/check` | owner；`{expected_revision,value:{branch:"main"或"kemo-preview"}}` |
| 执行已检查更新 | `POST /admin/api/resources/apply` | owner；`{expected_revision,value:{target_commit,resource_revision}}`，必须与刚检查计划一致 |

上述写操作沿用 CSRF、同源和 revision 校验。资源更新不修改目录数据或框架版本，成功需重启；失败恢复原资源。资源更新冲突返回 409，不兼容返回 422。

[返回操作入口](README.md) · 先完成 [连接与认证](connection.md)

本页供第三方智能体查询管理请求路径、权限与字段语义。具体操作顺序见 [提供商与上游凭据](providers.md)、[模型与路由](models.md)、[下游密钥](keys.md) 和 [运行与维护](operations.md)。

由当前源码路由核对。管理接口不属于 Kemo 2.0 公开协议，统一前缀 `/admin/api`。普通读取/目录修改需要 `admin:web` 或 owner；密钥和扩展修改、重启、启动文件读取与编辑需要 owner。认证入口按登录阶段开放。Cookie 写请求要求同源与 CSRF。

目录及密钥写操作正文为 `{expected_revision, value}`，并发冲突返回 409。扩展安装使用 multipart 的 `expected_revision` 和 ZIP `file`。发现为显式只读上游目录请求。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/admin/api/console` | 读取脱敏控制台快照、目录和权限 |
| POST | `/admin/api/upstreams` | 创建上游并发布 |
| PATCH | `/admin/api/upstreams/{identity}` | 局部修改上游并发布 |
| DELETE | `/admin/api/upstreams/{identity}` | 删除未被路由引用的上游 |
| POST | `/admin/api/upstreams/{identity}/discover` | 调用模型目录或返回模板目录，不发起生成 |
| POST | `/admin/api/upstreams/{identity}/register` | 检测选中模型能力，登记公开模型与 Deployment |
| POST | `/admin/api/models` | 创建公开模型 |
| PATCH | `/admin/api/models/{identity:path}` | 修改模型声明/启停 |
| DELETE | `/admin/api/models/{identity:path}` | 删除公开模型、全部关联 Deployment 及密钥白名单引用，保留历史 |
| POST | `/admin/api/deployments` | 创建 Deployment |
| POST | `/admin/api/deployments/{identity}/test` | 对指定部署执行一次基础模型测试，返回结果预览、用量与耗时 |
| POST | `/admin/api/deployments/{identity}/probe` | 对指定部署重新探测能力，返回待应用的结果，不改模型目录 |
| PATCH | `/admin/api/deployments/{identity}` | 修改路由/限额/价格 |
| DELETE | `/admin/api/deployments/{identity}` | 删除 Deployment |
| PATCH | `/admin/api/settings` | 修改运行策略并发布 |
| POST | `/admin/api/keys` | 创建下游 Token，返回原文并加密保存 |
| PATCH | `/admin/api/keys/{identity}` | 修改/停用下游密钥策略 |
| POST | `/admin/api/keys/{identity}/reveal` | owner 按需复制原文，带 CSRF/revision，禁止缓存并审计 |
| DELETE | `/admin/api/keys/{identity}` | 删除调用密钥，拒绝新请求并保留历史 |
| GET | `/admin/api/logs` | 分页、日期时间、日志类型、模型类型、状态筛选 |
| GET | `/admin/api/logs/{request_id}/{attempt_no}` | 完整请求、返回及上游 JSON/SSE 正文 |
| POST | `/admin/api/extensions/{kind}/install` | 上传 ZIP，校验并安装为停用 |
| PATCH | `/admin/api/extensions/{kind}/{identity}` | 启停扩展，构建候选并发布 |
| GET | `/admin/api/extensions/protocol_adapters/{identity}/logs` | 读取当前 worker 近期脱敏内存日志 |
| GET | `/admin/api/catalog.yaml` | 导出脱敏 YAML 目录 |
| GET | `/admin/api/auth/methods` | 查询可用认证阶段 |
| POST | `/admin/api/auth/token` | 第一阶段 Web Token 验证 |
| POST | `/admin/api/auth/password` | 用户名/密码验证 |
| GET | `/admin/api/auth/session` | 读取会话及 CSRF Token |
| POST | `/admin/api/auth/logout` | web_auth_logout |
| GET | `/admin/api/system/gateway-config` | 查看进程启动配置 |
| GET | `/admin/api/system/startup-config` | owner 读取可编辑启动 YAML、文件 revision 和敏感项掩码 |
| PUT | `/admin/api/system/startup-config` | owner 校验并原子保存启动 YAML，重启后生效 |
| GET | `/admin/api/diagnostics/usage` | 按时间范围汇总业务调用与提供商、模型、密钥排行 |
| GET | `/admin/api/diagnostics/activity` | 按天/小时汇总请求趋势及历史时段 |
| GET | `/admin/api/system/terminal-logs` | 当前进程脱敏 OUT/ERR 近期只读快照 |
| GET | `/admin/api/statistics/daily` | 每日统计 |
| GET | `/admin/api/statistics/hourly` | 小时统计 |
| GET | `/admin/api/statistics/rankings` | 维度排行 |
| GET | `/admin/api/statistics/invocations` | 请求级历史分页 |
| GET | `/admin/api/statistics/series` | 时间序列统计 |
| GET | `/admin/api/system/restart` | 读取排空/重启状态 |
| GET | `/admin/api/system/restart-required` | 检查需重启的源码变化 |
| GET | `/admin/api/system/version-check` | 查询版本更新 |
| POST | `/admin/api/system/restart` | 排空后重启（owner） |

公开模型、Asset、响应和状态的调用合同见 [api.md](../../api.md)。`/admin` 提供 SPA，`/admin/assets/*` 为构建资源，`/healthz` 为健康检查。仅开启 API_DOCS_ENABLED 时提供 `/docs`、`/redoc`、`/openapi.json`。

连接使用 `auth_scheme`，取值从对应适配器的 `auth_schemes` 读取。`profile.extra_body/extra_query` 与 `proxy` 的规则见 [提供商手册](providers.md)。配置错误返回 `detail: {code, field, message, details?}`，不回显提交的凭据值；发现的鉴权或网络错误不会被模板参考掩盖。

`GET /diagnostics/usage` 接受 `period=today|24h|7d|30d|all`，返回 `summary`、`rankings.provider/model/key`、起止时间与统计时区。`today` 按统计时区当天零点起算，`24h` 滚动回溯 24 小时；`summary.upstream_count` 和 `deployment_count` 为所选时段实际调用涉及的上游与路由去重数量。直接聚合持久化 Attempt，包含历史未标日志类型的记录，排除测试和探测；重试分别计数，运行中记录不进入成功率分母。缓存率按输入 Token 加权，只使用输入与缓存用量都已知的记录；未知用量与首字延迟保持 null，费用按币种分开，`unknown_cost_calls` 标记未知价格调用。历史对象使用调用时保存的名称。

`GET /system/terminal-logs` 接受 `limit`（1–1000，默认 300）、`stream=all|out|err`，返回采集状态、保留行数、OUT/ERR 数量和按时间顺序的行。`start_web.py` 采集当前进程一小时内最多 1000 行；重启后不续接历史文件。仅管理身份读取，所有响应禁止缓存。

`PUT /system/startup-config` 正文为 `{expected_revision, yaml, secrets?}`；与目录发布的 `{value}` 格式不同。`expected_revision` 取同路径 GET 的原始文件 SHA-256。`yaml` 只允许已知非敏感启动项，`secrets` 只允许显式替换 Web 用户名、密码、Web Token、Status Token 与启动引导密钥；空字符串清除，省略保留。原有未知字段和未修改敏感项保留，`KEMO_MASTER_KEY` 不可编辑。错误返回 422，并发变化返回 409，文件/校验进程故障返回 503；成功返回更新后的脱敏视图和 `restart_required: true`，不改变当前进程。owner、同源与 CSRF 边界沿用系统写接口。

`GET /logs` 参数：`limit` 1–200、`offset`、公开 `model`、带时区的 ISO `start/end`、`task`（llm/embedding/rerank）、`log_type`（request/model-test/capability-probe）、`status`（running/completed/failed/cancelled）。时间按 UTC 比较，界面将浏览器当地日期时间转换成带时区值。旧记录未采集的字段保留 null。列表不携带完整正文；详情按需加载，包含该次 Attempt 的完整适配器结果和原始上游正文，避免把重试后的最终响应误作之前失败尝试的响应。正文过期或历史未采集时返回 `detail: null`，此时可复用执行存储中仍保留的 Kemo 返回。

上游 `keys` 每项接受 `id/value/enabled/priority`，priority 范围 -1000 至 1000。`console.credential_health` 按上游 ID、凭据 ID 返回状态、最近检查时间、HTTP 状态及剩余冷却秒数；该运行状态不参与配置 revision。上游 `tokenizer` 从快照的 `tokenizers` 列表中选择，只影响日志的本地估算。

登记正文的 `value.models` 每项包含 `upstream_model`、`public_model`，可选 `task`；默认使用适配器的首个任务。省略 `capabilities` 时只对该选中模型执行付费能力检测，返回 `{revision, detection}`；`detection` 包含逐模型的 `verification` 和逐能力 `checks`，未确认项保留 `unknown`。保留显式传入 `capabilities` 的人工声明用法，不自动探测。过期 revision 和重复登记在上游请求前拒绝；最终发布仍检查 revision。模型能力 PATCH 成功后验证状态为 `declared`，真实探测结果不能通过人工修改伪造。

部署测试和探测均接受 `{expected_revision, value: {}}`，使用该版本已保存的连接、密钥和上游模型名。部署与上游必须启用；过期 revision、未知部署和额外字段在上游请求前拒绝。测试返回 `status`、`reason`、`latency_ms`，成功时附 `usage` 和文本 `preview`、向量 `dimensions` 或排序 `result_count`。探测返回 `verification`、逐项 `checks` 和建议 `capabilities`。两者均返回目标 `deployment_id`、公开 `model` 和实际使用的 `revision`，持有运行代际租约并记录 Attempt；不会自动发布或修改模型验证状态。控制台仅允许单部署模型把结果应用到编辑草稿，再经普通模型 PATCH 保存；多部署模型需逐个确认共同能力。

`POST /admin/api/deployments/{identity}/test?first_token=true` 为模型列表提供首字测试，正文不变。LLM 且 Adapter 实现流式时仅发送一次流式请求，新增 `streaming: true` 与 `first_token_ms`，计时到首个非空文本片段，继续消费至终态以验证成功并计量。非流式或检索任务保持单次基础请求，返回 `streaming: false`、`first_token_ms: null` 和真实响应耗时。尚未收到文本的失败请求也返回 `first_token_ms: null`。省略参数时保持编辑面板的单次基础测试行为。

LLM 探测按适配器范围使用内置音频、视频和 PDF 样本，`checks.audio/video/file` 在实际请求后包含 `sample_id`。正确回答为 `supported / response-confirmed`，答案不匹配为 `unknown / response-not-confirmed`；基础测试失败时不继续媒体请求，返回 `unknown / basic-test-failed`；适配器未实现为 `unsupported / adapter-not-implemented`，worker 无 Asset 访问为 `unknown / asset-access-unavailable`。响应不包含样本答案或上游错误原文；样本配置见 `gateway/probe_samples/manifest.yaml`。
