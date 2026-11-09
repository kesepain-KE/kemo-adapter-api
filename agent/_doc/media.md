# 音频、视频和文件

[返回操作入口](README.md) · [提供商](providers.md) · [模型与路由](models.md) · [图片](images.md)

本手册指导智能体配置媒体服务、验证能力并读写媒体结果。每个上游只选一个真实协议；同一服务的文本、语音、视频和文件生成应分别创建上游，保留原文本连接。

## 选择协议与模板

| 用途 | 适配器 ID | 上游协议 | 结果 |
| --- | --- | --- | --- |
| 音频转写 | `openai-transcription` | multipart `/audio/transcriptions` | 文本 |
| 文本转语音 | `openai-speech` | JSON `/audio/speech`，二进制音频响应 | 音频 Asset，可流式 |
| 音频对话 | `openai-audio-chat` | `/chat/completions` 的 `modalities/audio` 字段 | 文本或音频 Asset |
| 原生语音生成 | `gemini-speech` | `generateContent` 的 AUDIO 输出 | WAV Asset |
| 兼容视频任务 | `openai-video` | multipart `/videos`，轮询任务，读取 `/videos/<id>/content` | MP4 Asset |
| 原生视频任务 | `gemini-video` | `predictLongRunning`，轮询 operation，读取同源 files 内容 | MP4 Asset |
| 文件生成 | `openai-files` | Responses 托管代码工具及容器文件下载 | 文本和文件 Asset |

除 `openai-video` 外均提供同名内置模板，模板没有预填模型 ID。它们是连接参考，`verification: declared`，不能证明账号权限或特定模型可用。`openai-video` 表示兼容该任务协议的服务；不能仅凭协议名称推断某个官方视频服务仍可用，需填写用户实际使用的服务地址与模型。

音频与视频理解还可使用普通 `gemini`，音频和文件输入可使用 `openai-chat`；`openai-responses` 现在也支持 `input_file`。这些是输入能力，不能因此声明语音或视频输出。

所有协议沿用网关的凭据池、标准鉴权、代理、私网许可、用量与调用记录。用户手动输入模型 ID 后可以直接登记和探测，不要求它已出现在上游目录中。

## 登记与探测

按 [提供商手册](providers.md) 创建上游，使用实际地址、密钥和模型。媒体任务可能耗时较长，模板默认语音 180 秒、文件 300 秒、视频 600 秒；按实际服务调整，管理客户端的请求超时也需覆盖完整探测。

登记请求不填写 `capabilities` 时，网关按适配器清单的 `probe_operation` 执行对应任务：

- `audio_transcription`：上传内置录音，核对口令，收到任意文字不算通过。
- `speech_generation`：生成短语音并登记真实音频 Asset；支持流式的适配器另做流式验证。
- `speech_to_speech`：上传内置录音，请求音频回答并验证真实输出 Asset。
- `video_generation`：提交短视频任务，等到完成并下载有效 MP4。
- `file_generation`：请求包含指定校验码的文本文件，下载后检查文件内容。这个值用于适配器探测编排，**不是新增的 Kemo 公开操作**。

图片仍按 [图片手册](images.md) 验证生成和编辑。生成成功不代表参考图片、其他输入格式或其他媒体操作也已通过。只发布有证据的范围；401/403、限流、超时和服务临时故障保留为未知，不改成“不支持”。

探测会实际调用上游，可能产生生成费和托管工具费。仅在用户已授权范围内执行。无需立即调用时，按已确认资料显式填写能力，保存为 `declared`。

音频生成声明示例：

```json
{
  "input_modalities": ["text"],
  "output_modalities": ["audio"],
  "streaming": true,
  "extensions": {"operations": {"speech_generation": true}}
}
```

`gemini-speech`、`openai-audio-chat` 和两个视频适配器当前 `streaming: false`。音频对话只有已验证 `audio_transcription`、`speech_generation` 或 `speech_to_speech` 时才声明相应操作。文件生成使用 `output_modalities: ["text", "file"]`，不新增 `extensions.operations.file_generation`。

## 调用和读取结果

所有请求使用 `POST /model/responses`，请求头包含下游 Bearer 密钥、`X-Kemo-Protocol-Version: 2.0`，`Idempotency-Key` 等于正文 `request_id`。下游密钥需要模型调用权限，输入上传与结果下载还需要对应 Asset 权限。

