# 协议适配器开发

此目录提供可运行的虚构原生协议示例，不依赖 OpenAI 请求形状：

| 请求 | 示例上游格式 |
| --- | --- |
| `GET /native/models` | `{"engines":["native-v1"]}` |
| `POST /native/generate` | `{"engine":"native-v1","prompt":"...","stream":false}` |
| 非流式结果 | `{"answer":"hello","usage":{"in":2,"out":3}}` |
| SSE 结果 | `data: {"delta":"hello"}`，最后 `data: {"done":true,"usage":{"in":2,"out":3}}`，帧间空行 |

`adapter.py` 已实现 execute、stream、discover。将这些映射替换为真实上游格式，在 `manifest.yaml` 中设置唯一 ID，并按实际实现声明范围。`tests/gateway/test_worker_concurrency.py` 包含本地真实 HTTP 服务、ZIP 安装、发现及两种调用的集成回归，可运行：

```powershell
python -B -m pytest tests/gateway/test_worker_concurrency.py -q
```

## 清单与连接边界

`usage_semantics` 必填，必须写清上游 token 口径；`discovery` 默认 true，没有发现方法时必须显式设 false。`streaming` 默认 false。`contract_version` 当前为 `"1.0"`。

| manifest 字段 | 含义 |
| --- | --- |
| `id/name/version/contract_version` | 扩展身份与合同版本 |
| `operations` | 实现的 `llm/embedding/rerank` |
| `streaming/discovery` | 是否提供流式和发现方法 |
| `endpoints` | 已实现端点的说明；动态路径仍由适配器构造 |
| `fixed_headers` | 固定非敏感协议头，例如版本头 |
| `auth_schemes` | `id/location/name/prefix`，location 为 header/query/none |
| `body_fields` | 编码器可能写出的全部顶层字段，保存连接时用于拒绝额外参数冲突 |
| `media.input/output` | 编解码器真实范围，默认 text；检索输出为 embedding/score |
| `probe_operation` | 可选媒体探测任务，见媒体手册；file_generation 仅用于内部探测编排 |
| `tools/multiple_choices/structured_output` | 编码器实现范围，示例 tools 为 false |
| `reasoning_returns/reasoning_levels` | 推理返回形式与可映射档位 |
| `persisted_reasoning/multimodal_tool_results/prediction/service_tiers/logprobs/verbosity` | 对应的编码或解码实现 |
| `embedding_input_types/embedding_normalization` | 检索适配器实现范围 |
| `config_schema` | 适配器专属 Profile JSON Schema |

范围声明不能证明具体模型支持某个能力。每个模型的推理档位、并行调用、上下文长度仍由探测证据或操作者声明确定。worker 通过按调用身份绑定的 Asset 合同传递媒体，声明范围须与实际实现一致，详见 [媒体手册](../../agent/_doc/media.md)。

连接保存为 `Upstream`：包含 base_url、timeout、auth_scheme、非敏感 headers、profile、proxy 等。凭据由路由选取并通过 `RequestHeaders` 传入，其中 `.query` 包含 query 鉴权、`.secrets` 用于脱敏；请求时把此对象原样交给 Transport，不能转换成普通 dict 后丢失 query。`profile.extra_body/extra_query` 由 Transport 合并，不要重复合并。其余 Profile 字段按 config_schema 校验。

Transport 校验并固定出站地址，不依赖环境代理、不跟随跳转、不自动重试。连接与代理的完整操作规则见 [提供商手册](../../agent/_doc/providers.md)。

## 方法与请求上下文

`create_adapter(transport)` 返回适配器实例。所有生成方法接收 `(request, context, upstream_model, upstream, headers)`；`upstream_model` 是路由选择的上游真实模型名。

| 声明 | 方法与返回 |
| --- | --- |
| llm | `async execute(...) -> ProviderResult` |
| streaming | `stream(...) -> AsyncIterator[ProviderEvent]` |
| embedding | `async embed(...) -> ProviderEmbeddingResult` |
| rerank | `async rerank(...) -> ProviderRerankResult` |
| discovery | `async discover(upstream, headers) -> list[str]` |
| 可选详情 | `async describe_model(upstream, model, headers) -> dict` |

内置适配器的 `list_models` 转调 discover；worker 也提供同一入口。describe_model 返回归一化 `limits`（context_window、max_input_tokens、max_output_tokens）及可用的 reasoning_efforts；无元数据可返回 `{}`。网络和鉴权错误应抛出真实错误，由探测编排记为 unknown，不伪造支持证据。

`request` 为 KEMO 2.0 请求对象。LLM 的 input、tools、generation、reasoning、structured_output 已经过请求前能力校验；编码器仍需准确实现上游格式。

