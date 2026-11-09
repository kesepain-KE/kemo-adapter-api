# 网关测试：一个入口，按功能选择

在网关根目录执行：

```sh
python -m tests
```

不带参数运行全部**源码测试**。Windows PowerShell 与 Linux 使用相同命令。
使用网关自己的 Python 环境；不自动安装依赖、不修改实际密钥、不重启正在运行的网关。

不在根目录时，也可执行 `python /网关绝对路径/tests`。例如 Windows：

```powershell
& D:\Anaconda3\envs\kemo\python.exe E:\code\kemo-adapter-api\tests --suite gateway -q
```

路径只是示例，请使用本机实际的解释器和项目路径。

## 常用命令

```sh
python -m tests --list
python -m tests -q
python -m tests --suite gateway -q
python -m tests --suite agent-control -q
python -m tests --suite kemo-contract -q
python -m tests --suite protocol --suite storage -q
python -m tests --suite web -q
python -m tests --suite update -q
python -m tests --suite version -q
python -m tests --suite protocol -q -k tool
python -m tests --collect-only -q
```

`--suite` 可重复，交叉套件的文件会去重。`-q`、`-k`、`-x`、`--collect-only` 等参数原样交给 pytest。
pytest 失败、参数错误或零用例时，入口保留非零退出码；Ctrl+C 不会被报告成成功。

## 目录分工

```text
tests/
├─ __main__.py       唯一入口转发，不放用例
├─ runner.py         参数解析、启动 pytest、传递退出码
├─ suites.py         套件与路径声明，不导入测试
├─ support/          无全局运行副作用的共享构造器
│  ├─ paths.py       项目根路径
│  ├─ project.py     临时配置文件和基础项目
│  ├─ admin.py       管理测试身份与临时项目
│  ├─ llm.py         模拟文本 Provider 与请求
│  ├─ retrieval.py   模拟检索 Provider 与请求
│  └─ gateway.py     将协议 Fixture 接入声明式目录、路由与代际
├─ gateway/          新目录、路由、密钥限额、适配器、扩展 worker
├─ contracts/        跨项目公开契约；kemo_v2 验证固定的 Kemo 2.0 制品
├─ api/              公开 API、模型目录、资产、检索、状态
├─ web/              管理端 API、只读配置
├─ runtime/          启动、运行策略、重启控制、状态检测
├─ protocol/         能力、工具、多模态、传输、执行边界
├─ storage/          执行存储、统计
├─ maintenance/      安装、版本、更新及临时 Git 仓库
├─ architecture/     入口、收集覆盖、依赖方向门禁
└─ integration/      可选真实进程替换
```

`tests/gateway/` 使用模拟 HTTP 上游验证实际协议适配器与声明式运行时。
执行边界回归位于 `tests/protocol/test_execution_boundary.py`，通过同一运行时验证用量、
SSE 唯一终态、幂等重放、错误脱敏、超时与持久化。旧厂商包、密钥路由与模板测试已移除。

## 智能体操作回归

`agent-control` 套件对应 [智能体操作指南](../agent/_doc/README.md)，复用现有管理、模型、密钥和启动配置测试，并补充智能体连续操作流程：

- 两阶段管理登录与 CSRF、提供商创建、只读发现、显式能力登记、调用方模型选择。
- 调用密钥的模型白名单、操作范围和启停，以及模型重命名后的权限同步。
- 上游密钥池追加、替换、移除、无关编辑保留密文，以及重新打开后的持久化。
- 陈旧 revision 拒绝修改上游密钥或重复创建下游密钥；管理身份与调用身份的权限边界。

这些测试经过真实应用路由和临时数据库，上游由 MockTransport 隔离，显式声明登记不产生生成或能力探测。版本套件同时核对固定的 2.0.0、前端元数据、介绍徽章及应用/控制台实际返回值。

## Kemo 2.0 固定制品契约

`tests/contracts/kemo_v2` 验证 `vendor/kemo-protocol.lock.json` 锁定的协议制品：完整制品和
Schema 的 SHA-256、Fixture 摘要、协议版本，以及运行时模型确实从固定制品加载。
测试不会跨仓库导入生产模块，也不会访问真实 Provider、读取 `gateway.yaml` 或修改用户数据。

只验证当前仓库：

```sh
python -m tests --suite kemo-contract -q
```

只有明确修改公开协议时，才在 kemo-agent 重新生成
`provider/protocol/spec/{schema,model-index,freeze,fixture-manifest}.json`，
使用 `python -m provider.protocol.spec.build_artifact <网关仓库>/vendor` 更新制品和 lock 摘要，
再在两个仓库各自的根目录运行：

```sh
python -m pytest tests/contracts/kemo_v2 -q
```

协议模型、SSE、Asset、工具、多模态、Usage、Embedding、Rerank 或能力声明发生变化时，
必须先通过此门禁，再运行对应领域回归。

## 可选真实进程替换

```sh
python -m tests --suite restart-e2e -q
```

必须先构建前端。这一组显式启用 `KEMO_RUN_RESTART_E2E=1`，在 pytest 临时目录启动和替换进程，
不操作用户当前网关。其他套件不会因环境中残留该变量就执行进程替换。
GitHub 的网页重启工作流使用同一入口，并先构建前端。

## 直接运行单个用例仍然可用

```sh
python -m pytest tests/web/test_gateway_config.py -q
python -m pytest tests/protocol/test_execution_boundary.py -q
```

`pytest.ini` 的默认收集范围与 `python -m tests` 保持一致；架构测试会检查二者是否漂移。
新增协议 Adapter 或声明式模板的回归放在 `tests/gateway/`，测试实际安装、校验和调用路径。

## 协议层重构回归

```sh
python -B -m pytest tests/gateway/test_adapter_contract.py tests/gateway/test_connection_transport.py tests/gateway/test_worker_concurrency.py tests/gateway/test_adapter_events.py tests/gateway/test_provider_presets.py -q
```

这些回归覆盖三层能力边界、鉴权与 query 密钥脱敏、参数冲突、代理隔离、发现错误不回退、旧连接转换、worker 结构化错误及独立取消、非 OpenAI 示例 ZIP 的真实本地 HTTP 接入，以及四种原生流的工具增量、身份、phase 与拒绝信息。损坏 JSON 或不满足工具 Schema 时，增量仍可见，但不发布工具完成事件。测试使用临时数据，不证明 46 个官方预制已持钥实测。

## 新增或修改测试的规则

1. 测试放进对应功能目录；不要重新把所有文件堆在 tests 根目录。
2. 单文件专用 helper 留在本文件。被多组复用时提到 support 对应模块，不造全局大夹具。
3. 用例只依赖生产模块和 support，**禁止从其他 test_*.py 导入**。
4. support 不依赖测试模块、pytest、runner 或 suites；构造器接收 tmp_path，不能在 import 时建库、启动服务或读取真实配置。
5. 路径使用 `tests.support.paths.PROJECT_ROOT`，不要按各个文件层级分别推算根目录。
6. 添加新功能目录时同步 suites.py 与 pytest.ini，运行 architecture 分组；普通目录内新增 test_*.py 自动收集。
7. 不删除断言、缩减参数化、降低校验或无理由 skip 来通过重构验证。
8. 修改 Kemo 线路对象时，重新生成固定制品并同步 lock 摘要，两仓契约测试都必须通过；禁止只修改一侧或用放宽模型掩盖回归。

重构后先 `--collect-only` 核对，再运行实际测试。源码和进程测试分别报告数量，
不要把可选测试未运行说成通过。
