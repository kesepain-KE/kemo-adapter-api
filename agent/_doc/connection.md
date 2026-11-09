# 连接、认证与保存规则

[返回操作入口](README.md)

网关地址使用协议根地址，例如 `http://127.0.0.1:7531`，不要把 `/admin` 放进客户端模型服务地址。管理请求以 `/admin/api` 开头。

## 先连接并读取状态

下面示例使用项目已有的 `httpx`。环境变量是示例脚本读取凭据的方式；由操作者安全注入，不要把真实值写进命令历史、Markdown 或日志。远程连接使用 HTTPS。

```python
import os
import httpx

client = httpx.Client(
    base_url=os.environ.get("KEMO_GATEWAY_URL", "http://127.0.0.1:7531"),
    timeout=30,
)
try:
    management_token = os.environ.get("KEMO_GATEWAY_MANAGEMENT_TOKEN")
    if management_token:
        client.headers["Authorization"] = f"Bearer {management_token}"
    else:
        response = client.get("/admin/api/auth/methods")
        response.raise_for_status()
        methods = response.json()
        if not methods["configuration_valid"]:
            raise RuntimeError("网关认证配置不完整")
        if methods["token_required"]:
            response = client.post("/admin/api/auth/token", json={
                "token": os.environ["KEMO_GATEWAY_WEB_TOKEN"],
            })
            response.raise_for_status()
        if methods["password_required"]:
            response = client.post("/admin/api/auth/password", json={
                "username": os.environ["KEMO_GATEWAY_USERNAME"],
                "password": os.environ["KEMO_GATEWAY_PASSWORD"],
            })
            response.raise_for_status()
        if methods["token_required"] or methods["password_required"]:
            response = client.get("/admin/api/auth/session")
            response.raise_for_status()
            client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    response = client.get("/admin/api/console")
    response.raise_for_status()
    state = response.json()
    # 在此连接中执行对应手册的操作，保留 Cookie 和 CSRF Token。
finally:
    client.close()
```

其他手册中的 `client` 和 `state` 指上面同一连接及其已成功读取的快照；将操作放在关闭连接之前。每个请求检查状态后再读取结果。

## 选择正确的身份

| 凭据 | 用途 |
| --- | --- |
| 上游 API Key | 网关访问提供商，由 `keys` 字段写入对应上游 |
| 下游调用 Token | 智能体访问模型；不能用来管理目录或密钥 |
| Web Token、Web 用户名和密码 | 换取管理 Cookie 会话；Web Token 本身不直接作为管理 Bearer |
| 已配置且具有 `admin:web` 或 `owner` 的 Bearer | 非浏览器管理客户端；配置来自 `GATEWAY_API_KEYS_JSON`，不由下游密钥创建界面产生 |
| Status Token | 读取脱敏状态，不是管理凭据 |

直接本机回环访问支持免登录；代理、公网地址与当前认证设置会影响是否需要登录，以认证查询和实际响应为准。不能因为连接到 `127.0.0.1` 就断言必定免登录。

`admin:web` 可以读取管理状态并修改提供商、上游凭据、模型、路由和运行策略。下游密钥写入/复制/删除、扩展修改、启动文件读取/编辑和重启需要 `owner`。先查看快照的 `permissions`，服务端仍会校验每次操作。

Cookie 会话的写请求携带 `X-CSRF-Token`，浏览器还需同源。Bearer 管理客户端不需要 Cookie CSRF Token；不要通过伪造 Origin 或移除鉴权绕过 403。

## 快照里要看什么

`GET /admin/api/console` 返回提供商 `upstreams`、公开模型 `models`、路由 `deployments`、下游密钥 `keys`、可用 `adapters` 和 `templates`、`settings`、`runtime`、`permissions`，以及两个不同的 revision。普通响应中的凭据是脱敏预览，不能拿预览值做认证或写回密钥。

## 写入格式与三个 revision

| 修改对象 | `expected_revision` 来源 |
| --- | --- |
| 上游、模型、Deployment、运行策略、扩展启停/安装 | 编辑前快照的 `revision` |
| 下游密钥创建、编辑、复制、删除 | 编辑前快照的 `key_revision` |
| 启动 YAML | `GET /admin/api/system/startup-config` 返回的文件 `expected_revision` |

常规目录和密钥写入：

```python
response = client.patch("/admin/api/settings", json={
    "expected_revision": state["revision"],
    "value": {"max_attempts": 2},
})
response.raise_for_status()
response = client.get("/admin/api/console")
response.raise_for_status()
state = response.json()
assert state["settings"]["max_attempts"] == 2
```

DELETE 和复制下游密钥也带 `{expected_revision, value: {}}` 正文。例外：发现模型没有正文；启动文件使用 `{expected_revision, yaml, secrets?}`；重启使用 `{force, reason}`；扩展安装使用 multipart。

编辑期间保留最初读取的 revision，不用轮询得到的新 revision 偷换旧草稿。连续完成多步操作时，每一步成功后重新读取状态，再准备下一步的编辑。

## 失败处理

| 结果 | 下一步 |
| --- | --- |
| 401 | 检查管理身份或重新登录；不要把调用 Token 当管理凭据 |
| 403 | 检查 owner/admin 权限、CSRF、来源；在缺少权限时说明所需身份 |
| 404 | 重新读取目录，确认 ID、对象是否已被删除 |
| 409 | 重新读取状态，对比他人修改，再合并仍符合用户意图的字段；不盲目重试旧写入 |
| 422 | 依据字段、对象引用、能力或策略错误修正输入，原配置不应视为已修改 |
| 429 | 根据响应和 `Retry-After` 调整请求，停止批量探测 |
| 502/504 | 检查上游与日志；不要将网络/探测失败报告成配置成功 |

写请求发生连接中断或超时时，先读取对象和状态判断是否已经完成，再决定是否补做；创建密钥、重启或有费用的操作不能盲目重发。多步操作不是跨请求事务，某一步失败时核对已完成步骤并准确报告，不自动删除已经存在的配置。
