# Kemo Provider Gateway

<p align="center">
  <img src="kemo-adapter-api.png" alt="Kemo Gateway Logo" width="200">
</p>

<p align="center">
  <strong>面向智能体与知识图谱的统一多厂商模型协议网关</strong>
</p>

<p align="center">
  将不同厂商的请求、流式事件、工具调用、能力声明、错误和 Token 计量，<br>
  转换为稳定的 Kemo Provider 协议。
</p>

<p align="center">
  <strong>简体中文</strong> · <a href="README.en.md">English</a>
</p>

<p align="center">
  <a href="https://github.com/kesepain-KE/kemo-adapter-api"><img src="https://img.shields.io/badge/gateway-0.6.0-blue" alt="Gateway version 0.6.0"></a>
  <img src="https://img.shields.io/badge/Kemo%20Protocol-1.0-7c5cff" alt="Kemo Protocol 1.0">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776ab" alt="Python 3.11+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache License 2.0"></a>
</p>

---

## 项目定位

Kemo Gateway 位于智能体框架、知识图谱与模型厂商之间。上层只面对一套稳定协议，每个厂商的
私有差异则封装在独立的 `providers/<provider_id>/` 包内。

```text
kemo-agent / kemo-graph / 其他 Kemo 客户端
                       │
                       ▼
              Kemo Provider Protocol
                       │
                       ▼
        Provider Registry + Gateway Core
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
     Provider A   Provider B   Provider C
```

新增厂商时不应修改核心代码中的厂商分支，而是创建一个新的 Provider Package。厂商自己的鉴权、
请求格式、流解析、工具调用、错误映射、Token 语义和可达性探测都留在该目录中。

## 核心能力

- **Provider 隔离**：每个厂商独立封装协议、配置、密钥、能力、Usage、错误和探测逻辑。
- **统一模型调用**：支持 LLM 非流式、SSE 流式、多轮输入、推理档位和工具调用。
- **面向 kemo-graph**：提供独立的 Embedding 与 Rerank 请求/响应协议。
- **模型发现**：返回当前密钥实际可调用的模型，而不是未经鉴权的全量注册表。
- **能力声明**：提供模型任务、模态、工具、流式、推理和任务专属限制。
- **密钥白名单**：每个网关调用密钥可单独允许全部、禁止全部或仅允许指定模型。
- **运行时控制**：厂商配置、调用密钥、最高权限提示词和 Provider/模型启停支持热更新。
- **独立模型探测**：测试协议下沉到 Provider，核心只消费统一 `ProviderProbeResult`。
- **管理控制台**：提供 Provider、模型、密钥、统计、调用日志、版本和重启管理页面。
- **只读全局感知**：`GET /status` 使用独立 `STATUS_TOKEN`，供外部智能体读取脱敏状态。

## 公开 API