```json
{
  "protocol_version": "2.0",
  "request_id": "req_speech_example",
  "model": "<公开语音模型名>",
  "stream": false,
  "metadata": {"capability": "speech_generation"},
  "input": [{
    "type": "message", "id": "msg_user", "role": "user",
    "content": [{"type": "text", "text": "你好，欢迎使用网关。"}]
  }],
  "output": {
    "modalities": ["audio"],
    "audio": {"format": "wav", "voice": "default"}
  }
}
```

`openai-speech` 支持 WAV、MP3、FLAC、Opus。`voice: default` 使用上游 Profile.voice，未配置时使用协议默认音色；显式声音名由真实模型决定。PCM、AAC 当前不在此适配器可验证的输出格式内。`gemini-speech` 将原生单声道 16 位 PCM 包装为 WAV，保留采样率，不转码。

转写和语音转语音时先上传音频到 `/assets`，在 message.content 中增加 `{"type":"audio","asset_id":"<上传所得 ID>","mime_type":"audio/wav"}`。转写使用 `metadata.capability: audio_transcription` 与 `output.modalities: ["text"]`，一次一个音频。语音对话输入目前为 WAV、MP3，使用 `speech_to_speech` 和音频输出。

视频生成使用 `metadata.capability: video_generation`、`output.modalities: ["video"]`、`output.video: {"format":"mp4","duration_seconds":4}`。`openai-video` 支持此协议的 4/8/12 秒档位，Profile.size 设置宽高；`gemini-video` 将整数秒数交给实际模型校验，Profile.aspect_ratio 为 `16:9/9:16`。可选的一个参考图片通过图片 Asset 输入，是否可用须另行验证。Profile.poll_interval 为 0.1 至 30 秒，默认 2 秒。视频调用在网关执行超时范围内轮询，不是可跨网关重启恢复的任务队列。

文件生成使用 `metadata.capability: conversation`、`output.modalities: ["text","file"]` 和 `output.file: {"filename":"report.csv","mime_type":"text/csv"}`。上游必须实际支持 Responses 的 `code_interpreter`、`container_file_citation` 和容器文件内容接口；普通文本里的下载链接不算生成结果。文件名与 MIME 请求传给模型作为生成要求，响应始终报告实际下载文件的名称与类型，不把任意字节伪装成请求格式。

成功响应的 content 使用 `type: audio/video/file`、`asset_id`、`mime_type`、`checksum_sha256`。同一身份通过 `/assets/<id>` 查元数据，`/assets/<id>/content` 下载实际内容。语音流返回 `output_audio.delta`，完整内容登记后返回 `output_media.completed`；文件流在文件下载后返回 `output_media.completed`，随后返回终态。

Token 和时长以真实上游用量或可验证 WAV 时长为准，缺失项保持未知，不把请求视频时长当成实际用量。视频下载只走固定任务 ID 或已配置服务的同源 files 路径，不携带密钥访问任意上游返回 URL。任务已提交后的轮询、下载失败以及媒体生成超时不自动重新生成；错误若包含 `provider_job_id`，保留它用于在实际上游排障。

## 用户适配器的媒体能力

用户协议扩展可声明 `media.input/output` 中的图片、音频、视频和文件。方法内的 `context.assets` 与内置适配器使用相同接口：

```python
source = context.assets.resolve(block.asset_id)
with source.path.open("rb") as input_file:
    # 按真实协议将文件交给统一 Transport，不返回本地路径。
    ...

output = await context.assets.store_output(
    filename="answer.wav", mime_type="audio/wav", content=audio_chunks)
```

`resolve` 只取得本次输入已授权的 Asset 或本次 worker 生成的输出。`content` 可为 bytes 或异步字节迭代器，大文件不要转换成 RPC Base64。worker 将输出暂存到当前调用专用目录，主进程重新校验大小、内容、摘要与身份，再发布为调用者自己的 Asset；流事件和终态中的 ID 会一致映射。临时目录在调用结束或取消后清理。

`probe_operation` 可选填上表对应的探测任务，清单声明仍须与实现一致。未知协议的媒体返回不经过此访问器、伪造 Asset ID 或缺少所请求输出，都会失败。worker 是可信 Python 扩展的进程隔离，不是文件系统沙箱，只安装用户授权的代码。ZIP 安装、启停、替换与日志见 [资源手册](resources.md)。
