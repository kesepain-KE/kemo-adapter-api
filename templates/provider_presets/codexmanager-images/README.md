# CodexManager 图片服务

这是 `openai-images` 的参考模板，版本 `1.0.0`。请确认本机 CodexManager 的地址、鉴权方式和实际模型 ID；预填图片模型仅是参考目录，验证状态为 `declared`。

图片服务应使用单独上游，不修改已有 `openai-responses` 文本上游。`/models` 未列出图片模型时，仍可手动填入准确 ID 并登记；自动探测调用 `/images/generations`，成功后再调用 `/images/edits`。这些请求会产生真实用量。超时默认 180 秒，临时 429/5xx 不会作为能力不支持的证据。

智能体操作、登记和 Asset 调用示例见 [图片模型手册](../../../agent/_doc/images.md)。
