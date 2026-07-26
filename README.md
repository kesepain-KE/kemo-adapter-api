# Kemo Provider Gateway

Kemo 网关为 kemo-agent 提供统一 LLM Provider 协议，把不同厂商的请求、流式事件、工具调用、
能力、错误和 Usage 转换为稳定的 Kemo Response/SSE。

![Kemo Gateway](kemo-adapter-api.png)

## 顶层结构

```text
ADD_DIY/                智能体创建厂商、管理密钥和验证操作手册
agent_control.md        智能体操纵网关的总索引
api.md                  对外 LLM/Asset API 说明，不包含 Web
providers/              动态发现的独立厂商包
api/                    对外 API 路由、认证和 SSE
web/                    网关管理网页
core/                   网关执行核心、统一模型和状态端口
kemo-adapter-api.png    网关 Logo
.env.example            环境变量样例
restart.py              网关重启模块
update.py               网关更新模块
version.json            网关与协议版本
tests/                  网关及 Provider 契约测试
setup.py                首次部署模块
start_web.py            网关与 Web 管理端启动入口
开发目录/               开发者私有资料，默认不上传 GitHub
```

本地 `.env` 由部署产生并被 Git 忽略，不属于发布文件。

## 生效策略

无需重启的内容仅有：

- `api/runtime.json` 和 `api/keys.json`：网关 API 配置与调用密钥；
- `providers/<id>/config.json` 和 `secrets.json`：厂商 API 配置与密钥；
- `core/live_control.json`：最高权限系统提示词、Provider/模型启停。

以上变化对后续新请求生效，不中断在途请求。其他所有修改均需重启，包括任何环境变量、
Provider/核心/API/Web 代码、协议模型、依赖以及新增厂商目录。

## 架构原则

每个 `providers/<provider_id>/` 是完整的厂商包，独立负责厂商鉴权、协议、模型能力、流解析、
错误映射和 Token/媒体 Usage 解释。网关核心只调用 `ProviderPackage`，不解析厂商原始字段。

厂商包输出标准化结果和无传输信封事件；核心统一负责 HTTP、SSE sequence/event_id、幂等、
查询、取消和恢复。

## 首次部署

```powershell
python setup.py --install-dependencies --init-env
python start_web.py
```

管理网页默认位于 `http://127.0.0.1:8741/admin`。`start_web.py` 从项目根目录 `.env` 读取
`HOST`、`PORT`、`LOG_LEVEL`、`WEB_ACCESS_LOG` 和 `WEB_OPEN_BROWSER`，已有进程环境变量优先。
上述环境变量都只在启动时读取，修改后必须重启。

未配置任何带 `admin:web`/`owner` scope 的启动或热加载管理 Token，且 `WEB_USERNAME`、
`WEB_PASSWORD` 均为空时，管理网页会直接进入免登录 owner 模式；普通模型调用 Key 不影响该
判定，公开 LLM/Asset API 也不会因此取消鉴权。非可信网络部署必须配置管理 Token。

## 平滑重启

由 `start_web.py` 启动的实例可以执行：

```powershell
python restart.py --reason "environment updated"
python restart.py --status
```

重启会先完成启动前检查，然后进入 Drain：拒绝新的模型创建请求，但保留已有 Response 的查询、
取消以及管理端访问。默认等待活动执行归零；Drain 超时会取消重启并恢复服务。只有显式传入
`--force` 才允许在超时后继续。新实例必须在同一端口通过 `/healthz` 后才记为成功。

创建或修改 Provider 前先阅读 [agent_control.md](agent_control.md)。公开接口见 [api.md](api.md)。

## 当前状态

Provider Package 骨架、模型路由、非流式/流式执行、内存幂等与恢复、认证和模型接口已搭建。
四类运行时控制配置已支持最后有效版本热刷新。Web 管理端已提供 React 控制台和受保护的
`/admin/api/*` 私有管理接口。真实厂商包、生产持久化、完整 Asset API 与 Provider State 服务
仍需继续实现。
