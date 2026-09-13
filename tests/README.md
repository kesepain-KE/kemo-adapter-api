# 网关测试：一个入口，按功能选择

在网关根目录执行：

```sh
python -m tests
```

不带参数运行全部**源码测试和 Provider 模板测试**。Windows PowerShell 与 Linux 使用相同命令。
使用网关自己的 Python 环境；不自动安装依赖、不修改实际密钥、不重启正在运行的网关。

不在根目录时，也可执行 `python /网关绝对路径/tests`。例如 Windows：

```powershell
& D:\Anaconda3\envs\kemo\python.exe E:\code\kemo-adapter-api\tests --suite templates -q
```

路径只是示例，请使用本机实际的解释器和项目路径。

## 常用命令

```sh
python -m tests --list
python -m tests -q
python -m tests --suite kemo-contract -q
python -m tests.contracts.kemo_v1 --peer-root E:\code\kemo-agent -q
python -m tests --suite templates -q
python -m tests --suite protocol --suite providers -q
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
│  └─ retrieval.py   模拟检索 Provider 与请求
├─ contracts/        跨项目公开契约；kemo_v1 是与 kemo-agent 镜像的线协议 Fixture
├─ api/              公开 API、模型目录、资产、检索、状态
├─ web/              管理端 API、只读配置
├─ runtime/          启动、热配置、重启控制、状态检测
├─ protocol/         能力、工具、多模态、传输
├─ providers/        模拟 Provider 合同、密钥池路由
├─ storage/          执行存储、统计
├─ maintenance/      安装、版本、更新及临时 Git 仓库
├─ templates/        模板与操作配方
├─ architecture/     入口、收集覆盖、依赖方向门禁
├─ integration/      可选真实进程替换
└─ local_providers/  可选本地厂商适配器
```

`tests/providers/` 使用模拟 Provider，不等于根目录的 `providers/` 厂商包。

## Kemo 1.0 共享契约

`tests/contracts/kemo_v1` 是网关与 kemo-agent 共用的离线线协议基准。两个仓库只镜像脱敏的
`manifest.json` 和 `wire.json`，各自使用本项目的公开模型与传输代码验证；测试不会跨仓库导入
生产模块，也不会访问真实 Provider、读取 `.env` 或修改用户数据。

只验证当前仓库：

```sh
python -m tests --suite kemo-contract -q
```

同时检出 kemo-agent 时，再核对两个 Fixture 是否逐字节一致：

```powershell
python -m tests.contracts.kemo_v1 --peer-root E:\code\kemo-agent -q
```

Linux 或不同目录下把 `--peer-root` 改成实际路径。协议模型、SSE、Asset、工具、多模态、Usage、
Embedding、Rerank 或能力声明发生变化时，必须先通过此门禁，再运行对应领域回归。

## 两类可选测试

### 本机未发布厂商

```sh
python -m tests --suite local-providers -q
```

此组会加载 `tests/local_providers/` 与根目录 `providers/` 中的测试。运行前必须确认这些本地
厂商测试可信、使用脱敏 Fixture，且没有未经授权的真实调用；入口不能保证第三方测试不访问上游。
仓库不发布这些厂商，因此默认源码测试不依赖它们。缺少旧厂商时相应适配器用例会明确 skipped。

### 真实进程替换

```sh
python -m tests --suite restart-e2e -q
```

必须先构建前端。这一组显式启用 `KEMO_RUN_RESTART_E2E=1`，在 pytest 临时目录启动和替换进程，
不操作用户当前网关。其他套件不会因环境中残留该变量就执行进程替换。
GitHub 的网页重启工作流使用同一入口，并先构建前端。

## 直接运行单个用例仍然可用

```sh
python -m pytest tests/web/test_gateway_config.py -q
python -m pytest tests/providers/test_provider_boundary.py -q -k registry
```

`pytest.ini` 的默认收集范围与 `python -m tests` 保持一致；架构测试会检查二者是否漂移。
明确运行 `python -m pytest .` 会递归包含本地厂商测试，不作为推荐默认命令。
新增厂商自身的 `providers/<provider_id>/test_contract.py` 仍可按路径单独运行。

## 新增或修改测试的规则

1. 测试放进对应功能目录；不要重新把所有文件堆在 tests 根目录。
2. 单文件专用 helper 留在本文件。被多组复用时提到 support 对应模块，不造全局大夹具。
3. 用例只依赖生产模块和 support，**禁止从其他 test_*.py 导入**。
4. support 不依赖测试模块、pytest、runner 或 suites；构造器接收 tmp_path，不能在 import 时建库、启动服务或读取真实配置。
5. 路径使用 `tests.support.paths.PROJECT_ROOT`，不要按各个文件层级分别推算根目录。
6. 添加新功能目录时同步 suites.py 与 pytest.ini，运行 architecture 分组；普通目录内新增 test_*.py 自动收集。
7. 不删除断言、缩减参数化、降低校验或无理由 skip 来通过重构验证。
8. 修改 Kemo 线路对象时，两边 Fixture、清单摘要和固定摘要必须同步；禁止只修改一侧或用放宽模型掩盖回归。

重构后先 `--collect-only` 核对，再运行实际测试。源码、本地厂商和进程测试分别报告数量，
不要把可选测试未运行说成通过。