| RequestContext 字段 | 含义 |
| --- | --- |
| tenant_id/subject_id | 可信调用身份 |
| request_id/response_id/trace_id | 当前请求与追踪身份 |
| gateway_system_prompt | 网关最高优先级系统提示词 |
| live_config_revision | 此执行持有的配置代号 |
| gateway_key_id | 下游调用密钥 ID，可为空 |
| assets | 按调用身份绑定的资源访问器；worker 可解析本次输入并暂存输出，由主进程校验发布 |

不要将内部上下文和凭据写进公开 output、metadata 或日志。

## 规范化结果

```python
ProviderResult(
    status="completed",  # requires_action/incomplete/failed/cancelled 也可用
    output=[{
        "type": "message", "id": "msg_unique", "role": "assistant",
        "phase": "final_answer", "content": [{"type": "text", "text": "hello"}],
    }],
    usage=normalize_usage(raw_usage, input_field="in", output_field="out"),
    provider_response_id="upstream-response-id",
)
```

其余字段为 `error`（统一 ErrorObject）、`incomplete_details`（例如 `{"reason":"output_truncated"}`）、metadata、extensions。工具输出使用 ToolCallItem：id、call_id、name、arguments，并保留 arguments_raw 和参数解析错误。可复用 `gateway.adapters.builtin.common.tool_item`，不要把损坏的 JSON 当作空对象成功执行。

```python
normalize_usage(
    raw, *,
    input_field, output_field,
    cache_field=None, reasoning_field=None,
    input_includes_cache=True,
    output_includes_reasoning=True,
) -> Usage
```

字段可用点分路径，例如 `input_tokens_details.cached_tokens`。未知 token 保持 None，不能填零伪造精确计量；input_includes_cache=False 表示上游 input 不含 cache，需要加回，output_includes_reasoning=False 同理将推理 token 加回 output。向量/排序结果的结构见 `core/provider_contract.py`，向量 index 必须对应输入位置。

## 流式事件全集

事件是 `core.provider_contract.ProviderEvent`，kind 使用 ProviderEventKind。适配器不生成公共 SSE 信封、sequence、event_id 或重放记录，这些由核心处理。

| Kind | 主要字段 |
| --- | --- |
| ITEM_ADDED | item_id、item 身份快照 |
| TEXT_DELTA / TEXT_DONE | item_id、content_index、delta / text |
| REFUSAL_DELTA / REFUSAL_DONE | item_id、delta / text |
| AUDIO_DELTA | item_id、content_index、data |
| REASONING_SUMMARY_DELTA / REASONING_CONTENT_DELTA | item_id、delta |
| TOOL_ARGUMENTS_DELTA | item_id、call_id、name、delta |
| TOOL_COMPLETED | item_id、call_id、name、完整 item |
| MEDIA_COMPLETED | item_id、content_index、完整媒体 item，必须包含已登记输出 Asset |
| USAGE | usage |
| COMPLETED / INCOMPLETE / FAILED / CANCELLED | result 完整终态 |
| ERROR | error，不再发第二个终态 |

相同工具调用的增量、完成及终态必须使用相同 item_id/call_id。增量是未验证的生成过程，只有参数解析和 Schema 校验通过后，核心才发布 TOOL_COMPLETED。终态须包含完整 output 与 usage。

首次文本/拒绝增量可在内部 `item={"phase":"commentary"}` 中提示阶段。核心将它写入 ItemStart，不把提示作为公开 delta 的 item。无法判断时保持 None，不能先声明 final_answer 后改为 commentary。仅有文本增量时核心会在 usage/终态前补 TEXT_DONE；拒绝生产者须发 REFUSAL_DONE，并与终态 refusal 一致。

## 错误、并发与安装

已知上游错误抛 `ProviderException(ErrorObject(...))`，确定性参数错误设 retryable=False，保留 provider_status 和可报告 details。worker 编程异常默认不可重试，真实 traceback 进入脱敏 stderr 缓冲；控制台可读取 Worker 日志。不要捕获异常后返回空成功，也不要承诺任何网络错误都不会重复计费。

同一实例的多个请求可并发执行，实例可变状态须按请求隔离。取消某个流只取消对应任务；不要在单请求 finally 中关闭共享 Transport，不阻塞事件循环，不吞 CancelledError。开发代码使用 stderr 输出诊断，stdout 由 worker 保留给 RPC。

将 manifest.yaml、adapter.py 和可选 README.md 放在 ZIP 根目录，通过控制台安装并启用，再添加使用该适配器的连接和模型。依赖须已安装在网关 Python 环境，包不能包含安装脚本。worker 隔离进程故障，不是文件系统或网络沙箱，只安装信任的代码。完整智能体操作入口为 [agent/_doc](../../agent/_doc/README.md)。
