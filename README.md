# Kemo Provider Gateway

<p align="center">
  <img src="kemo-adapter-api.png" alt="Kemo Gateway logo" width="200">
</p>

<p align="center">
  <strong>面向多厂商的统一模型 Provider 协议网关</strong>
</p>

<p align="center">
  Kemo Gateway 为 <a href="https://github.com/kesepain-KE/kemo-agent">kemo-agent</a> 提供统一的 Provider 协议层，<br>
  将不同厂商的请求协议、流式事件、工具调用、能力声明、错误处理和用量计量<br>
  转换为稳定的 Kemo Response/SSE 契约，支持 LLM、Embedding 与 Rerank 三类任务。
</p>

<p align="center">
  内置 Web 管理控制台、每日 SQLite 调用统计，以及使用独立 STATUS_TOKEN 的智能体只读全局感知接口。
</p>

<p align="center">
  <a href="https://github.com/kesepain-KE/kemo-adapter-api"><img src="https://img.shields.io/badge/version-0.5.0-blue" alt="version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="license"></a>
</p>

---

## 如果每次换一个厂商就要重写一遍集成逻辑呢？

接入一个新的 LLM 厂商，通常意味着要处理一套新的 API 协议、不同的流式格式、各异的能力声明方式和五花八门的错误码。

请求结构不同、SSE 事件字段不同、Token 计量口径不同、工具调用的表示方式也不同。这些差异分散在每个集成点里，导致网关核心充斥着厂商特殊判断，新增一个厂商就得动一遍核心代码。

Kemo Gateway 尝试换一种方式。

它不要求厂商迁就某个固定的 API 格式，而是定义一组稳定的 Provider 契约接口。每个厂商的适配逻辑封装在独立的 Provider Package 中，网关核心只调用 `ProviderPackage`，不接触厂商原始字段。

新增厂商 = 新增一个目录，不影响核心。更换厂商 = 切换模型名，不更换调用方式。

---

## 稳定契约，厂商隔离

`providers/<provider_id>/` 是一个完整的厂商包，独立负责：

- **鉴权**：厂商专属的认证方式
- **协议转换**：将厂商请求/响应映射为统一协议
- **能力声明**：暴露模型真实能力（流式、工具、多模态、结构化输出、Embedding、Rerank）
- **流解析**：将厂商 SSE 转换为无传输信封的统一事件
- **错误映射**：将厂商错误转换为标准 ErrorObject
- **用量计量**：Token 与媒体 Usage 的解释

网关核心统一处理 HTTP 路由、SSE sequence/event_id、幂等、查询、取消、恢复和运行时控制，厂商包只负责「翻译」。

### 三层任务协议

除 LLM 响应外，0.4.0 新增了面向检索的 Embedding 与 Rerank 协议：

| 任务 | 端点 | 协议 Object |
|------|------|------------|
| LLM 响应 | `POST /model/responses` | `KemoResponse` / Kemo SSE 事件 |
| 模型能力 | `GET /model/capabilities` | `ModelCapabilities` |
| Embedding | `POST /model/embeddings` | `kemo.embedding_list` |
| Rerank | `POST /model/rerank` | `kemo.rerank` |

三种任务共享相同的 Provider Package 发现机制和运行时控制框架。

---

## 生效策略

无需重启的内容：

- `api/runtime.json` 和 `api/keys.json`：网关 API 配置与调用密钥
- `providers/<id>/config.json` 和 `secrets.json`：厂商 API 配置与密钥
- `core/live_control.json`：最高权限系统提示词、Provider/模型启停

以上变化对后续新请求即时生效，不影响在途请求。其他所有修改均需重启——包括环境变量、Provider/核心/API/Web 代码、协议模型、依赖以及新增厂商目录。

---

## 顶层结构

