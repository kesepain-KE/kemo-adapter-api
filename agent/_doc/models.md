# 模型与路由

[返回操作入口](README.md) · 先完成 [连接与认证](connection.md)

公开模型是客户端选择的名字；Deployment 将公开模型映射到提供商的原始模型 ID。同一个公开模型可以有多个来源，路由通过 ID 和关联关系工作，不解析公开名称前缀。

## 登记选定模型

`POST /admin/api/upstreams/{upstream_id}/register` 带目录 revision：

```json
{
  "expected_revision": "<编辑前的目录 revision>",
  "value": {
    "models": [{
      "upstream_model": "<提供商的原始模型 ID>",
      "public_model": "my-assistant",
      "task": "llm",
      "capabilities": {
        "input_modalities": ["text"],
        "output_modalities": ["text"]
      }
    }]
  }
}
```

这会创建公开模型与对应 Deployment；上例显式提供能力声明，**不会发起能力探测**。声明使用用户或提供商确认的能力，不把例子中的能力自动套给未知模型；`capabilities: {}` 表示采用协议默认的基础声明，不代表已验证。结果仍以实际快照为准。

省略 `capabilities` 会探测所选模型，可能产生多次计费请求和较长耗时。仅在用户已授权测试时使用，适当增加客户端请求超时，读取 `detection` 的逐项证据。HTTP 200 不表示每项能力都已确认；未知项保留未知。手动声明状态为 `declared`，不能直接写 `verification` 或 `verified_at`。

网页“登记选中模型”走此自动探测流程，包含推理档位检查。OpenAI Chat 先用提供商 Profile 的 `reasoning_effort_map`；未配置映射的自定义提供商逐项尝试 `minimal / low / medium / high / xhigh / max`，只发布有推理 Token 或推理输出证据的档位。原厂目录若返回 `effort.supported_levels`，会将可编码的档位记录为原厂声明，与响应验证分开；明确被拒绝的档位不会由元数据重新开放。返回普通文字但不含推理证据的结果仍为未知，不能把所有候选档位批量标成支持。

DeepSeek 的 Profile 应保留 `thinking_mode: true` 与 `reasoning_effort_map: {low: low, high: high, max: max}`；显式开关对应原厂 `thinking.type`，档位对应 `reasoning_effort`。模板更新不会覆盖已经创建的上游，编辑已有连接时合并 Profile 并保留原密钥。自定义档位可通过映射声明；OpenAI Responses 使用原生 `reasoning.effort`。模板与映射只说明如何调用，实际支持范围仍由具体模型的证据决定。

网关将能力声明中的标准档位按 `minimal → low → medium → high → xhigh → max` 排序，仅返回模型已有的档位。自定义档位保留在标准档位之后，并保持彼此的原始顺序；客户端不能据此推断自定义档位的思考强度。此规则也适用于已保存模型、手动编辑和重新探测的结果，读取时无需再次探测。

工具能力分别声明在 `tools.function_calling` 和 `tools.parallel_calls`。确认单次函数调用后，自动探测会另发一次并行请求；只有同一响应包含要求的两个工具调用及对应参数，才声明支持并行。HTTP 200 或仅返回一个调用不能作为并行证据。客户端应按能力声明设置 `parallel_tool_calls`；模型支持工具但未声明并行时，发送 `false` 仍可正常使用工具。自动探测无法确认时，可在网关模型配置页手动开启“并行工具”，保存后通过 KEMO 能力接口传递给客户端。

`public_model` 可自定义，最长 200 字符，不接受 `/`、反斜杠或空白。省略时根据提供商模板和模型生成名称，已登记的合法公开名不随模板或连接改名而变化。客户端要使用登记后的公开名，提供商原始 ID 保留在 Deployment 的 `upstream_model` 中。`task` 为 `llm`、`embedding` 或 `rerank`，必须属于所选协议的实际能力。

## 选择模型与确认权限

1. 从 `state["models"]` 确认公开名、任务、`enabled` 和能力。
2. 从 `deployments` 找到该模型的启用来源，核对对应上游已启用且有可用凭据。
3. 核对客户端调用密钥的 `allowed_models` 和 `allowed_ops`。多个模型对该智能体可见时，在客户端选择具体的公开名；不要为了一个客户端切换而停用全局模型。
4. 用客户端自己的 Token 读取目录与能力，确认可见性，不生成内容：

