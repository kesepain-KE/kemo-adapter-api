# Advanced Plugin SDK v1

实现位于 `gateway/plugins/`。示例位于 `examples/advanced_plugins/local_text`、`codex_cli`。运行时是独立 Python 进程，不运行安装脚本或自动安装依赖。可随包携带纯 Python 模块，其他依赖由管理员预先安装。

## 包、校验与版本

ZIP 根目录含 `plugin.yaml`、`backend/main.py`，可含 `ui/`、`module/panel.json`、README；不要再包一层目录。

```sh
python -m gateway.plugins create my-plugin --id my_plugin
python -m gateway.plugins check examples/advanced_plugins/local_text
python -m gateway.plugins pack examples/advanced_plugins/local_text local-text.zip
python -m pytest tests/gateway/test_advanced_plugins.py -q
```

正式严格 Schema 为 `gateway.plugins.schema.Manifest`，可用 `Manifest.model_json_schema()` 导出 JSON Schema。配置 Schema 支持包内 `#` 引用，拒绝外部引用，校验过程不会拉取远程 Schema。完整清单参看示例；关键字段：

```yaml
schema_version: 1
id: my_plugin
name: My Provider
version: 1.0.0
category: provider_extension
compatibility: {gateway: ">=2.0.0,<3.0.0"}
runtime: {kind: process, entry: backend/main.py, protocol: kemo-plugin-rpc-v1, timeout: 120, concurrency: 2}
provider: {enabled: true, provider_id: my-provider, model_discovery: dynamic}
ui: {mode: sandbox, entry: ui/index.html, fallback_panel: module/panel.json}
permissions:
  requested: [model.register, model.invoke, plugin.config.write]
config_schema:
  type: object
  additionalProperties: false
  properties:
    api_key: {type: string, writeOnly: true, title: API Key}
actions: {refresh-account: plugin.config.write}
```

Provider ID 全局唯一，不能为 gateway；公开模型为 `<provider_id>-<stable-name>`，不含斜杠、反斜杠或空白。category 为 `model_account / cli_provider / provider_extension`。无模型的增强插件可设 provider.enabled=false。

权限集合：`model.register`、`model.invoke`、`process.cli.execute`、`account.status.read`、`account.authorize`、`plugin.config.write`、`network.access`。动作映射到已请求权限，owner 明确批准后才执行代码。权限不是 OS 沙箱，见 [可信边界](advanced-plugins.md)。

秘密仅在顶层字符串字段声明 `writeOnly:true`；嵌套秘密或带 default/const/enum/examples 的秘密声明在安装校验时拒绝。不要把秘密放在普通对象、日志或诊断中。宿主加密存储，仅向该插件后端发送解密值；Web 接口返回非秘密 values 和 secrets_set。更新不得删除或公开已有秘密字段，先由管理员显式清除。

上传限制 16 MiB，解包 64 MiB / 1000 文件；拒绝路径穿越、符号链接、非法文件类型、Windows 保留路径、重复路径和异常压缩比。单帧 2 MiB，流队列 128，待处理请求有界。握手 15 秒，runtime.timeout 1–1200 秒，并发 1–16。

## 进程 SDK 与 RPC

后端提供 create_plugin()，返回 `gateway.plugins.sdk.Plugin` 子类。RPC 方法里的点号替换为下划线查找异步处理器。initialize 调用基类设置 self.config/self.data_root。stdout 专用于 JSONL；stderr 不公开原文。

```python
from gateway.plugins.sdk import Plugin, text_capabilities, text_result

class MyPlugin(Plugin):
    async def models_list(self, params):
        # 只能返回已配置、已授权且实际可调用的模型。
        return [text_capabilities("my-provider", "example")]

    async def models_execute(self, params):
        # 真实服务调用在插件实现中完成；完整可运行例子见 local_text。
        return text_result(params["request"]["input"][0]["content"][0]["text"].upper())

def create_plugin():
    return MyPlugin()
```

标准帧：

```json
{"jsonrpc":"2.0","id":1,"method":"models.execute","params":{"request":{},"context":{}}}
{"jsonrpc":"2.0","id":1,"result":{"status":"completed","output":[],"usage":{}}}
{"jsonrpc":"2.0","id":1,"done":true}
```

非流式恰好一个结果，流式多个结果，最后一个 done。失败使用 `{"id":1,"error":{"code":"PLUGIN_NEEDS_AUTH"}}` 再 done，不发送异常正文。未实现方法返回 PLUGIN_UNSUPPORTED。

| 方法 | 合同 |
| --- | --- |
| initialize | 接收 protocol、plugin_id、permissions、config、data_root；返回 `{protocol:"kemo-plugin-rpc-v1"}` |
| health/shutdown | 存活与关闭；基类提供处理，插件释放自身资源 |
| models.list | 完整 Kemo 2.0 ModelCapabilities 数组 |
| models.capabilities | 可选；公共目录使用已验证的注册能力快照 |
| models.probe | 真正探测，成功为 `{available:true}` |
| models.execute | ProviderResult 字典 |
| models.stream | 异步迭代 ProviderEvent 字典，唯一终态 |
| diagnostics | 脱敏依赖、授权及错误状态，不可查询额度为 unavailable |
| ui.actions.invoke | `{action,value}`，动作须在清单声明并获批 |

配置 schema/get/update 由宿主管理接口统一处理。账户状态放入 diagnostics，授权动作在清单声明；不支持的操作明确拒绝。SDK v1 开放**文本输入输出的 llm Provider**；媒体、embedding、rerank、多候选在注册边界拒绝，不假报支持。

