# 协议适配器、提供商模板与独立更新

[返回操作入口](README.md) · 先完成 [连接与认证](connection.md)

本手册指导第三方智能体在用户授权范围内扩展和维护网关。管理安装和更新使用 owner 身份，沿用登录会话、同源检查、CSRF 与目录 revision。所有资源状态从 `GET /admin/api/console` 读取；不要直接编辑运行对象、数据库密文或安装记录。

## 内置与用户

| 类型 | 内置资源 | 用户资源 |
| --- | --- | --- |
| 协议适配器 | `gateway/adapters/builtin/` | `extensions/protocol_adapters/<id>/` |
| 提供商模板 | `templates/provider_presets/<id>/` | `extensions/provider_templates/<id>/` |
| 来源值 | `source: builtin`，`builtin: true` | `source: user`，`builtin: false` |
| 更新行为 | 官方更新或源码修复会清除并替换对应目录 | 框架更新、资源更新、修复均保留 |
| 使用方式 | 内置启用 | 安装后未启用，通过管理请求启用 |

来源由网关根据目录和安装通道确定，不接受包内自行提升来源。需要保留的定制必须使用用户资源和独立 ID；不要直接修改内置文件后期待更新保留。新安装的用户 ID 不可与内置同名。旧版已安装的同名用户模板继续沿用原有启用和引用规则，更新不会改名或重写它的上游；后续新建时使用独立 ID。官方新增资源与现有用户 ID 冲突时，独立更新会拒绝覆盖，提示先处理命名。

模板提供创建上游的默认值。已创建上游的地址、Profile、密钥与超时不会被新模板重填。上游仍引用模板 ID 和协议 ID，因此更新后的模板必须保持引用兼容；独立更新会实际构建当前目录进行检查，不兼容会恢复原资源。

## 新增用户提供商模板

先读取当前 adapters 列表，选择真实存在、已启用的协议，按其 `auth_schemes` 和 `config_schema` 填写模板。下面使用 `openai-chat`，请改成目标服务的实际数据：

`manifest.yaml`：

```yaml
id: user-example
name: My service
version: "1.0.0"
contract_version: "1.0"
description: 用户维护的服务配置
category: provider
verification: declared
```

`template.yaml`：

```yaml
adapter_id: openai-chat
name: My service
base_url: https://example.invalid/v1
auth_scheme: bearer
headers: {}
profile: {}
tokenizer: auto
catalog: []
timeout: 120
allow_private_network: false
```

`catalog` 可填写已知模型 ID，作为未验证参考。不要在模板、README 或 ZIP 内保存真实密钥；创建上游时用 `keys` 字段提交。鉴权头交给凭据管理，非敏感头填 `headers`，附加参数放 `profile.extra_body / extra_query`，不得覆盖协议和鉴权字段。

将 `manifest.yaml`、`template.yaml` 和可选 `README.md` 压在 ZIP **根目录**。包内不允许父目录、子目录、链接或安装脚本。

```python
import io
import zipfile

buffer = io.BytesIO()
with zipfile.ZipFile(buffer, "w") as archive:
    archive.writestr("manifest.yaml", manifest_yaml)
    archive.writestr("template.yaml", template_yaml)

state = client.get("/admin/api/console").json()
response = client.post("/admin/api/extensions/provider_templates/install",
    data={"expected_revision": state["revision"]},
    files={"file": ("provider.zip", buffer.getvalue(), "application/zip")})
response.raise_for_status()
identity = response.json()["id"]

state = client.get("/admin/api/console").json()
response = client.patch(f"/admin/api/extensions/provider_templates/{identity}", json={
    "expected_revision": state["revision"], "value": {"enabled": True}})
response.raise_for_status()
```

安装不会自动启用。启用会检查所引用协议、鉴权和 Profile；失败时保留原运行目录。启用成功后按 [提供商与密钥](providers.md) 创建上游，模板可在网页上看到并选用。

## 新增用户协议适配器

复用项目提供的可运行示例：`templates/protocol_adapter/manifest.yaml` 与 `templates/protocol_adapter/adapter.py`。示例实现一个自定义 JSON/SSE 协议，包含请求编码、响应解码、模型发现和用量处理。根据真实厂商协议修改，替换示例 ID、端点与字段，不要把示例映射当成真实协议。

