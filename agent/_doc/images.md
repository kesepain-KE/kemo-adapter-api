# 图片模型的发现、能力探测与调用

[返回操作入口](README.md) · [提供商](providers.md) · [模型与路由](models.md)

图片模型可能不出现在上游 `/models` 目录中。目录为空、只返回文本模型、模板有参考图片模型，都不能证明该图片模型可用或不可用。使用用户提供或服务方确认的准确模型 ID，独立登记并测试。

## 选择图片上游

图片生成与编辑使用 `openai-images`，对应 `/images/generations` 和 `/images/edits`。`openai-responses` 与 `openai-chat` 继续处理其文本协议。若同一个服务同时提供文本和图片，创建两个上游，共用相同地址和授权凭据，各选自己的适配器；不要把已有文本上游改成图片协议。

CodexManager 可选 `codexmanager-images` 模板，默认地址 `http://127.0.0.1:48760/v1`、私网允许、`api-key` 鉴权、180 秒超时。按实际部署修改。模板中的 `gpt-image-2`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst` 是使用反馈里的参考 ID，不是网关验证结果。

发现成功时 `models` 只表示上游返回的目录；`reference_models` 单独列出未被上游列出的模板参考，状态 `unverified`。网页可选择参考项，也可手填 ID；登记前仍需确认目标模型。

## 登记与探测

创建图片上游后，按 [模型登记](models.md) 使用：

```json
{
  "expected_revision": "<目录 revision>",
  "value": {
    "models": [{
      "upstream_model": "gpt-image-2",
      "public_model": "my-image-model",
      "task": "llm"
    }]
  }
}
```

省略 `capabilities` 会实际生成一张图片；成功后使用输出 Asset 再测试图片编辑。两次调用都计量、记日志，可能产生费用；仅按用户授权执行。图片探测使用该上游保存的超时，不受文本探测的 20 秒上限影响；HTTP 客户端超时需要覆盖整批探测时间。

结果 `checks.image_generation`、`checks.image_edit` 和 `checks.output.image` 分别给证据。生成成功后公开模型为 `verified`；只有编辑成功才加入图片输入和 `image_edit: true`。明确的 400/404/415/422 为该项不支持，401/403、限流、超时、502 等故障为未知；不能把临时故障改成人工“不支持”。

无需立即测试时显式声明：

```json
{
  "input_modalities": ["text", "image"],
  "output_modalities": ["image"],
  "streaming": false,
  "extensions": {
    "operations": {"image_generation": true, "image_edit": true}
  }
}
```

声明只在资料已确认时使用，保存为 `declared`。后续用 Deployment 的 `/test` 单次生成检查，或 `/probe` 获取完整建议能力。探测接口不自动发布；编辑后保存仍为人工声明。图片协议未实现工具和流式，不要勾选这些能力。

## 调用与 Asset

使用有目标模型、任务与 Asset 权限的下游密钥，`POST /model/responses`：

```json
{
  "protocol_version": "2.0",
  "request_id": "req_image_example",
  "model": "my-image-model",
  "stream": false,
  "input": [{
    "type": "message", "id": "msg_user", "role": "user",
    "content": [{"type": "text", "text": "画一个白底红色苹果"}]
  }],
  "metadata": {"capability": "image_generation"},
  "output": {"modalities": ["image"], "image": {"format": "png", "size": "1024x1024"}}
}
```

请求头 `Idempotency-Key` 必须等于 `request_id`，另带 `Authorization: Bearer <下游密钥>` 和 `X-Kemo-Protocol-Version: 2.0`。

成功结果的 message.content 包含 `type: image`、`asset_id`、`mime_type`、`checksum_sha256`。使用同一身份 `GET /assets/<asset_id>` 读取元数据，`GET /assets/<asset_id>/content` 下载内容。实际内容经严格 Base64 解码、图片签名和 Asset 存储边界处理；`revised_prompt` 保存在 Asset 元数据中。上游只返回 URL 时当前适配器会报响应错误，不会自行访问该 URL。

编辑时，先按 Asset 上传接口将源图存入网关，在 input.content 中增加：

```json
{"type": "image", "asset_id": "asset_<真实 ID>", "mime_type": "image/png"}
```

同时把 `metadata.capability` 改为 `image_edit`。也可以引用同一身份已有的输出 Asset。网关解析授权后的本地 Asset，用 multipart 发送 `image[]`；需要遮罩时在 `metadata.multimodal.assets` 中将对应已出现在 input 中的 Asset 标为 `role: mask`。最多一个遮罩，必须另有源图片。不支持把远程 URL 直接用作编辑源图。

输出格式为 `png/jpeg/webp`，尺寸通过 `output.image.size` 传递。上游需要的 `quality`、`background`、`moderation`、`output_compression` 在图片上游的 Profile 中配置；其实际取值是否支持由该模型决定。需要旧式 Base64 响应参数的服务可在 Profile 设置 `response_format: b64_json`。普通调用一次生成一张图片，不把一次 Kemo 多候选误映射成多个收费生成。

图片返回的 `input_tokens/output_tokens/total_tokens` 保留上游计量，`usage.media.output_images` 来自实际保存的图片数；未返回 Token 时保持未知，不用文字计量器猜测图像成本。
