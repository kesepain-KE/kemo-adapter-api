# 高级插件：安装、授权和模型接管

高级插件是独立安装的 **Provider 来源**，把本机 CLI、已授权账户或其他模型服务接入网关。插件模型进入现有 RuntimeRegistry 和执行器，使用 Kemo 2.0、下游密钥权限、调用记录、统计、幂等和 SSE 重放。开发插件另读 [SDK v1](plugins-sdk.md)。

## 安装顺序

1. 按 [连接与认证](connection.md) 获取管理客户端。读取 `GET /admin/api/advanced-plugins`，保存独立 `revision`，不要使用普通目录 revision。
2. multipart 上传 ZIP 到 `POST /advanced-plugins/preview`，字段 `file`。只校验、不执行代码。展示名称、作者、版本、兼容范围、权限、SHA-256 和未签名来源提示。
3. 用户确认来源后，`POST /advanced-plugins/install`，multipart 字段 `file`、`expected_revision`、`sha256`、`update=false`。新安装插件默认停用。
4. 读取 `GET /{id}/config`，按 `schema` 填写配置。`PATCH /{id}/config` 正文 `{expected_revision, value: {...}}`；遗漏字段保留，秘密字段只返回 `secrets_set`。显式 `null` 清除字段，仍需满足 Schema。
5. 用户明确同意权限后，`POST /{id}/enable`，正文 `{expected_revision, value: {grants: [...]}}`。grants 须等于清单请求的权限；启用会运行第三方代码，不代替用户批准未知插件。
6. 读取 `GET /{id}/models`、`GET /{id}/diagnostics`，再用下游自己的密钥读取 `/model/models`。不把“安装成功”描述为“账户已授权”或“模型可用”。

上面缩写的路径前缀为 `/admin/api/advanced-plugins`。Cookie 写请求须 CSRF 和同源；安装、配置、启停、更新、卸载、源管理及动作均须 owner。

## 生命周期

| 需求 | 管理操作 |
| --- | --- |
| 库存与状态 | `GET /advanced-plugins`、`GET /{id}` |
| 授权启用 | `POST /{id}/enable`，`value.grants` |
| 停用并撤销权限 | `POST /{id}/disable` |
| 重载并发现模型 | `POST /{id}/reload` |
| 真实探测并发布模型 | `POST /{id}/probe`，可带 `value.model`；可能产生费用 |
| 上传更新 | `POST /install`，`update=true`，须先预览摘要 |
| 恢复上一版本 | `POST /{id}/rollback` |
| 卸载并撤销路由 | `DELETE /{id}`；保留配置、凭据和历史 |
| 能力与诊断 | `GET /{id}/models`、`GET /{id}/diagnostics` |
| 插件动作 | `POST /{id}/actions/{action}`，value 由插件定义 |
| 专属 UI | `GET /{id}/ui/manifest` |

除上传外，写入正文均为 `{expected_revision, value}`。409 时重新读取，不覆盖并发修改。更新先验证候选进程与模型，再切换新请求；失败时旧版本继续服务。新增权限的更新须先停用、安装，再授权。旧请求持有原进程引用，完成或取消后退役；停用立即撤销新请求。

模型启停、命名、能力及默认选项由插件配置定义，改变后重载或探测。主模型页展示插件来源及管理入口，下游密钥可选择插件模型白名单。不要另建绕过权限的调用入口。

## 插件源

`PUT /sources` 正文 `{expected_revision, value: {sources: [...]}}`。源字段为 `id`、`name`、`url`、`enabled`、`allow_private_network`；地址不能含凭据、查询或 fragment。私网须明确允许，DNS/连接复用现有网络校验，不跟随重定向。

`GET /catalog` 返回实际索引及逐源错误。先 `POST /catalog/preview` 再 `POST /catalog/install`，正文：

```json
{"expected_revision":"库存 revision","value":{"source_id":"company","id":"my_plugin","version":"1.0.0","update":false}}
```

市场固定索引版本和 SHA-256，预览、安装均核对下载包身份与摘要；不执行源站脚本。索引格式见 SDK。

## 示例与 CLI

```sh
python -m gateway.plugins check examples/advanced_plugins/local_text
python -m gateway.plugins pack examples/advanced_plugins/local_text local-text.zip
python -m gateway.plugins pack examples/advanced_plugins/codex_cli codex-cli.zip
```

`local_text` 真实执行离线 Unicode 文本转换，验证完整包、进程、注册及协议链路；它不是语言模型，不伪造推理或 Token。模型为 `local-text-transform`。

Codex 示例配置原生 `executable`、已授权 `auth_home` 和 `model`。启用后检查 CLI/登录态，由管理员主动探测；真实请求成功才登记模型。CLI 不在同机、未安装、未登录或探测未通过时不宣称可用，不含远程 Worker。

桥接不加载用户配置、MCP、规则或历史会话，关闭 shell、unified_exec、多智能体和 Web 搜索，使用只读沙箱和独立工作目录。内部工具/推理不转为下游工具调用。只映射完成回答；可提供标准 SSE，但不伪装逐 Token 输出。Usage 仅采用 CLI 实际报告，缺失为 unknown。

## 存储与可信边界

代码在 `data/advanced_plugins/packages/<id>/<sha256>/`，运行数据在 `runtime/<id>/`，库存、配置及授权在 `inventory.json`。顶层 `writeOnly` 字符串由 SecretVault 加密。全部位于 Git 忽略及更新器保护的 data，不随源码更新覆盖。卸载保留的配置可在重装相同 Schema 时恢复。

进程隔离负责故障和生命周期，**不构成 OS 安全沙箱**。权限清单是管理员的信任批准边界；只启用可信作者。不可信代码应在独立 OS 用户、容器或虚拟机运行。本 SDK 不宣称限制任意 Python 的全部文件或网络权限。

专属网页在严格沙箱 iframe，CSP 拒绝网络及表单。Bridge 绑定指定 MessageChannel、每次加载 nonce、版本和请求 ID；修改仍由管理 API 鉴权。页面不获得管理 Cookie、密钥或宿主目录。stderr 仅展示有界字节统计，具体排错通过插件结构化脱敏 diagnostics。
