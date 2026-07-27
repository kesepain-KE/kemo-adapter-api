# Kemo Provider Gateway

<p align="center">
  <img src="kemo-adapter-api.png" alt="Kemo Gateway Logo" width="200">
</p>

<p align="center">
  <strong>简体中文</strong> · <a href="README.en.md">English</a>
</p>

<p align="center">
  <strong>面向智能体与知识图谱的统一多厂商模型协议网关。</strong>
</p>

<p align="center">
  将不同厂商的请求格式、流式事件、工具调用、能力声明、错误处理和 Token 计量，<br>
  转换为稳定的 Kemo Provider 协议。
</p>

<p align="center">
  <a href="https://github.com/kesepain-KE/kemo-adapter-api"><img src="https://img.shields.io/badge/gateway-0.6.1-blue" alt="Gateway version 0.6.1"></a>
  <img src="https://img.shields.io/badge/Kemo%20Protocol-1.0-7c5cff" alt="Kemo Protocol 1.0">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776ab" alt="Python 3.11+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache License 2.0"></a>
</p>

---

## 如果每个厂商都在发明自己的协议

模型厂商越来越多。请求格式、流式事件、工具调用、能力声明、错误和 Token 计量——每一层都有各自的理解。

如果智能体直接对接厂商，每接入一个新模型就要重写一遍协议适配。更麻烦的是，厂商的鉴权方式、密钥管理和可达性探测也各不相同，这些差异会散落在智能体的各个角落，难以维护。

Kemo Gateway 想解决这件事。

它不是又一个模型聚合接口。它做的事情更接近翻译层：把智能体用一套稳定协议表达的需求，转译给每个厂商各自的接口语言；再把厂商的响应，转译回智能体熟悉的格式。

厂商特化逻辑留在独立的 `providers/<provider_id>/` 包内，核心代码中没有厂商名称分支。新增厂商时，不需要改核心。

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

---

## 它可以帮你做什么

| 场景 | 网关带来的价值 |
|------|---------------|
| 多厂商统一调用 | 上层只面对一套 Kemo 协议，厂商差异由各自的 Provider Package 隔离 |
| 模型发现与能力查询 | 返回当前密钥实际可调用的模型及其真实能力，不是未经鉴权的全量注册表 |
| Embedding 与 Rerank | 为知识图谱场景提供独立的向量化和重排序协议，契约与 LLM 调用同级 |
| 密钥白名单控制 | 每个网关调用密钥可单独允许全部、禁止全部或仅允许指定模型 |
| 运行时热更新 | 厂商配置、调用密钥、系统提示词和 Provider/模型启停可在运行中生效 |
| 独立模型探测 | 测试协议下沉到 Provider，核心只消费统一的探测结果 |
| 管理控制台 | 在网页中管理 Provider、模型、密钥、统计、调用日志、版本和重启 |
| 智能体状态感知 | `GET /status` 使用独立状态 Token，供外部智能体读取脱敏的网关快照 |

这些能力不是分散的功能入口。它们共同服务于同一个目标：让上层智能体可以专注于理解用户，而不必关心背后是哪个厂商以什么方式提供服务。

---

## 公开 API

公共接口只提供模型、检索和只读状态，不暴露管理 API：

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

`GET /v1/models` 只用于模型发现兼容。网关不提供 `/v1/chat/completions` 或 `/chat/completions`；模型调用必须使用 Kemo 协议的 `POST /model/responses`。

完整请求字段、鉴权、SSE、幂等、Embedding、Rerank 和错误契约见 [api.md](api.md)。

### 模型命名

所有公开模型名固定为 `<provider_id>-<厂商内部模型名>`，例如 `deepseek-deepseek-v4-flash`。网关通过注册表保存精确归属，不按任意连字符猜测厂商，也不接受旧式 `provider_id/model` 命名。

---

## 快速开始

### 环境要求

- Python 3.11 或更高版本
- 可访问 Python 与前端依赖源的网络

部署模块会自动安装 Python 依赖；未安装 pnpm 时会通过 npm 使用锁定版本。Windows 或 Linux
缺少 Node.js 时，会从 Node.js 官方发布源下载 LTS、校验 SHA-256，并安装到项目本地的
`web/frontend/.runtime/`，不需要管理员权限，也不会修改系统级 Node.js。

### 1. 初始化

```powershell
python setup.py
```

无参数运行即执行完整部署：安装 Python 依赖、重新构建管理网页，并在 `.env` 不存在时从
`.env.example` 创建它；不会覆盖现有的 `.env`。只检查现有部署而不安装或构建时，使用
`python setup.py --check`。

### 2. 配置调用密钥

正式部署建议从样例创建热加载密钥文件：

```powershell
Copy-Item api/keys.json.example api/keys.json
```

然后替换样例 Token，设置 `scopes` 与 `allowed_models`。`allowed_models: null` 表示允许全部模型，空数组表示全部禁止，非空数组是模型白名单。真实的 `api/keys.json` 已被 Git 忽略。

首次启动或应急场景也可以在 `.env` 中配置单个 `GATEWAY_API_KEY`。环境变量只在启动时读取，修改后必须重启。

### 3. 安装 Provider

仓库默认不提交部署端的真实 `providers/*` 厂商包。可以从 `template/provider/` 创建本地厂商包，也可以让智能体按照 [agent_control.md](agent_control.md) 与 [ADD_DIY/README.md](ADD_DIY/README.md) 完成创建和验证。

新增 Provider 目录、修改 Python、模型清单或依赖后必须重启。已有 Provider 的 `config.json` 和 `secrets.json` 可以热更新。

