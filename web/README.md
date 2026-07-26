# Web 管理端

本目录用于网关管理网页，包括 Provider 状态、模型能力、密钥元数据、Usage 和运行状态管理。

Web 的内部接口不属于公开 LLM API，不写入顶层 `api.md`。Web 不得向浏览器返回厂商完整密钥、
Bearer Token 或 Provider State 明文；密钥页面只能展示 key id、状态和脱敏后的后四位。

## 技术栈与目录

- `frontend/`：React 19、TypeScript、Vite；生产构建由 FastAPI 挂载到 `/admin`。
- `backend/`：FastAPI 私有管理路由，统一位于 `/admin/api/*`。

管理 API 必须使用带 `admin:web` 或 `owner` scope 的 Bearer Token。浏览器只在当前会话保存
用户输入的管理密钥；Provider 密钥为只写字段，后端不会读回。对外 LLM API 被停用时，管理面
仍保持鉴权可用，以便管理员重新开启网关。

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
