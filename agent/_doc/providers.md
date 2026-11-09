# 提供商与上游凭据

[返回操作入口](README.md) · 先完成 [连接与认证](connection.md)

提供商连接是 `upstreams` 中的一个对象。操作前确定名称、Base URL、协议、鉴权方式与用户提供的凭据；创建后用返回的 ID 操作。相同厂商可以有多个不同账号或地址的连接。

## 添加提供商

优先从 `state["templates"]` 选择已启用、有效的模板，读取其 `template` 默认值，核对 `adapter_id` 和地址。模板列表由实际快照给出，不根据厂商名拼模板 ID。

```python
import os

response = client.post("/admin/api/upstreams", json={
    "expected_revision": state["revision"],
    "value": {
        "name": "我的提供商",
        "template_id": template_id,
        "keys": [{"id": "primary", "value": os.environ["PROVIDER_API_KEY"]}],
    },
})
response.raise_for_status()
upstream_id = response.json()["id"]
```

`template_id` 使用用户选定模板的真实 ID。未提供 `id` 时服务端生成；可用固定 ID 标识用户明确要求的新连接，重复 ID 返回 409。

自定义连接不用模板，`value` 例如：

```json
{
  "id": "my-provider",
  "name": "我的提供商",
  "adapter_id": "openai-chat",
  "base_url": "https://provider.example/v1",
  "auth_scheme": "bearer",
  "timeout": 120,
  "keys": [{"id": "primary", "value": "<由用户安全提供的真实上游密钥>"}]
}
```

示例域名和密钥占位符须替换。协议从 `state["adapters"]` 选择；`openai-chat` 与 `openai-responses` 是不同协议，不根据 URL 或品牌猜测。读取所选适配器的 `auth_schemes`，将其中一个 `id` 写入 `auth_scheme`：

| scheme | 凭据注入位置 |
| --- | --- |
| `bearer` | `Authorization: Bearer <key>` |
| `api-key` | `x-api-key: <key>`，保留普通 Chat 中转站的旧行为 |
| `azure-key` | `api-key: <key>` |
| `anthropic-key` | `x-api-key: <key>` |
| `gemini-query` | URL 查询参数 `key=<key>` |
| `gemini-key` | `x-goog-api-key: <key>` |
| `none` | 不发送凭据 |

不同适配器只开放其中的适用项，自定义适配器也可以声明自己的 scheme。所有凭据均通过 `keys` 加密保存，由网关注入；不能将密钥塞进 `headers`、`profile` 或 `base_url`。`none` 可配空密钥池。本机或私网地址需要用户选择该连接范围后显式设置 `allow_private_network: true`。Base URL 不能带查询参数、认证信息或未替换的 `{account_id}` 等占位符。

## 编辑连接

`PATCH /admin/api/upstreams/{upstream_id}` 使用目录 revision，`value` 只带要改的字段。常用字段为 `name`、`base_url`、`adapter_id`、`auth_scheme`、`timeout`、`enabled`、`allow_private_network`、`template_id`、`tokenizer`、`profile`、`headers`、`proxy`、`note`。

省略 `keys` 会完整保留密钥池。不要把普通快照的 `credentials`、掩码、发现时间或加密字段整包写回。认证凭据通过 `keys` 写入，不能放进普通 `headers` 或 URL。`tokenizer` 从快照的 `tokenizers` 中选择，`auto` 自动选择；不要用模型名字臆造计量器或能力。

切换协议或模板前核对所有关联模型及路由。修改已有连接的 `template_id` 不会重新套用模板默认字段，需要同时提交确实要更改的连接参数。

## 额外参数、代理与配置错误

连接级参数放在 `profile.extra_body` 与 `profile.extra_query`，例如：

```json
{
  "profile": {
    "extra_body": {"enable_search": true},
    "extra_query": {"api-version": "2024-08-01"}
  },
  "proxy": "http://127.0.0.1:7890"
}
```

