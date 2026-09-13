# Web 管理端

本目录用于网关管理网页，包括 Provider 状态、模型能力、密钥元数据、Usage 和运行状态管理。

Web 的内部接口不属于公开 LLM API，不写入顶层 `api.md`。Provider 厂商密钥、请求头和 Provider
State 明文永远不得返回浏览器；Provider 密钥池及只读配置页由后端生成前五后三掩码，短值完全隐藏。
网关调用 Token 只有 owner 通过会话、同源和 CSRF 校验后，才能由短时 reveal 接口按需查看，
响应必须 `no-store`，前端在短生命周期结束后立即清除。

## 技术栈与目录

- `frontend/`：React 19、TypeScript、Vite；生产构建由 FastAPI 挂载到 `/admin`。
- `backend/`：FastAPI 私有管理路由，统一位于 `/admin/api/*`。

浏览器先按 `.env` 配置完成 Web Token、用户名和密码流程，服务端签发 `HttpOnly` 会话 Cookie；
前端内存只保存写请求需要的 CSRF Token，不把 Web 凭据或会话 Token 放进浏览器存储。
管理接口也保留带 `admin:web` 或 `owner` scope 的直接 Bearer Token 兼容方式。
Provider 密钥原文为只写字段，普通状态接口只返回掩码和状态。对外 LLM API 被停用时，管理面
仍保持鉴权可用，以便管理员重新开启网关。三项 Web 凭据都为空时进入免登录 owner 模式，
只适用于受信任本机或局域网；公网必须启用两阶段鉴权、HTTPS 和主机白名单。

系统设置中的只读网关配置来自当前进程，不解析待生效 `.env`；修改环境变量后必须重启。
该接口、管理文档和 HTML 响应使用 `no-store`，凭据预览不能用于鉴权或写回配置。

## 启动

先构建前端，再从项目根目录启动：

```powershell
Set-Location web/frontend
pnpm run build
Set-Location ../..
python start_web.py
```

入口读取项目 `.env` 中的 `HOST`、`PORT` 和 `LOG_LEVEL`。`WEB_ACCESS_LOG` 控制 Uvicorn 访问
日志，`WEB_OPEN_BROWSER` 控制启动后是否打开 `/admin`；进程环境同名变量优先。所有这些变量
均为启动配置，修改后必须重启。

## 私有重启 API

重启接口只属于 Web 管理面，不写入公开 `api.md`：

| 方法与路径 | 权限 | 用途 |
| --- | --- | --- |
| `GET /admin/api/system/restart` | `owner` | 查询实例阶段、活动执行数和最近重启结果 |
| `POST /admin/api/system/restart` | `owner` | 提交 Drain 后重启，返回 `202` 和 request_id |

提交正文为 `{"reason":"...","force":false}`。普通 `admin:web` 无权重启；并发重启返回
`409`。前端可以轮询 GET 接口，依次展示 `queued → draining → stopping → starting → succeeded`
或 `failed`。
