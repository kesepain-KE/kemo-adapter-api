# 提供商预制模板

本目录包含 46 个连接预制：P0 4 个、P1 6 个、P2 33 个、P3 3 个。各模板完成离线结构与合同校验；DeepSeek、Command Code 已按模板默认配置完成基础非流式与流式实测，标为 `verified`，其余 44 个为 `declared`。文档核对不升级为 `verified`，基础连通性验证不代表所有模型或能力均受支持。原有 `../provider_template/` 保留为自行开发模板的脚手架。

## 使用

1. 网关启动时自动加载本目录预制，控制台“提供商模板”直接列出并标记为内置、已启用，无需打包安装。
2. 在“上游服务”添加连接，选择模板，在凭据字段填写密钥；先替换 Cloudflare 的 `{account_id}`、Bedrock 的 `{region}` 等占位符，再发现或手动登记模型，建立 Public Model 与 Deployment。
3. 自定义模板仍通过 ZIP 安装和启用，ZIP 根目录包含 `manifest.yaml`、`template.yaml`、`README.md`，不包含外层文件夹。同 ID 扩展启用时采用扩展配置，停用后恢复内置默认值；已创建上游的独立配置不变。
4. 根据真实模型能力填写声明。离线测试不产生 `verified` 状态，真实调用验证成功后才能标记 `verified`。

`catalog` 全部为空，避免固定过期模型。`perplexity-agent`、`cloudflare-workers-ai`、`cohere-rerank` 当前没有模型发现能力，需手动登记模型；其他上游若不开放目录，也应手动登记。

只有 `openai-chat` 接受 Profile：OpenAI 新模型与 Bedrock GPT-OSS 预制显式使用 `max_completion_tokens`，其余 Chat 预制显式使用 `max_tokens`。OpenAI 的推理档位映射只适用于接受对应档位的模型；其他模板未配置 `reasoning_effort_map`，不声明推理能力。其他八个适配器的 Profile 均为空。Anthropic Messages 当前不开放结构化输出；Cloudflare 当前只支持文本非流式生成。模板不推断模型能力。

46 个模板均提供 `tokenizer` 选项。所有厂商优先使用自身响应的 usage；仅成功文本调用缺失字段时，在 Attempt 日志中补充本地估算。OpenAI / Responses / Embeddings 使用 `openai`，DeepSeek 使用 `deepseek`；Qwen、GLM、Kimi、Cohere、Mistral、混元、ERNIE、MiniMax、StepFun、百川等模板预选各自公开词表。Anthropic、xAI、豆包与商汤预选的公开资源分别来自 Claude legacy、Grok-1、Seed OSS、SenseNova SI，不代表这些厂商全部闭源模型的精确计费。聚合服务等模板使用 `auto`，未识别模型由 DeepSeek 兜底；用户可在高级配置中改选全部 22 个家族。词表随源码分发，不额外调用远程 count_tokens；缓存和媒体未知值不估算。详见 [词表版本、许可证及计量边界](../../gateway/tokenizers/README.md)。

## 连接契约与核对记录

模板使用 `auth_scheme`，取值须来自适配器 `auth_schemes`。可选 `timeout`、`note`、`allow_private_network` 在创建连接时作为默认值生效，显式提交值优先；更换模板不会回填已有连接。连接与模板的 `adapter_id` 不一致时拒绝发布。额外 body/query 与代理按 [提供商手册](../../agent/_doc/providers.md) 操作，模板不保存密钥。

2026-11-09 按以下官方资料核对优先项，均保持 declared：