1. 从 `console.adapters` 确认目标协议不存在，选择唯一 ID，例如 `user-native`。
2. 清单保留 `contract_version: "1.0"`，填写资源自己的 `version`、实际操作 `llm/embedding/rerank`、端点、鉴权、输入输出、流式和配置 schema。声明范围必须与实现一致。
3. `adapter.py` 导出 `create_adapter(transport)`；通过收到的 Transport 发起网络请求。不要自行创建 HTTP 客户端绕过地址校验、代理、超时和错误分类。
4. 将 `manifest.yaml`、`adapter.py`、可选 README 压到 ZIP 根目录，安装到 `/admin/api/extensions/protocol_adapters/install`，正文和前例相同。
5. 用 `PATCH /admin/api/extensions/protocol_adapters/<id>` 启用，再创建对应上游，手动填准确模型 ID，按已授权范围测试和探测。
6. 排障读取 `GET /admin/api/extensions/protocol_adapters/<id>/logs` 与具体调用详情。不要把包含业务内容的日志整段复制到公开报告。

用户协议运行在独立 worker 中，通过统一网络 Transport 和按本次身份绑定的 `context.assets` 读写媒体。可以按真实实现声明图片、音频、视频或文件输入输出；主进程校验并发布 worker 输出，避免将大文件放进 RPC。接口、探测声明和限制见 [音频、视频和文件](media.md)，图片服务见 [图片模型](images.md)。worker 是进程隔离，不是恶意 Python 代码沙箱，安装内容需在用户授权的信任范围内。

## 更换用户资源版本

安装相同 ID 默认拒绝覆盖。需要更新时：

1. 读取哪些上游/模板引用它。仍被引用的资源不能直接停用；先按用户目标迁移引用，或使用新 ID 安装后逐项切换。
2. PATCH `enabled: false`，确认发布成功。
3. 再次安装同 ID 的包，multipart 增加 `replace: "true"`，使用最新目录 revision。
4. PATCH `enabled: true`；网关重新记录文件摘要并完整验证。
5. 检查相关上游、路由、模型能力，再执行已授权的测试。

直接改已启用扩展文件会触发摘要不一致，不能通过这种方式偷偷改变运行内容。替换只触及对应用户资源，不会改其上游、模型、密钥或日志。

## 从 GitHub 单独更新内置资源

框架保持 `2.0.0`、Kemo 协议保持 `2.0`。适配器和每个模板有自身版本；两类目录各有资源包版本清单。官方变更共享适配器代码、增删条目或更新模板时，要递增相应资源包版本；修改单个条目时同时递增条目版本。独立更新允许同一框架版本获得新资源。

命令行：

```sh
python update.py --resources-check --branch kemo-preview
python update.py --resources-apply --branch kemo-preview --yes
```

稳定分支使用 `--branch main`。无参数更新菜单也有“只更新内置协议适配器和提供商模板”。CLI 可显式指定 `--repo-url`；管理网页和 HTTP 更新固定从项目官方 GitHub 获取，只接受 `main` 或 `kemo-preview` 分支。

智能体通过 HTTP：

```python
state = client.get("/admin/api/console").json()
response = client.post("/admin/api/resources/check", json={
    "expected_revision": state["revision"], "value": {"branch": "kemo-preview"}})
response.raise_for_status()
plan = response.json()

if plan["available"]:
    # 在用户已授权更新范围内执行，提交刚检查过的同一份计划。
    state = client.get("/admin/api/console").json()
    response = client.post("/admin/api/resources/apply", json={
        "expected_revision": state["revision"],
        "value": {"target_commit": plan["target_commit"],
                  "resource_revision": plan["resource_revision"]}})
    response.raise_for_status()
    result = response.json()
```

检查返回每类资源包 `current/target/available/changes`，以及锁定的 Git 提交和本地资源摘要。执行不能替换成未检查的提交；本地文件或目录 revision 改变时需重新读取并检查。

更新只提取并替换上表的两个内置目录，不修改 Git HEAD、`version.json`、依赖、前端、`extensions/`、`gateway.yaml`、`.env` 和 `data/`。它与框架更新共用更新锁，拒绝路径穿越、符号链接、版本倒退和用户 ID 冲突；替换前备份，替换后在新进程中只读用户目录并验证适配器、模板及引用，失败恢复旧文件。新资源若依赖当前框架没有的能力或依赖，也会拒绝并恢复，需先更新框架。

成功返回 `backup` 与 `restart_required: true`。旧进程继续使用其原有适配器和模板快照；通过既有重启接口或 `python restart.py` 重启后生效，再读取 `console.resources.running` 和资源列表核对版本。不要在尚未重启时宣称新版本已运行。

资源备份在 `.backup/resources/<id>/`，包含更新前两个资源目录和 `update.json`；整库备份列表不把它当作框架快照。若需人工恢复，应在停服后仅恢复这两个对应目录，再做启动预检和重启，勿复制备份覆盖 `data/` 或用户扩展。
