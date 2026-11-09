# 内置能力探测样本

`manifest.yaml` 声明样本文件、MIME、问题、操作与本地答案匹配规则。登记模型和手动“探测能力”复用这些文件；只对 Adapter 已实现的输入模态发送请求。普通“测试模型”仍只发送一次基础请求。

| 文件 | 内容 | 验证目标 |
| --- | --- | --- |
| `audio.wav` | 2.94 秒英语合成语音，口令为 `copper meadow` | 读取音频内容并转写 |
| `video.mp4` | 3 秒无声视频，每秒依次显示红、绿、蓝色 | 读取视频内容并识别时间顺序 |
| `document.pdf` | 单页文档，识别码为 `LARCH-5821` | 读取 PDF 附件内容 |

样本是本项目创建的合成内容，不含个人数据，也不依赖外部下载。音频使用 Windows System.Speech / Microsoft Zira Desktop 生成，16 kHz、单声道、16-bit PCM；视频使用 FFmpeg 生成，128×128、4 fps、H.264/yuv420p；PDF 使用 ReportLab 生成，使用内置字体。上述工具只用于制作素材，网关运行时直接读取随源码提供的文件，无需安装生成工具。

样本通过现有 AssetStore 登记为 `gateway / capability-probe` 主体的输入 Asset，使用内容摘要和样本 ID 生成幂等键，重复和并发探测复用同一份有效内容，按 Asset 保留策略过期。Adapter 通过绑定的 `RequestContext.assets` 读取，Kemo 请求只携带 Asset ID，不传本地路径。自定义 worker 尚无跨进程 Asset 访问能力，对应检查返回 `asset-access-unavailable`。

上游问题不包含答案或匹配规则。只有正常完成且回答匹配样本内容才标为 `supported`；泛泛的“OK”、错误答案或未完成响应为 `unknown / response-not-confirmed`。基础文本测试未通过时不再发送媒体请求。HTTP 错误和超时沿用逐项探测状态及计量流程。

通过只说明模型能处理这些具体样本，不代表支持该模态下的全部格式、时长或复杂任务。当前样本覆盖输入理解；媒体生成仍由 Adapter 的真实输出实现决定。
