# 配方 C：给单个模型增加多模态

只改变指定模型；不要给同厂商所有模型一起开启媒体能力。视觉识别不等于图片生成，
语音转文字不等于语音合成，文本流式也不等于媒体流式。

## 1. 选择准确的操作

下表是网关**核心合同**，不是厂商支持列表。这九种操作的 task 都是 `llm`，
请求走 `/model/responses`，用 `metadata.capability` 区分。

| 操作 | metadata.capability | 至少需要的输入 | 至少需要的输出 |
| --- | --- | --- | --- |
| 对话 | conversation | 文本 | text |
| 看图 | vision | text、image | text |
| 文生图 | image_generation | text | image |
| 图片编辑 | image_edit | text、image | image |
| 语音转文字 | audio_transcription | audio | text |
| 文字转语音 | speech_generation | text | audio |
| 语音转换 | speech_to_speech | audio | audio |
| 视频理解 | video_understanding | video | text |
| 视频生成 | video_generation | text | video |

文件是内容块模态，不是第十个 operation，也不是 `task=file`。是否支持文档内容、如何读取或
转换由目标 Provider 决定。Embedding 与 Rerank 使用各自接口和能力类型；实时双向会话等
合同外需求先说明需要核心扩展，不发明 `task=video` 绕过协议。

## 2. 先做真实链路，再公开能力

1. 核实目标上游端点支持**该模型、该账号、该操作**，整理授权范围内的脱敏样本。
2. 在 `provider.py` 的 execute/stream 路径按 `metadata.capability` 分派本包实现。
3. 协议映射读取 `context.assets`。模板默认文本调用没有把 assets 传给 mapper；
   改多模态时必须显式把 RequestContext 或 assets 向下传，不能只导入 `media.py`。
4. `protocol.py` 将文本和媒体保留在原顺序；调用 `parse_media_block()`，按目标上游格式转换。
5. `client.py` 使用实际端点及上传格式。TTS、ASR、图片/视频生成不能机械发到聊天端点。
6. 输出媒体经 `store_output_media()` 或 `context.assets.store_output()` 登记，再放入
   assistant message 的 content。不返回本机路径，不把二进制塞进普通文本或 SSE。
7. 覆盖协议与输出测试后，只给这一模型追加 input/output 模态及
   `extensions.operations.<操作>.supported=true`，同步 manifest。
8. 按任务更新 probe。文本 OK 探测不能验证视觉或生成链路；无法做对应探测时明确不支持。

模拟增量声明函数 `template/examples/model_workflows.py:add_verified_operations()`
演示保留旧能力与自定义字段、只修改单个模型。它**只合并声明，不实现媒体适配器**。
单模型同时支持 conversation、vision、speech_generation 时可以合并三项，不必复制三个模型名。
撤销某一操作时也要核对其他操作需要的模态，不能把它们仍使用的 image/audio 一并删掉。

## 3. 输入结构：只使用这些真实字段

```json
{
  "type": "image",
  "mime_type": "image/png",
  "detail": "auto",
  "source": {"kind": "url", "uri": "https://media.example.invalid/input.png"}
}
```

这是离线结构示例，`.invalid` 不可调用。真实 URL 必须经过授权且符合网关访问限制。
来源替换关系：`url/data_url` 用 `source.uri`，`inline_base64` 用 `source.data`；
MIME 始终在内容块的 `mime_type`，不是 `source.media_type`。
不要把客户端发送的本地路径当作可访问文件，不要读取未授权的任意路径。

大型媒体优先先上传 `/assets`，取得当前主体可访问的真实 asset_id，再传：

```json
{"type":"image","asset_id":"asset_replace_with_uploaded_id","mime_type":"image/png"}
```

此 ID 是占位符，不可用于真实调用。不要复用其他会话、租户或已过期的附件。
不支持的 object_store/provider_file_id 必须拒绝，不能伪装成 URL 或静默跳过。

## 4. 请求、响应都要验

九种完整请求与配对声明在 `template/examples/model_workflows.py`，不必从零猜必填字段。
`request_example("vision")` 返回合法 KemoRequest；真实调用前必须替换模型名、request_id、媒体，
并遵守对应网关凭据与网络安全限制。生成 image/audio/video 时必须同时填写
`output.modalities` 与 `output.image/audio/video`；专用多模态操作不携带业务 tools。

媒体响应是标准 `ProviderResult.output` 中的 assistant MessageItem，content 放
`asset_id/mime_type/checksum_sha256`；如流式已发送 MEDIA_COMPLETED，最终 output 保留同一个 Item。
`created_at` 要么省略使用默认值，要么是合法日期时间；不要发送 null。
Item ID 应按响应稳定且不冲突，工具结果必须保留对应 call_id/name，不能放宽协议校验掩盖错误。

## 5. 最低测试矩阵

- 正向：目标操作 + 目标媒体 → 上游 DTO 正确 → 标准响应可解析。
- 反向：纯文本模型收到媒体、错误 MIME、空来源、未声明输出、未知操作 → 明确拒绝。
- 权限：非法或过期 Asset、其他主体的 Asset → 拒绝；本机路径不泄露。
- 输出：MIME/checksum/Asset 一致；流式完成事件与最终 output 一致。
- 回归：同一模型原先文本/工具能力不被破坏；其他模型声明不变。
- 密钥：专用媒体调用仍经过统一密钥路由，取消回到原 Client；未接入的专用路径必须报告。

离线例子通过只证明 Kemo 结构与核心门禁匹配，不证明图片内容被识别或上游能生成媒体。
真实测试未授权时停止在离线阶段，不宣称“多模态完整支持”。