| 模板 | 结论与依据 |
| --- | --- |
| PPIO | 默认改为 `https://api.ppio.com/openai/v1`；[官方请求示例](https://ppio.com/docs/models/ppio-4b) 使用该 Base URL，[快速开始](https://ppio.com/docs/quickstart) 也采用 `/openai` 路径族。旧保存连接不被覆盖 |
| 腾讯 TokenHub | 保留 `https://tokenhub.tencentmaas.com/v1`，与 [官方调用示例](https://cloud.tencent.com/document/product/1772/128947) 一致 |
| UCloud Modelverse | 保留 `https://api.modelverse.cn/v1`，与 [官方文本生成参考](https://docs.ucloud.cn/modelverse/api_doc/text_generation) 一致 |
| Cohere Chat / Rerank | Chat 默认统一为 `https://api.cohere.com`，Rerank 保持 `https://api.cohere.com/v2`，与 [官方 SDK 生产环境](https://github.com/cohere-ai/cohere-python/blob/main/src/cohere/environment.py) 对齐 |

`command-code`、`opencode` 保留原 ID 与连接兼容，分类为 `coding_service`，控制台显示“编码工具服务”；其他为 `provider`。分类用于辨认服务类型，不推断模型或能力。具体连接的成功测试也不能证明该厂商所有预制入口或所有模型均已验证。

本轮实测于 2026-11-09 完成：DeepSeek 的 `deepseek-flash`、Command Code 的 `deepseek/deepseek-v4.1-flash` 各调用一次非流式和流式，使用预制的默认 Base URL、Bearer 与 Profile，不借用已有连接的鉴权覆盖。四次均完整返回短文本及厂商 usage。验证证据与适用范围写入各预制说明；模型能力仍独立探测和配置。

## 预制清单

| 批次 | 模板 ID | 名称 | 内置适配器 |
| --- | --- | --- | --- |
| P0 | [anthropic](anthropic/README.md) | Anthropic | `anthropic-messages` |
| P0 | [gemini](gemini/README.md) | Google Gemini (AI Studio) | `gemini` |
| P0 | [openai](openai/README.md) | OpenAI | `openai-chat` |
| P0 | [openai-responses](openai-responses/README.md) | OpenAI Responses | `openai-responses` |
| P1 | [dashscope](dashscope/README.md) | 阿里云百炼 | `openai-chat` |
| P1 | [deepseek](deepseek/README.md) | 深度求索 | `openai-chat` |
| P1 | [moonshot](moonshot/README.md) | 月之暗面 Kimi | `openai-chat` |
| P1 | [volcengine](volcengine/README.md) | 火山方舟 豆包 | `openai-chat` |
| P1 | [xai](xai/README.md) | xAI Grok | `openai-chat` |
| P1 | [zhipu](zhipu/README.md) | 智谱 GLM | `openai-chat` |
| P2 | [ai21](ai21/README.md) | AI21 | `openai-chat` |
| P2 | [baichuan](baichuan/README.md) | 百川智能 | `openai-chat` |
| P2 | [bedrock](bedrock/README.md) | AWS Bedrock | `openai-chat` |
| P2 | [byteplus](byteplus/README.md) | BytePlus ModelArk | `openai-chat` |
| P2 | [cerebras](cerebras/README.md) | Cerebras | `openai-chat` |
| P2 | [cloudflare](cloudflare/README.md) | Cloudflare Workers AI | `cloudflare-workers-ai` |
| P2 | [cohere](cohere/README.md) | Cohere | `cohere-chat` |
| P2 | [command-code](command-code/README.md) | Command Code Provider | `openai-chat` |
| P2 | [deepinfra](deepinfra/README.md) | DeepInfra | `openai-chat` |
| P2 | [fireworks](fireworks/README.md) | Fireworks AI | `openai-chat` |
| P2 | [gitee-ai](gitee-ai/README.md) | Gitee AI / 模力方舟 | `openai-chat` |
| P2 | [groq](groq/README.md) | Groq | `openai-chat` |
| P2 | [huggingface](huggingface/README.md) | HF Router | `openai-chat` |
| P2 | [hunyuan](hunyuan/README.md) | 腾讯混元 | `openai-chat` |
| P2 | [hyperbolic](hyperbolic/README.md) | Hyperbolic | `openai-chat` |
| P2 | [infini](infini/README.md) | 无问芯穹 | `openai-chat` |
| P2 | [minimax](minimax/README.md) | MiniMax | `openai-chat` |
| P2 | [mistral](mistral/README.md) | Mistral | `openai-chat` |
| P2 | [modelscope](modelscope/README.md) | 魔搭 ModelScope | `openai-chat` |
| P2 | [nebius](nebius/README.md) | Nebius Token Factory | `openai-chat` |
| P2 | [opencode](opencode/README.md) | OpenCode Console | `openai-chat` |
| P2 | [openrouter](openrouter/README.md) | OpenRouter | `openai-chat` |
| P2 | [perplexity](perplexity/README.md) | Perplexity | `perplexity-agent` |
| P2 | [ppio](ppio/README.md) | PPIO 派欧云 | `openai-chat` |
| P2 | [qianfan](qianfan/README.md) | 百度千帆 | `openai-chat` |
| P2 | [sambanova](sambanova/README.md) | SambaNova | `openai-chat` |
| P2 | [sensenova](sensenova/README.md) | 商汤日日新 | `openai-chat` |
| P2 | [siliconflow](siliconflow/README.md) | 硅基流动 | `openai-chat` |
| P2 | [spark](spark/README.md) | 讯飞星火 | `openai-chat` |
| P2 | [stepfun](stepfun/README.md) | 阶跃星辰 | `openai-chat` |
| P2 | [tencent-tokenhub](tencent-tokenhub/README.md) | 腾讯云 TokenHub | `openai-chat` |
| P2 | [together](together/README.md) | Together AI | `openai-chat` |
| P2 | [ucloud](ucloud/README.md) | UCloud Modelverse | `openai-chat` |
| P3 | [cohere-rerank](cohere-rerank/README.md) | Cohere Rerank | `cohere-rerank` |
| P3 | [openai-embedding](openai-embedding/README.md) | OpenAI Embeddings | `openai-embedding` |
| P3 | [siliconflow-embedding](siliconflow-embedding/README.md) | 硅基流动 Embeddings | `openai-embedding` |

## 与旧分配表的差异

- 按最终协议决策及已注册源码，Cohere、Cloudflare、Perplexity 分别使用 `cohere-chat`、`cloudflare-workers-ai`、`perplexity-agent`。
- Cloudflare 的 Base URL 包含 `/accounts/{account_id}/ai`，由适配器追加 `/run/<model>`；不直接使用最终决策表中缺少账户路径的 `/client/v4`。
- Cohere Rerank 按官方 v2 HTTP 接口使用 `https://api.cohere.com/v2` 与 Bearer；由现有适配器追加 `/rerank`。旧表的无版本地址与 api-key 不能形成正确请求。
- 当前源码已将 Chat 默认 token 字段改为 `max_tokens`；模板仍显式声明，避免依赖隐式默认值。
- 批次数量以完整厂商清单为准，总数 46；分批文档中的 42 和 P2 29 未采用。所有模板均遵守本轮 `catalog: []` 要求，包括需要手动登记的 rerank 上游。

结构回归：`D:\Anaconda3\envs\kemo\python.exe -m pytest tests/gateway/test_provider_presets.py -q`。
