# 发布配方：版本、说明、验证、交接

仅适用于用户明确要求准备发布。不要替用户推送；修改文件、暂存、提交、推送是不同操作。
先读 `agent_control.md`，确认源代码目录，不覆盖部署版或现有用户配置。

## 1. 确认版本来源

用户指定的目标版本是本次应用版本。同步以下位置，不全仓库机械替换历史版本：

| 位置 | 应同步什么 |
| --- | --- |
| `version.json` | version、与本次变更一致的 notes |
| `web/frontend/package.json` | 前端包 version 与根版本一致 |
| `README.md`、`README.en.md` | 徽章、alt 文字、当前版本介绍、中英文语义一致 |
| `agent_control.md` | 已核对版本说明与实际操作入口 |

`protocol_version` 是单独的公开协议版本。UI、文档、缓存与测试结构升级不自动变成 Kemo 2.0；
修改它必须有协议变更授权、合同及客户端迁移验证。测试中模拟旧版本的 Fixture 不随发布号替换。
不要把未执行的上游验证、Windows/Linux 验证或性能压测写入更新说明。

## 2. 核对引导，不只是改标题

- `ADD_DIY/tasks.md` 能将目标正确分到创建、模型维护、多模态、密钥或发布；
- 创建只复制新目录，已有厂商增量修改；密钥保持原位置，不能被掩码替换；
- 模型集合、capabilities、manifest、协议映射与测试一致；未验证能力不虚构为支持；
- 使用 `python -m tests` 和功能分组，不从别的测试文件导入；
- 统计读缓存与 Token 缓存计量分开，不能宣传为延迟落盘或零丢失；
- Web 认证、掩码、热更新与重启说明和代码一致；
- 相对链接、示例 JSON、离线请求可以通过仓库测试。

## 3. 按顺序验证

在项目根目录、使用本项目 Python 环境执行：

```sh
python -m tests --suite version --suite templates -q
python -m tests -q
pnpm --dir web/frontend run build
python -m tests --suite restart-e2e -q
python -m compileall -q api core storage web/backend template tests update setup.py start_web.py restart.py
git diff --check
```

真实重启测试只使用临时项目、假凭据与空厂商，不操作运行中的用户网关。
前端依赖缺失时先按项目部署流程安装锁定依赖，不能靠跳过构建报告成功。

本机存在可信、使用脱敏 Fixture 的未发布厂商测试时，另外执行：

```sh
python -m tests --suite local-providers -q
```

先检查本地厂商测试是否会真实联网计费；这条命令本身不授予付费调用权。
源码、厂商、真实重启分别报告数量；不能把 skipped、未运行和通过相加。
更新测试中的本地临时 Git 仓库提交不等于授权提交当前源码仓库。

## 4. 交给用户提交

查看 `git status --short`。目录迁移会先显示旧文件删除、新目录未跟踪；
用户提交时必须同时纳入新文件，不能只提交旧文件的删除记录。
检查 `.env`、`api/keys.json`、真实厂商包、运行数据和 `开发目录/` 仍被忽略，不扩大发布范围。
构建产物 `web/frontend/dist/` 仍由部署时构建，不强行加入 Git。

最终报告：目标版本和协议版本、变更范围、各组测试、前端构建、是否需要重启、
未验证项目，以及是否已暂存/提交/推送。用户保留远程提交权时，到此停止。