### 4. 启动

```powershell
python start_web.py
```

默认地址：

- Web 管理端：`http://127.0.0.1:7531/admin`
- Kemo 模型目录：`http://127.0.0.1:7531/model/models`
- OpenAPI：`http://127.0.0.1:7531/docs`

`HOST`、`PORT`、`LOG_LEVEL` 和公开展示用的 `GATEWAY_BASE_URL` 从 `.env` 读取。若网关通过反向代理或域名对外发布，请把 `GATEWAY_BASE_URL` 设置为外部访问地址；它只用于控制台展示和复制，不会改变监听地址或路由。

---

## 鉴权边界

网关使用三类相互独立的凭据：

| 凭据 | 用途 | 配置位置 |
| --- | --- | --- |
| 网关调用密钥 | 调用模型、Embedding、Rerank | `api/keys.json` 或启动配置 |
| Web 管理凭据 | 访问管理端和受保护管理接口 | `.env` 中的 `WEB_TOKEN`、用户名和密码 |
| 状态 Token | 只读访问 `GET /status` | `.env` 中的 `STATUS_TOKEN` |

同时配置 Web Token 和用户名/密码时，必须先通过 Token，再通过用户名和密码；两个会话阶段的有效期均为两小时。三项 Web 凭据全部为空时，管理网页进入免登录 owner 模式，因此非可信网络部署必须主动配置管理凭据。

`STATUS_TOKEN` 必须独立于模型调用密钥和 Web Token。状态接口不会返回网关密钥原文、Provider 密钥、请求正文、原始厂商响应或堆栈。

---

## 连接 kemo-agent 状态拓展

kemo-agent `v0.6.0` 起内置 `global_expand/kemo_gateway_status/`。该拓展默认未激活，只在用户明确授权后使用独立状态 Token 读取 `GET /status`，不会调用重启、密钥管理或 Provider 配置等管理接口。

### 1. 在网关配置状态 Token

在网关 `.env` 中设置一个新的独立 Token：

```dotenv
STATUS_TOKEN=replace-with-a-dedicated-random-token
```

环境变量只在启动时读取，因此修改后必须重启网关。该值不能与 `WEB_TOKEN`、`GATEWAY_API_KEY` 或 `api/keys.json` 中的任一模型调用密钥相同，否则 `/status` 会拒绝启用。

### 2. 让主智能体激活拓展

向 kemo-agent 明确提出“激活 Kemo 网关状态拓展”，并提供网关根地址与刚才配置的状态 Token。主智能体会通过统一拓展工具执行等价调用：

```text
expand_call(
  scope="global",
  module="kemo_gateway_status",
  command="activate",
  params={
    "base_url": "http://127.0.0.1:7531",
    "status_token": "<独立 STATUS_TOKEN>"
  }
)
```

拓展会先验证接口和响应合同，成功后才在 kemo-agent 本地保存配置并打开状态注入。它会生成简短状态摘要、严格白名单过滤的 JSON 快照和 `1600×900` PNG 图表，展示运行阶段、版本、Provider/模型、调用成功率、延迟、缓存命中率及 Token 统计。

如果网关位于反向代理、FRP 或域名之后，`base_url` 必须填写 kemo-agent 实际可访问的外部根地址。状态客户端不会自动跟随 HTTP 重定向，避免把 Token 发送到配置地址之外的主机。

### 3. 刷新、检查或停用

- `refresh`：立即重新采集，也可以指定统计日期；
- `configuration_status`：只查看本地是否已激活，不联网、不返回 Token；
- `deactivate`：删除 kemo-agent 本地凭据、快照和图表，不会关闭或修改网关。

完整状态字段和错误语义见 [api.md](api.md#智能体全局感知接口)。

---

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

### 项目自更新

```powershell
python update.py --check
python update.py --apply
```

执行更新前会在 `.backup/` 创建冷备份；备份失败时不会拉取代码。远端提交如果触碰 `.env`、
API 密钥、Provider、每日统计、运行时或私有开发目录，更新器会拒绝整次更新，避免覆盖部署端数据。
前端发生变化时会复用 `setup.py` 的 Windows/Linux 一站式工具链自动安装依赖并重新构建。

---

## Provider 开发

权威模板只有 `template/provider/`。Provider 至少需要明确实现或声明：

- **注入**：在 `core/provider_contract.py` 的 ProviderPackage 抽象类中实现抽象方法
- **协议**：在 `protocol.py` 中完成 KemoRequest ↔ 厂商请求的映射
- **流式**：在 `streaming.py` 中将厂商 SSE 流转换为无信封的 ProviderEvent
- **能力**：在 `capabilities.py` 中声明厂商支持的任务、模态、工具和推理档位
- **探测**：在 `probe.py` 中实现连通性与模型可达性探测
- **契约验证**：`test_contract.py` 包含实现是否符合接口约定的测试用例

除了 `template/provider/`，没有第二份权威参考。厂商包实现后，建议先通过提供的契约测试再投入使用。

创建流程见 [ADD_DIY/provider-package.md](ADD_DIY/provider-package.md)。

---

## 网关不只是连接

Kemo Gateway 并不试图成为一个包罗万象的网关。

它更希望成为一种稳定的协议桥梁：

- 新增厂商时，不需要改核心代码；
- 切换模型时，不需要改调用方的代码；
- 厂商升级 API 时，只需要更新对应的 Provider Package；
- 密钥和配置可以在运行中变更，不需要中断服务。

上层智能体可以持续使用同一套协议与你交互，厂商的差异、升级和切换，都被挡在这层翻译后面。