PATCH `profile` 会整体替换原 Profile；编辑前读取现值，保留原有推理档位映射、`thinking_mode` 或 token 字段配置。`extra_body` 不得覆盖 `model/messages/input/stream/tools/tool_choice` 等协议字段，也不得与适配器声明的编码字段冲突。`extra_query` 不得包含密钥、token 或覆盖协议查询参数。单次 KEMO 请求的 `provider_options` 仍受模型能力校验，与连接级额外参数无关。

`headers` 只用于非敏感头。认证头、`Host`、`Content-Length`、Cookie 和换行注入均被拒绝。`proxy` 为可选的无凭据 HTTP(S) 代理地址，不支持 SOCKS、内嵌账号密码或环境变量代理；清空用 `null`。代理可位于本机，但目标服务仍按连接的私网权限解析、校验并固定已检查 IP；链路本地及元数据地址始终被拦截。代理本身也经过地址校验。

保存时校验结构与字面地址，不发 DNS 请求；DNS 和连接问题会在实际调用或发现时出现。配置错误响应包含 `detail.code`、`detail.field`、`detail.message`，部分错误另有 `detail.details`。例如 `TEMPLATE_PLACEHOLDER_UNRESOLVED` 点明待替换占位符，`EXTRA_BODY_CONFLICT` 点明冲突键。向用户报告字段与原因，修正后重新读取 revision，不用重试掩盖配置错误。模板与连接的 `adapter_id` 必须一致。

## 追加、更换、禁用或移除上游密钥

**`keys` 是完整替换密钥池，不是追加命令。** 传入数组时必须包含所有需要保留的凭据。保留同一 ID 且省略 `value`，服务端保留原密文；同一 ID 带新 `value` 才替换原文；未出现在数组中的 ID 会被移除。

下面在当前池中追加一把备用密钥：

```python
upstream = next(item for item in state["upstreams"] if item["id"] == upstream_id)
pool = [
    {"id": item["id"], "enabled": item["enabled"], "priority": item["priority"]}
    for item in upstream["credentials"]
]
pool.append({"id": "backup", "value": os.environ["PROVIDER_BACKUP_KEY"], "priority": -10})
response = client.patch(f"/admin/api/upstreams/{upstream_id}", json={
    "expected_revision": state["revision"],
    "value": {"keys": pool},
})
response.raise_for_status()
```

替换某把密钥时，在保留数组的目标项中添加新的 `value`；禁用时设置该项 `enabled: false`；删除时移除该项。不要用空字符串表示删除，现有 ID 的空 `value` 会保留原密钥。数值更高的 `priority` 优先使用，同优先级轮换。每次编辑后重新读取快照，核对全部凭据 ID 和启停；健康状态来自真实操作，没有调用过的密钥可显示 `unknown`。

## 发现并添加模型

```python
response = client.post(f"/admin/api/upstreams/{upstream_id}/discover")
response.raise_for_status()
discovered = response.json()
```

发现只读取模型目录，不发起内容生成；结果包含 `models`、`source`、`verification`，参考结果还带有 `message`。上游给出列表时 `source=upstream`，不据此标记模型能力为已验证；上游返回空列表或不提供发现端点（404）时，可展示 `source=template`、`verification=unverified` 的模板参考，没有参考则 `source=manual`。401/403 直接报鉴权失败，DNS、网络与其他上游故障直接报连接或调用错误，均不回退到模板清单。

发现结果不会自动登记模型。没有目录时，使用用户/提供商文档给出的确切模型 ID 手动添加，不能据此断言服务不可用。模板的 `verification=declared` 表示连接预设未完成真实调用验证；它与每个模型的探测证据分别管理。

用户选定后按 [模型与路由](models.md) 登记。省略 `capabilities` 会真实探测选中模型；有明确能力资料且不需要探测时显式提供声明。

## 停用与删除

停用连接：PATCH `{"enabled": false}`，已有配置与历史保留。重新启用使用 `true`。

删除连接：先读取关联 `deployments`，按用户希望移除的范围删除它们，再 `DELETE /admin/api/upstreams/{upstream_id}`，带最新目录 revision 与空 `value`。仍有路由引用时返回 409。删除路由是否同时清理孤立公开模型，见 [模型删除规则](models.md#删除)。