params.request 是完整 KemoRequest；context 含当前租户、主体、请求/响应 ID、系统提示词，不含 AssetAccess 或管理凭据。核心预检已声明的工具、推理、结构化输出能力。插件必须实际实现才能声明。

```python
async def models_stream(self, params):
    result = await self.models_execute(params)
    yield {"kind":"output_text.delta", "item_id":"msg_plugin", "content_index":0,
           "delta":result["output"][0]["content"][0]["text"]}
    yield {"kind":"provider.completed", "result":result}
```

插件输出 ProviderEventKind，不直接组装 SSE。sequence、event_id、终态、持久化后发布及重放由既有执行器处理。不能把 CLI 的内部工具、进度和推理转为下游工具调用。

取消为 `$/cancelRequest`、参数 `{id:RPC请求ID}`。SDK 取消对应协程；插件 finally 关闭连接及自身子进程树。超时宿主发送取消，4 秒不响应则终止此进程树，受影响请求显式失败，无自动重试。原代际仍持有在途请求及取消目标。

Usage 采用原 Kemo measurement：权威值 mode=provider、exact=true、列明 exact_fields；估计列 estimated_fields；缺失为 `{}`。网关不为插件编造 Token/费用，计数和终态仍进入原统计与调用记录。

## 通用 panel.json

与 kemo-agent 共享基本语义，自行校验和渲染，不导入另一个项目。schema_version=1，containers 支持：

- status：fields 的 badge/text/markdown 按 key 读取脱敏 diagnostics；markdown 作为安全文本展示。
- config：string/text/enum/number/boolean、masked、options、min/max、default；字段 key 必须对应 config_schema，面板负责分组、标签、选项名称与多行文本，类型和范围以 config_schema 为准。masked 的配置字段必须声明 writeOnly；presets.values 填充非秘密配置。
- action：button/send、command、inputs；command 须在 actions 声明。source/values 文件可保留声明，但运行数据由宿主 API 提供，浏览器不能读任意文件。

专属 UI 和通用配置并存，UI 故障不影响其他插件。

## 专属 UI Bridge

页面及资源位于 ui/，入口必须是 ui/*.html。使用自有相对 JS/CSS，禁止内联脚本。后台文件、配置及源码 map 不公开。iframe 只有 sandbox=allow-scripts，CSP 禁止网络、表单、对象和顶层跳转；不导入宿主 React。保存等交互使用 type=button 的 click 事件调用 Bridge，不依赖被沙箱禁止的原生表单提交。

宿主通过已认证的 ui/manifest 获取 asset_base，再用该地址加载页面及相对资源。地址包含仅存内存、有效两小时的随机凭据，只可读当前插件版本的 ui/ 静态文件，不赋予管理 API 或模型调用权限。插件更新、卸载或网关重启后原地址失效；过期时重新加载控制台。这样严格沙箱中的 JS/CSS 无需携带管理员 Cookie。管理读写全部经下述 Bridge 完成。

宿主在 load 后传递 MessageChannel，插件检查 `event.source===parent`、`event.data.type==="kemo-plugin-init"`、转交的 port，再发送：

```javascript
port.postMessage({version:1, nonce, id:1, method:"plugin.getContext", params:{}});
```

响应含相同 id/nonce，返回 result 或 error。后续只接收该 port，绑定版本、nonce 和请求 ID；不是因为 origin=null 就信任任意 window 消息。卸载销毁 port、timer、监听器。每页最多 8 个并发管理调用。

Bridge 方法：plugin.getContext、plugin.config.read/update、plugin.model.list/probe、plugin.account.status/action、plugin.action.invoke、plugin.ui.resize。写操作须 owner，后端重验权限、revision、CSRF。getContext 只返回插件 ID/版本、主题、语言和页面，不含 Cookie、凭据和宿主路径。完整 Promise/超时示例见 local_text/ui/plugin.js。

plugin.ui.resize 接收 `{height: 数值}`，宿主将高度限制在 320–2000 像素并返回实际 `{height}`；页面可用 ResizeObserver 上报内容高度。

页面支持白色/暗色和窄屏；展开/全屏由宿主处理，不销毁页面状态或重启插件。

## 索引

```json
{"schema_version":1,"plugins":[{"id":"my_plugin","name":"My Provider","version":"1.0.0","category":"provider_extension","author":"Example","summary":"Actual source","url":"https://example.com/my-plugin.zip","sha256":"64 个小写十六进制字符"}]}
```

每源 2 MiB / 500 条，固定版本及摘要，下载不执行源站代码。当前为摘要验证，无签名信任链，界面明确显示 unsigned。

## CLI 示例与兼容

Codex 示例由管理员指定原生二进制、CODEX_HOME 和模型，不导入其他主机凭据，不读取 Token 正文。主动探测实际调用模型，须授权且可能计费。成功记录模型、CLI 版本和授权目录，升级 CLI 需重探测。不猜测账户完整模型列表。

调用参数以本机 codex exec --help 为准，包括 --json、--ephemeral、--ignore-user-config、--ignore-rules、--skip-git-repo-check、--sandbox read-only；关闭 shell/unified_exec/多智能体/MCP/Web 搜索和环境继承。Prompt 从 stdin 传递，Windows 原生 exe 直接启动，不拼 shell。只取 agent_message 及 turn.completed.usage，私有进度和工具日志丢弃。

清单版本为 1，运行协议 kemo-plugin-rpc-v1，框架仍 2.0.0。未知版本拒绝；更新验证成功才切换新请求，失败保留旧 Provider。
