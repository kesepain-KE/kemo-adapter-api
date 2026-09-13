# 配方 B：增加模型、修改能力与推理档位

前置：已经确认目标 Provider 存在。只改这个包，不重新复制模板，不改其他模型。

## 1. 增加一个模型

1. 记录原始模型名。例如厂商 `demo_vendor`，原始名 `chat-v2-fast`，完整名是
   `demo_vendor-chat-v2-fast`。不能拆开原始名里的连字符，也不能改成 `demo_vendor/chat-v2-fast`。
2. 读取 `provider.py` 的 `models` 属性。若它由 `MODEL_CAPABILITIES` 派生，只增加能力字典条目；
   若是独立集合，同步增加完整模型名，不要重写所有模型。
3. 在 `capabilities.py` 增加**独立的** `ModelCapabilities`。从相近模型复制时必须重新核实
   task、模态、工具、推理、额度和操作；不能共享并原地修改同一个 extensions 字典。
4. 在 `manifest.json.models` 增加相同键。其 `upstream_model` 必须与能力的
   `metadata.upstream_model` 一致；保留其他模型和厂商自定义字段。
5. 检查 `protocol.py` 是否已有这个模型的端点、模型名与参数映射。模板仅移除完整厂商前缀；
   新模型若使用不同 API，必须增加本包内的分派，不能只添加目录项。
6. 添加测试：新旧模型均能路由；新增模型使用自己的上游名字与参数；旧模型行为不变。

可复制的最小声明（示例名称必须替换；不代表上游已验证）：

```python
from core.models import ModelCapabilities, ReasoningCapabilities, ToolCapabilities

new_model = ModelCapabilities(
    model="demo_vendor-chat-v2-fast",
    task="llm",
    input_modalities=["text"],
    output_modalities=["text"],
    streaming=False,
    reasoning=ReasoningCapabilities(supported=False, efforts=[]),
    tools=ToolCapabilities(function_calling=False, parallel_calls=False),
    structured_output=False,
    metadata={"upstream_model": "chat-v2-fast"},
    extensions={
        "operations": {"conversation": {"supported": True}},
        "limits": {},
        "reasoning_effort_map": {},
        "reasoning_policy": {
            "mode": "unsupported", "logical_efforts": [],
            "upstream_parameter": None, "collapsed": False,
        },
    },
)
```

这只是条目构造，不会自动注册。将其加入本包 `MODEL_CAPABILITIES`，再同步 manifest。
新模板内 `catalog_contract.manifest_entry(new_model)` 可生成这一项的静态字段；
只合并这一项，不用生成结果覆盖整个 manifest。不认识的真实额度留空，不能抄模板数字。

## 2. 更新现有模型能力

按这张表逐项核实，声明与转换必须一起完成：

| 本次改动 | 声明字段 | 还必须改/验证 |
| --- | --- | --- |
| 流式 | `streaming` | Client 流、streaming 映射、分片、终态、取消 |
| 工具 | `tools.function_calling` | tools 入站、tool_call 出站、tool_result 下一轮；不执行工具 |
| 并行工具 | `tools.parallel_calls` | 每个 call_id 独立、分片交错、参数完整、终态包含所有调用 |
| 思考 | `reasoning`、reasoning 映射扩展 | 统一字段优先、真实档位、关闭行为、非法档位拒绝 |
| 结构化输出 | `structured_output` | 上游参数映射和真实结构校验，不能只勾选 |
| 图片/音频/视频/文件 | 模态与 `extensions.operations` | [多模态配方](multimodal.md) |
| 上下文或输出额度 | `extensions.limits` | 只写厂商证实的数值，同步 manifest |

修改 `model_dump()` 得到的新字典后，用 `ModelCapabilities.model_validate(data)` 重建，
不要用 `model_copy(update=...)` 绕过新能力的校验。更新完整模型名这个键，不要顺便重命名模型，
否则旧历史、调用方配置和白名单都会受影响。

### Embedding / Rerank 不能按文本模型注册

可运行的配对样例在 `template/examples/model_workflows.py:retrieval_example`。
样例的三维向量、批量 2 仅供测试，真实数值必须来自目标模型的证据。

- Embedding：task=embedding，填写 embedding.input_types/default_dimensions/max_batch_size 等字段，
  实现 `ProviderPackage.embed()`，请求走 `/model/embeddings`，使用 inputs 而非 messages。
- Rerank：task=rerank，填写 rerank.max_documents 等字段，实现 `ProviderPackage.rerank()`，
  请求走 `/model/rerank`，使用 query/documents，保留返回结果的原始 index/document_id。
- 仅修改 task 而保留文本 execute() 不算适配；这两个任务不添加 vision/TTS 等 llm operations。

## 3. 推理档位的唯一口径

Kemo 标准逻辑档位为 `minimal / low / medium / high / max`。**不是所有模型都必须支持推理。**

- 不支持或新增能力尚未验证：`supported=false, efforts=[]`。
- 已验证五档映射：优先公开五档。厂商只有三档时允许显式折叠，但映射必须真实有效。
- 只验证了部分档位：只公开已验证集合，报告还缺哪些档位；不能为凑五档盲透传。
- 厂商只有开关、尚无可信强度映射：`supported=true, efforts=[]`，支持 enabled，不虚构强度。
  若明确验证五档均对应默认推理，可公开五档并用 `provider_default` 说明它们不是五种真实强度。
- `none` 是关闭请求，不放进 efforts；`xhigh` 不是模板第六档，只在明确兼容映射后处理。

模拟三档上游的可运行教学例子在
`template/examples/model_workflows.py:reasoning_example` 和 `reasoning_payload_example`。
例子的 `thinking_level` 是**模拟厂商字段**，不是通用 API 字段。
真实包要把每个逻辑档位转换为目标厂商的真实字段和值，不能把整份 Kemo reasoning 原样发送。

请求优先级：有 `request.reasoning` 时使用它；只有缺失时才考虑
`provider_options.reasoning_effort`，两条路径使用同一能力检查和映射。
测试必须覆盖每个已声明档位、禁用推理、非法档位、统一字段与旧字段冲突。
推理正文、摘要、Token 计量和可回放状态是四件不同的事，不要一起开。

## 4. 同步、测试、生效

`catalog_contract.validate_catalog()` 检查三处模型集合、完整模型名、静态能力字段与推理映射。
它不证明上游真的支持，不代替真实 Fixture。旧 Provider 尚无此检查时，可仅引入该检查文件和测试，
不要用整个模板覆盖旧包。保留该包原有格式；迁移 manifest 字段时逐项合并并运行回归。

Python/manifest 改动后需要重启。重启须经用户授权；用实际调用方密钥查询目录与能力，
确认新模型在其白名单内。若 `/model/models` 没出现，依次检查注册、启停、scope、完整名白名单，
不要先关闭鉴权或删除白名单。
