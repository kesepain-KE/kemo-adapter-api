# 配方 A：创建一个厂商

前置：读过 `agent_control.md` 和 [任务导航](tasks.md)，用户要求创建新厂商，而不是给现有厂商加模型。

## 1. 检查目标与材料

在用户确认的项目根目录检查 `template/provider/`、`core/provider_contract.py`、`core/models.py`。
确认目标 `providers/<provider_id>/` 不存在；已存在就转 [模型维护](model-maintenance.md)，不要覆盖。

至少需要：厂商 API 地址、鉴权方式、一个原始模型名、实际请求/响应协议、脱敏样本。
缺少模型列表接口时不要猜 `/v1/models`，也不要把兼容接口当成 Kemo 私有接口。
如果只有地址和密钥，先说明还缺少什么协议证据，不要复制模板后就声称接入完成。

## 2. 复制骨架，不复制缓存

下面命令在 Windows PowerShell 和 Linux Shell 都可以执行。`demo_vendor` 是教学目录名，
必须先替换为用户确认的厂商 ID；必须在网关根目录执行。

```sh
python -c "from pathlib import Path; import shutil; assert Path('agent_control.md').is_file(); shutil.copytree('template/provider', 'providers/_draft_demo_vendor', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache'))"
```

目标已存在时命令会失败。不要加覆盖选项，不要删除旧目录来强行通过。
下划线开头的 `_draft_demo_vendor` 是暂存名，registry 不加载。先在暂存包内实现和测试，
最终完成且正式目录不存在时才改名 `demo_vendor`；正式目录名必须与 provider_id 一致。
将新目录内三个文件分别改名：`config.json.example` → `config.json`、
`secrets.json.example` → `secrets.json`、`manifest.json.example` → `manifest.json`。
`requirements.txt.example` 只有需要相应 SDK 时才改为 `requirements.txt`，不要安装示例依赖。

## 3. 按顺序填写，不要只替换厂商名

| 顺序 | 文件 | 必须完成 | 完成判据 |
| --- | --- | --- | --- |
| 1 | `__init__.py`、`provider.py` | 类名、工厂、provider_id、模型集合 | 工厂可导入，ID 与文件夹一致 |
| 2 | `config.json`、`secrets.json` | 真实地址、超时；密钥仅写 secrets 的 api_keys 数组 | 不留 .invalid，不把密钥写进源码 |
| 3 | `capabilities.py`、`manifest.json` | 同一个完整模型名与保守能力 | `validate_catalog()` 通过 |
| 4 | `client.py`、`protocol.py` | 端点、鉴权、真实 DTO、响应映射 | FakeClient/脱敏 Fixture 通过 |
| 5 | `usage.py`、`errors.py` | 计量含义、状态与脱敏 | 缺失计量保持 None，HTTP 状态不丢失 |
| 6 | `streaming.py` | 仅当支持流式时适配真实事件 | 分片正确、一个终态、工具不丢失 |
| 7 | `probe.py` | 按任务构建最小探测 | 不能把所有媒体操作都用文本 OK 探测 |
| 8 | `test_contract.py` | 保留通用门禁，替换模拟厂商 DTO，补目标用例 | 每个模型、每个公开能力都有用例 |

模板默认只公开文本、非流式能力。示例里存在 stream 方法、工具解析或媒体 helper，
不代表目标上游支持这些能力。`vendor_*` 计量字段、示例 `message` 响应形状、端点均必须重写。
模板 `provider.models` 从 `MODEL_CAPABILITIES` 派生，不必再造第二个模型列表。

只接一个普通文本模型时，不必修改网关核心、Web UI 或其他 Provider。
要接 Embedding/Rerank 时，另行实现 `ProviderPackage.embed/rerank`，不能只修改 task。
要接媒体操作时，继续 [多模态配方](multimodal.md)。

## 4. 离线验证后再上线

以下 `<provider_id>` 是路径占位符，执行前替换，不要原样粘贴：

```sh
python -m compileall -q providers/_draft_<provider_id>
python -m pytest -q providers/_draft_<provider_id>/test_contract.py
```

必须替换 `test_contract.py` 中的 Example 类名、GATEWAY_MODEL、FakeClient 厂商 DTO；
不要删除目录一致性、选项拒绝、密钥和错误边界测试来“让测试通过”。
多个任务并存时，把文本映射测试只用于 LLM 文本模型，额外为 embed/rerank/媒体端点建立用例。

按 [验证清单](verification.md) 执行对应网关回归测试。真实调用须有用户授权；先测试一个模型，
再逐项启用工具、流式、推理或媒体能力。代码与 manifest 更新需要重启。
最后用调用方 Token 查询 `/model/models` 和 `/model/capabilities?model=<完整模型名>`；
目录受调用方 scope、白名单和启停状态过滤，不能只看 owner 网页。

目录暂未完成时不要启动生产网关自动扫描它。测试通过前保留在非 `providers/` 的临时工作目录，
或使用下划线开头的暂存包目录（registry 不加载），交付时才改为正式目录名。
