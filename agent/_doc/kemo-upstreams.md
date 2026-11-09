# 接入 Kemo 协议上游

[返回操作入口](README.md) · [提供商与密钥](providers.md) · [模型与路由](models.md)

使用内置 `kemo-v2` 接入 Kemo 2.0 网关，使用 `kemo-v1` 接入归档 Kemo 1.0 服务。本地客户端始终使用 **Kemo 2.0**。两种适配器都支持模型目录、能力读取、文本、工具、流式、Embedding、Rerank 和 Asset 转移，实际可用范围由上游声明决定。

## 建立连接与登记

1. 从用户确认目标网关根地址、上游调用密钥和要开放的模型。地址不附加 `/model`；反向代理有前缀时保留该前缀。
2. 读取管理状态，选择 `kemo-v2` 或 `kemo-v1` 提供商模板。使用 Bearer 鉴权，将密钥写入加密凭据池。私网连接显式设置 `allow_private_network: true`。
3. 使用发现功能读取目录，也可以手填准确模型 ID。登记时省略 `capabilities`，适配器会通过上游能力接口导入完整声明；省略 `task` 时也从上游声明识别任务。
4. 读取登记结果。`verification: declared` 和 `source: upstream-capabilities` 表示上游声明，**不代表已执行生成验证**。按用户授权使用“测试模型”发起真实请求，产生的用量进入调用日志。
5. 为客户端配置本地公开模型名与本地调用密钥。用该密钥核对本地目录和能力，再按用户要求验证实际任务。

创建连接的 `value` 示例：

```json
{
  "id": "gateway-b",
  "template_id": "kemo-v2",
  "name": "Gateway B",
  "base_url": "http://192.168.1.20:7531",
  "allow_private_network": true,
  "keys": [{"id": "primary", "value": "<上游调用密钥>"}]
}
```

创建后重新读取 revision，登记到 `/admin/api/upstreams/gateway-b/register`：

```json
{
  "expected_revision": "<当前 revision>",
  "value": {
    "models": [{"upstream_model": "<上游公开模型名>", "public_model": "my-assistant"}]
  }
}
```

显式填写 `task` 时，必须与上游一致。显式填写 `capabilities` 会跳过能力读取，由操作者承担声明准确性。新增路由时仍须确保所有来源满足同一公开模型的能力。

模型编辑页的“探测能力”只刷新上游声明，不生成内容；“应用上游声明”会将完整结果写入当前表单，保存后生效。媒体专用模型的“测试模型”按其能力选择图片、语音、视频等请求。上游声明缺失、格式错误或任务不匹配时直接报告错误，不猜测能力。

## 媒体和工具结果

上游密钥除 `model:invoke` 外，媒体转发还需要 `asset:read` 与 `asset:write`。本地 Asset ID 不能直接交给另一台网关：

- 输入 Asset 先从本地已鉴权存储读取，再上传到上游，改写请求及媒体元数据中的引用。
- 输出 Asset 先校验上游描述符，再下载并校验校验和、大小和 MIME，登记为本地输出文件。
- 流式媒体完成事件与最终结果复用同一份本地 Asset；客户端从本地 `/assets/{id}/content` 读取。
- 图片、音频、视频、文件以及工具回执中的媒体均走上述路径，受两端存储大小、保留时间、权限与请求超时限制。URL 或内联媒体来源继续由上游处理。

文本、工具调用 ID 与回执在 2.0 之间保留；1.0 边界进行可逆前缀转换。上游 `request_id` 按调用身份、来源与模型重新生成，同一请求重试时保持一致，幂等头与正文一致。下游响应 ID、事件 ID 和序列由本地核心生成。`parent_request_id` 同样映射，保留后续工具回执的请求关联。

持久推理状态封装在本地 opaque 状态内，绑定上游 ID、地址与模型。后续请求只向原来源还原原始状态；更换来源时明确拒绝，不把旧状态交给另一家服务。多来源路由需要由客户端在切换后开始新一轮推理。

## 1.0 转换范围

1.0 兼容依据是仓库归档的 `kemo-1.0-wire-2026-09-13` fixture。它是兼容基线；不同旧服务仍需实际测试确认。

| 内容 | 行为 |
| --- | --- |
| 工具与推理 Item | 转换 `tool_call_ / call_`、`tool_result_ / result_`、`reasoning_ / rs_`；工具 call ID 在 `call_ / callid_` 间转换 |
| 并行工具开关 | 移入 `generation.parallel_tool_calls`，回执保持配对 |
| 推理档位 | 保留上游的动态档位，如 `adaptive-high`；支持已确认的 `none / auto / summary` 返回方式 |
| 逐项 metadata/extensions | 出站补齐，入站转换到 2.0 的 Item 边界；顶层元数据保留 |
| 流式 | 校验来源、序列、重复事件和终态；本地核心补齐 2.0 的 Item 开始与文本完成事件 |
| 未知错误码 | 转 `UNKNOWN_ERROR`，原值保存在 `error.details.original_code` |
| 无法确认表达的选项 | 拒绝强制、命名或 allowed 工具选择、带工具的 `none`、结构化输出、多候选、prediction、service、logprobs、verbosity 与推理 content 返回 |

能力导入会收窄到转换器能够执行的范围，不把 1.0 的结构化输出布尔声明当作可用的 2.0 功能。未知用量保留未知，不补零。遇到非空且未知的旧事件 `data`、断流、身份冲突或不合法响应时明确失败。

## 环路、更新与验证

配置阶段拒绝网关自指地址，包含配置的监听地址、公开地址和回环别名。联邦请求携带 `X-Kemo-Federation-Hops`；最多允许两次转发，超限拒绝。反向代理须保留该头；旧服务若不实现跳数检查，仍需避免配置环路。

网关版本保持 `2.0.0`，本次内置适配器包与模板包为 `1.3.0`。首次引入原生能力读取和环路入口需要**更新本次框架源码并重启**；仅覆盖两份资源目录不能给旧框架补上这些入口。旧框架的资源预检失败时会回滚并提示不兼容。后续兼容更新可按 [资源独立更新](resources.md) 执行，用户资源与运行数据继续独立保存。

离线回归使用两个临时网关和归档 1.0 数据，不读取用户密钥，不调用计费服务：

```sh
python -m pytest tests/gateway/test_kemo_upstreams.py tests/gateway/test_kemo_v1.py -q
```

真实上游验收按用户授权测试文本、工具回执、流式、目标媒体或检索任务；检查本地输出 Asset 可读、错误和用量符合真实响应。