```text
api/                    对外 API 路由、认证和 SSE
  routes/retrieval.py   Embedding/Rerank 端点
  routes/status.py      智能体只读全局感知端点
  server.py             应用创建与生命周期
  errors.py             统一异常处理
  dependencies.py       依赖注入
core/                   执行核心、统一模型和运行时状态
  models.py             数据模型（LLM + Embedding + Rerank）
  provider_contract.py  Provider 契约接口
  retrieval_executor.py Embedding/Rerank 统一执行器
  executor.py           LLM 统一执行器
  registry.py           Provider 注册表
  runtime_state.py      运行时状态与 Drain
  live_config.py        热配置管理
  stores.py             幂等存储
providers/              部署本地热加载的独立厂商包
storage/                每日 SQLite 调用统计与脱敏调用日志
web/                    网关管理网页（React 控制台）
tests/                  网关及 Provider 契约测试
template/provider/      Provider 包创建模板
ADD_DIY/                厂商创建与管理操作手册
agent_control.md        智能体操纵网关总索引
api.md                  对外模型、Asset 与智能体状态 API 说明
kemo-adapter-api.png    网关 Logo
.env.example            环境变量样例
start_web.py            网关与 Web 管理端启动入口
restart.py              平滑重启模块
update.py               更新模块
version.json            网关与协议版本
```

---

## 快速开始

环境要求：Python 3.11 或更高版本，以及 Node.js 和 pnpm。

```powershell
python setup.py --install-dependencies --build-frontend --init-env
python start_web.py
```

管理网页默认位于 `http://127.0.0.1:7531/admin`。

启动参数从 `.env` 读取 `HOST`、`PORT`、`LOG_LEVEL` 等环境变量，已有进程环境变量优先。上述变量只在启动时读取，修改后必须重启。

### 免登录模式

未配置任何带 `admin:web`/`owner` scope 的启动 Token，且 `WEB_TOKEN`、`WEB_USERNAME`、
`WEB_PASSWORD` 均为空时，管理网页进入免登录 owner 模式。普通模型调用 Key 不影响该判定，
公开模型、Asset 与状态 API 也不会因此取消鉴权。非可信网络部署必须配置管理凭据。

### 平滑重启

```powershell
python restart.py --reason "update configuration"
python restart.py --status
```

重启先完成启动前检查，然后进入 Drain：拒绝新模型创建请求，保留已有 Response 的查询、取消以及管理端访问。默认等待活动执行归零；超时可取消重启并恢复服务。显式传入 `--force` 才允许超时后继续。

### 创建厂商包

Provider 包属于部署本地内容，默认不会提交到网关主仓库。参考 `template/provider/` 创建
`providers/<provider_id>/`，或由智能体按照 [agent_control.md](agent_control.md) 和 `ADD_DIY/`
中的规程生成。新增目录后需要重启，已有 Provider 的 API 配置与密钥可以热更新。

---

## 当前状态

版本：`0.5.0`

已搭建：

- Provider Package 骨架与模型路由
- LLM 非流式/流式执行与统一事件协议
- Embedding/Rerank 统一执行器
- 内存幂等、查询、取消与恢复
- 认证与作用域鉴权
- 独立 `STATUS_TOKEN` 鉴权的智能体只读全局感知接口
- 每日 SQLite 调用统计、Token 排行与脱敏调用日志
- 四类运行时控制配置的热刷新
- Web 管理控制台与受保护管理 API
- Provider 包创建模板

仍在实现中：

- 更多真实厂商包（OpenAI、Anthropic 等）
- 跨进程执行状态与响应持久化
- 完整 Asset API
- Provider State 服务与流恢复

---

## 相关项目

- [kemo-agent](https://github.com/kesepain-KE/kemo-agent)  
  以潮汐式记忆系统为核心的本地多用户 Agent Runtime，Kemo Gateway 的上层调用方。

- [votx-agent](https://github.com/kesepain-KE/votx-agent)  
  独立维护的 Agent 项目，与 kemo-agent 无继承关系。

---

## 维护者

[@kesepain](https://github.com/kesepain-KE)

---

## 许可证

[Apache License 2.0](LICENSE)
