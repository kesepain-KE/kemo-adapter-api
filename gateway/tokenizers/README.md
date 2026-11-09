# 本地 token 计量

厂商响应中的 usage 是计量依据，适配器负责解释各协议的输入、输出、缓存、推理用量。OpenAI Chat 同时接受标准 `prompt_tokens_details.cached_tokens` 和 DeepSeek 的 `prompt_cache_hit_tokens`。Anthropic 保留缓存读取/写入语义，Gemini 保留缓存与推理用量，Cohere 使用 billed_units；rerank 的 search_units 不转换成 token。

成功调用缺失文本用量时，`gateway/token_counting.py` 为 **Attempt 日志**补充估算。公开 Kemo 响应仍保留厂商提供的 Usage，不把估算写成厂商证据。失败、取消、多模态输入和检索搜索单位不估算为精确用量。估算不会减少 TPM 或预算的安全预留；缓存数量、缓存命中率、价格未知时继续显示未知。

| 设置 | 词表与版本 |
| --- | --- |
| `auto` | 先识别 OpenAI 模型编码，再按 `catalog.yaml` 的模型前缀识别；未知模型用 DeepSeek |
| `deepseek` | DeepSeek V4，网关统一兜底 |
| `openai` | tiktoken 模型映射，内置 cl100k_base / o200k_base；未识别模型用 o200k_base |
| `qwen` | Qwen3 |
| `glm` | GLM 4.5 |
| `minimax` | MiniMax M2 |
| `mistral` | Mistral Small 3.1，Tekken |
| `hunyuan` | Tencent Hunyuan A13B |
| `ernie` | Baidu ERNIE 4.5 |
| `doubao` | ByteDance Seed OSS |
| `stepfun` | StepFun Step3 |
| `yi` | 01.AI Yi 1.5 |
| `mimo` | Xiaomi MiMo V2 |
| `sensenova` | SenseNova SI |
| `baichuan` | Baichuan2 |
| `internlm` | InternLM3 |
| `cohere` | Cohere Command R+ |
| `gemini` | Google GenAI SDK 使用的 Gemma3 词表 |
| `grok` | xAI Grok-1 |
| `ai21-j2` | AI21 Jurassic-2 legacy |
| `llama` | Meta Llama3.2，Unsloth 公开分发 |
| `claude-legacy` | Anthropic 公开 legacy tokenizer |
| `kimi` | Moonshot Kimi K2 |

共 22 个可选家族，另有 `auto`。计量器可在上游“高级配置”设置，界面从同一 YAML 目录读取选项。46 个提供商模板均声明 `tokenizer`：有对应公开词表的模板预选该词表，聚合服务等保留自动识别；修改模板不会覆盖用户已保存的上游设置。不为估算额外发送 count_tokens 请求。

这些估算统计文本和请求的结构信息，不复现厂商未公开的聊天模板或媒体编码；即使使用原厂词表也始终标记 `estimated` / `mixed`，费用显示近似值。完整输入/输出来自厂商时，保留 `provider` 精确证据。价格来自 Deployment 的调用时快照，不内置可能过期的模型价格。

## 资源

所有资源随源码分发，运行时不下载模型、不执行远程 tokenizer 代码。`tokenizers` 读取本地压缩 JSON，`sentencepiece` 读取原生模型，`tiktoken` 读取本地 BPE；Anthropic 的紧凑 ranks 按其原始 offset 格式展开，并沿用 NFKC 规范化。词表使用原生数据格式，网关自己的选择配置和来源清单使用 YAML。

`sources.yaml` 记录资源 URL、固定 revision、体积和 SHA-256；原有 `.LICENSE` 与 `notices/` 保留各资源许可证、使用条款和来源说明。`openai.yaml` 的正则表达式和特殊 token 来自 tiktoken 0.13.0 的 `tiktoken_ext/openai_public.py`。

公开词表的版本边界会直接体现在选择器标签中。Claude 公开 tokenizer 对 Claude 3 及以后版本只提供粗略估算；Grok-1 不代表当前 Grok 全系；AI21 Jurassic-2 不代表 Jamba；Seed OSS、SenseNova SI 不证明闭源豆包、日日新的分词规则。Gemma3 按 Google GenAI SDK 的公开本地实现接入，不假定适用于每个新版 Gemini。当前未取得可分发的星火、Jamba 专属词表时，保留自动识别与 DeepSeek 兜底。聚合服务按实际模型名选词表，不存在统一的“聚合平台 tokenizer”。

升级词表时同时更新来源清单、许可证和回归样例，不能把词表更新当作厂商计费规则的实测。