```python
with httpx.Client(base_url=str(client.base_url), timeout=30, headers={
    "Authorization": f"Bearer {downstream_token}",
    "X-Kemo-Protocol-Version": "2.0",
}) as caller:
    response = caller.get("/model/models", params={"task": "llm"})
    response.raise_for_status()
    available = response.json()["data"]
    assert any(item["id"] == public_model for item in available)
    response = caller.get("/model/capabilities", params={"model": public_model})
    response.raise_for_status()
    capabilities = response.json()
```

独立连接避免复用管理 Cookie 或覆盖管理 Bearer。随后按用户指定的客户端格式设置网关根地址、调用 Token 和公开模型名，不推测或修改其他客户端配置。

## 编辑公开模型

`PATCH /admin/api/models/{public_model}`，使用目录 revision。

| 目标 | `value` |
| --- | --- |
| 修改显示名称 | `{"display_name": "日常助手"}` |
| 全局停用/启用模型 | `{"enabled": false}` / `{"enabled": true}` |
| 修改公开名 | `{"id": "new-assistant"}` |
| 修改能力 | `{"capabilities": <合并后的完整能力对象>}` |

`capabilities` 是整个嵌套对象替换，先与读取时的能力合并，保留不需要修改的已知字段；例如修改 `limits.context_window` 时保留其他 limits。填真实的上下文、输入和输出上限，不为通过预算校验而编造数值。手动修改能力会清除自动验证时间并回到 `declared`。

公开名重命名会原子更新关联 Deployment 和下游密钥显式模型白名单。客户端保存的旧模型名不会自动更改，需同步更新用户指定客户端。能力必须由所有候选路由共同支持。

## 新增或调整路由

需要将另一个来源接到已有公开模型时，可以直接 `POST /admin/api/deployments`。公开模型和上游都必须先存在：

```json
{
  "upstream_id": "<已有上游 ID>",
  "upstream_model": "<该来源的原始模型 ID>",
  "public_model": "my-assistant",
  "priority": 10,
  "weight": 1,
  "enabled": true,
  "rpm": 0
}
```

将此对象放进 `{expected_revision, value}`。未提供 `id` 时服务端生成。也可以先 `POST /admin/api/models` 创建 `{"id": "my-assistant", "task": "llm", "capabilities": {...}}`，再创建 Deployment；这两步分别提交，不是跨请求事务。

修改使用 `PATCH /admin/api/deployments/{deployment_id}`。`priority` 越大越优先，同优先级按 `weight` 选择；`rpm: 0` 表示该路由不设每分钟次数限制。停止使用一个来源只需停用该 Deployment。

价格对象 `price` 按每百万 Token 设置 `input_per_million`、`output_per_million`、`cached_input_per_million` 和三位大写 `currency`。PATCH 中的 `price` 会整体替换，先保留原有价格字段再修改。价格来自用户或提供商确认的信息，未知价格保持 `null`，不要填 0 冒充免费。

## 测试与探测

`POST /admin/api/deployments/{deployment_id}/test` 执行一次基础模型测试；可通过查询参数 `first_token=true` 按流式实现测首字延迟。`POST .../probe` 重新探测多项能力。两者都使用目录 revision、`value: {}`，会访问真实上游，且不会自动修改公开模型。

读取检查结果后，需应用的能力再按普通编辑方式保存。共享公开模型不能根据单个 Deployment 的探测扩大所有来源的能力。失败和未知结果据实呈现。

## 删除

- 删除一个来源：`DELETE /admin/api/deployments/{deployment_id}`。默认保留公开模型；`?remove_orphan_model=true` 会在最后一个路由移除时同时清理该公开模型。
- 删除整个公开模型：`DELETE /admin/api/models/{public_model}`，移除全部关联路由及下游密钥白名单中的该模型，保留其他对象和历史调用。

两者都带目录 revision 与 `value: {}`。明确区分用户要“移除一个提供商的模型”还是“删除整个公开模型”，按已授权范围执行。删除最后一个允许模型后，调用密钥白名单应为 `[]`，不能扩大为 `null`。