公开接口只提供模型、检索任务和只读状态，不暴露 Web 管理 API：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /model/models` | 获取当前密钥真正可调用的 Kemo 模型目录 |
| `GET /model/models/{model}/capabilities` | 获取指定模型的真实能力声明 |
| `GET /v1/models` | 兼容常见客户端的模型发现入口 |
| `POST /model/responses` | 创建 LLM 非流式或 SSE 流式响应 |
| `GET /model/responses/{response_id}` | 查询响应 |
| `POST /model/responses/{response_id}/cancel` | 取消响应 |
| `POST /model/embeddings` | 批量生成查询或文档向量 |
| `POST /model/rerank` | 对候选文档重排序 |
| `GET /status` | 外部智能体只读网关状态快照 |

`GET /v1/models` 只用于模型发现兼容。网关不提供 `/v1/chat/completions` 或
`/chat/completions`；模型调用必须使用 Kemo 协议的 `POST /model/responses`。

完整请求字段、鉴权、SSE、幂等、Embedding、Rerank 和错误契约见 [api.md](api.md)。

## 模型命名

所有公开模型名固定为：

```text
<provider_id>-<厂商内部模型名>
```

例如 Provider 目录名和 ID 为 `deepseek`，上游模型名为 `deepseek-v4-flash`，对外名称就是：

```text
deepseek-deepseek-v4-flash
```

厂商内部模型名可以继续包含连字符。网关通过注册表保存精确归属，不按任意连字符猜测厂商，
也不接受旧式 `provider_id/model` 命名。

## 快速开始

### 环境要求

- Python 3.11 或更高版本
- Node.js
- pnpm

### 1. 初始化

在项目根目录执行：

```powershell
python setup.py --install-dependencies --build-frontend --init-env
```

该命令安装 Python 依赖、构建管理网页，并在 `.env` 不存在时从 `.env.example` 创建它；不会
覆盖现有 `.env`。

### 2. 配置调用密钥

正式部署建议从样例创建热加载密钥文件：

```powershell
Copy-Item api/keys.json.example api/keys.json
```

然后替换样例 Token，并按需要设置 `scopes` 与 `allowed_models`。`allowed_models: null` 表示允许
全部模型，空数组表示全部禁止，非空数组表示模型白名单。真实 `api/keys.json` 已被 Git 忽略。

首次启动或应急场景也可以在 `.env` 中配置单个 `GATEWAY_API_KEY`。环境变量只在启动时读取，
修改后必须重启。

### 3. 安装 Provider

仓库默认不提交部署端的真实 `providers/*` 厂商包。可以从 `template/provider/` 创建本地厂商包，
也可以让智能体按照 [agent_control.md](agent_control.md) 与 [ADD_DIY/README.md](ADD_DIY/README.md)
完成创建和验证。

新增 Provider 目录、修改 Python、模型清单或依赖后必须重启。已有 Provider 的 `config.json` 和
`secrets.json` 可以热更新。

### 4. 启动

```powershell
python start_web.py
```

默认地址：

- Web 管理端：`http://127.0.0.1:7531/admin`
- Kemo 模型目录：`http://127.0.0.1:7531/model/models`
- OpenAPI：`http://127.0.0.1:7531/docs`

`HOST`、`PORT`、`LOG_LEVEL` 和公开展示用的 `GATEWAY_BASE_URL` 从 `.env` 读取。若网关通过
反向代理或域名对外发布，请把 `GATEWAY_BASE_URL` 设置为外部访问地址；它只用于控制台展示和
复制，不会改变监听地址或路由。

## 鉴权边界

网关使用三类相互独立的凭据：

| 凭据 | 用途 | 配置位置 |
| --- | --- | --- |
| 网关调用密钥 | 调用模型、Embedding、Rerank、Asset | `api/keys.json` 或启动配置 |
| Web 管理凭据 | 访问 `/admin` 和受保护管理接口 | `.env` 中的 `WEB_TOKEN`、用户名和密码 |
| 状态 Token | 只读访问 `GET /status` | `.env` 中的 `STATUS_TOKEN` |

同时配置 Web Token 和用户名/密码时，必须先通过 Token，再通过用户名和密码；两个会话阶段的
有效期均为两小时。三项 Web 凭据全部为空时，管理网页进入免登录 owner 模式，因此非可信网络
部署必须主动配置管理凭据。

`STATUS_TOKEN` 必须独立于模型调用密钥和 Web Token。状态接口不会返回网关密钥原文、Provider
密钥、请求正文、原始厂商响应或堆栈。

## 热更新与重启

| 变更 | 是否需要重启 |
| --- | --- |
| `api/runtime.json`、`api/keys.json` | 否 |
| Provider `config.json`、`secrets.json` | 否 |
| 最高权限系统提示词、Provider/模型启停 | 否 |
| `.env` 环境变量 | 是 |
| Python、Provider 清单、依赖、协议模型 | 是 |
| 新增或删除 Provider 目录 | 是 |
| Web 前端源码或构建产物 | 是 |

平滑重启：

```powershell
python restart.py --reason "update configuration"
python restart.py --status
```

重启模块会先进入 Drain，等待在途请求结束。管理控制台也提供二次确认、耗时反馈和状态轮询。

## Provider 开发

权威模板只有 `template/provider/`。Provider 至少需要明确实现或声明：

- Kemo 请求到厂商请求的映射；
- 非流式结果和流式事件转换；
- 工具调用及并行工具调用；
- Token、缓存 Token、推理 Token和其他媒体单位的真实语义；
- 统一且脱敏的错误映射；
- 每个模型的真实能力声明；
- 本厂商自己的最小可达性探测；
- 脱敏 Golden Fixture 和契约测试。

不得根据营销页猜测能力，也不得让核心补齐厂商缺失的 Usage。完整流程见：

- [Agent 操作总索引](agent_control.md)
- [Agent DIY 起点](ADD_DIY/README.md)
- [Provider 创建规范](ADD_DIY/provider-package.md)
- [发布验证清单](ADD_DIY/verification.md)
- [Provider 模板说明](template/provider/README.md)

## 项目结构

```text
api/                    公开 API、认证、路由与 SSE
core/                   执行核心、统一模型、注册表与运行时控制
providers/              部署本地的厂商包（默认不提交）
storage/                每日 SQLite 统计与脱敏调用日志
template/provider/      唯一 Provider 创建模板
ADD_DIY/                智能体创建和修改网关的操作手册
web/backend/            FastAPI 私有管理 API
web/frontend/           React 19 + TypeScript + Vite 控制台
tests/                  网关与 Provider 边界测试
agent_control.md        智能体操作总索引
api.md                  公开 API 完整说明
start_web.py            网关与管理端启动入口
restart.py              平滑重启模块
update.py               更新模块
version.json            网关和 Kemo 协议版本
```

## 开发验证

```powershell
python -m compileall core api web/backend template/provider
python -m pytest -q
Set-Location web/frontend
pnpm run build
```

发布前还应执行 `git diff --check`，并确认没有提交 `.env`、`api/keys.json`、Provider 密钥、运行
数据库、日志、缓存或真实厂商响应。

## 当前状态

当前网关版本为 `0.6.0`，Kemo 协议版本为 `1.0`。已具备 LLM、Embedding、Rerank、模型发现、
能力声明、调用统计、调用日志、密钥模型白名单、管理控制台、只读状态感知和 Provider 创建模板。

仍在推进：更多正式厂商包、跨进程执行状态与响应持久化、完整 Asset API，以及 Provider State
服务与流恢复。

## 相关项目

- [kemo-agent](https://github.com/kesepain-KE/kemo-agent)：Kemo Gateway 的主要智能体调用方。
- `kemo-graph`：面向知识图谱的 Embedding 与 Rerank 使用方。
- [votx-agent](https://github.com/kesepain-KE/votx-agent)：独立维护的 Agent 项目。

## 维护者与许可证

维护者：[@kesepain](https://github.com/kesepain-KE)

本项目使用 [Apache License 2.0](LICENSE)。
