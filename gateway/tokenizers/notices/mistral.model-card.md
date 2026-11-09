---
library_name: vllm
language:
- en
- fr
- de
- es
- pt
- it
- ja
- ko
- ru
- zh
- ar
- fa
- id
- ms
- ne
- pl
- ro
- sr
- sv
- tr
- uk
- vi
- hi
- bn
license: apache-2.0
inference: false
base_model:
- mistralai/Mistral-Small-3.1-24B-Base-2503
extra_gated_description: >-
  If you want to learn more about how we process your personal data, please read
  our <a href="https://mistral.ai/terms/">Privacy Policy</a>.
tags:
- mistral-common
---

# Model Card for Mistral-Small-3.1-24B-Instruct-2503

Building upon Mistral Small 3 (2501), Mistral Small 3.1 (2503) **adds state-of-the-art vision understanding** and enhances **long context capabilities up to 128k tokens** without compromising text performance. 
With 24 billion parameters, this model achieves top-tier capabilities in both text and vision tasks.  
This model is an instruction-finetuned version of: [Mistral-Small-3.1-24B-Base-2503](https://huggingface.co/mistralai/Mistral-Small-3.1-24B-Base-2503).

Mistral Small 3.1 can be deployed locally and is exceptionally "knowledge-dense," fitting within a single RTX 4090 or a 32GB RAM MacBook once quantized.  

It is ideal for:
- Fast-response conversational agents.
- Low-latency function calling.
- Subject matter experts via fine-tuning.
- Local inference for hobbyists and organizations handling sensitive data.
- Programming and math reasoning.
- Long document understanding.
- Visual understanding.

For enterprises requiring specialized capabilities (increased context, specific modalities, domain-specific knowledge, etc.), we will release commercial models beyond what Mistral AI contributes to the community.

Learn more about Mistral Small 3.1 in our [blog post](https://mistral.ai/news/mistral-small-3-1/).

## Key Features
- **Vision:** Vision capabilities enable the model to analyze images and provide insights based on visual content in addition to text.
- **Multilingual:** Supports dozens of languages, including English, French, German, Greek, Hindi, Indonesian, Italian, Japanese, Korean, Malay, Nepali, Polish, Portuguese, Romanian, Russian, Serbian, Spanish, Swedish, Turkish, Ukrainian, Vietnamese, Arabic, Bengali, Chinese, Farsi.
- **Agent-Centric:** Offers best-in-class agentic capabilities with native function calling and JSON outputting.
- **Advanced Reasoning:** State-of-the-art conversational and reasoning capabilities.
- **Apache 2.0 License:** Open license allowing usage and modification for both commercial and non-commercial purposes.
- **Context Window:** A 128k context window.
- **System Prompt:** Maintains strong adherence and support for system prompts.
- **Tokenizer:** Utilizes a Tekken tokenizer with a 131k vocabulary size.

## Benchmark Results

When available, we report numbers previously published by other model providers, otherwise we re-evaluate them using our own evaluation harness.

### Pretrain Evals

| Model                          | MMLU (5-shot) | MMLU Pro (5-shot CoT) | TriviaQA   | GPQA Main (5-shot CoT)| MMMU      |
|--------------------------------|---------------|-----------------------|------------|-----------------------|-----------|
| **Small 3.1 24B Base**         | **81.01%**    | **56.03%**            | 80.50%     | **37.50%**            | **59.27%**|
| Gemma 3 27B PT                 | 78.60%        | 52.20%                | **81.30%** | 24.30%                | 56.10%    |

### Instruction Evals

#### Text

| Model                          | MMLU      | MMLU Pro (5-shot CoT) | MATH                   | GPQA Main (5-shot CoT) | GPQA Diamond (5-shot CoT )| MBPP      | HumanEval | SimpleQA (TotalAcc)|
|--------------------------------|-----------|-----------------------|------------------------|------------------------|---------------------------|-----------|-----------|--------------------|
| **Small 3.1 24B Instruct**     | 80.62%    | 66.76%                | 69.30%                 | **44.42%**             | **45.96%**                | 74.71%    | **88.41%**| **10.43%**         |
| Gemma 3 27B IT                 | 76.90%    | **67.50%**            | **89.00%**             | 36.83%                 | 42.40%                    | 74.40%    | 87.80%    | 10.00%             |
| GPT4o Mini                     | **82.00%**| 61.70%                | 70.20%                 | 40.20%                 | 39.39%                    | 84.82%    | 87.20%    | 9.50%              |
| Claude 3.5 Haiku               | 77.60%    | 65.00%                | 69.20%                 | 37.05%                 | 41.60%                    | **85.60%**| 88.10%    | 8.02%              |
| Cohere Aya-Vision 32B          | 72.14%    | 47.16%                | 41.98%                 | 34.38%                 | 33.84%                    | 70.43%    | 62.20%    | 7.65%              |

#### Vision

| Model                          | MMMU       | MMMU PRO  | Mathvista | ChartQA   | DocVQA    | AI2D        | MM MT Bench |
|--------------------------------|------------|-----------|-----------|-----------|-----------|-------------|-------------|
| **Small 3.1 24B Instruct**     | 64.00%     | **49.25%**| **68.91%**| 86.24%    | **94.08%**| **93.72%**  | **7.3**     |
| Gemma 3 27B IT                 | **64.90%** | 48.38%    | 67.60%    | 76.00%    | 86.60%    | 84.50%      | 7           |
| GPT4o Mini                     | 59.40%     | 37.60%    | 56.70%    | 76.80%    | 86.70%    | 88.10%      | 6.6         |
| Claude 3.5 Haiku               | 60.50%     | 45.03%    | 61.60%    | **87.20%**| 90.00%    | 92.10%      | 6.5         |
| Cohere Aya-Vision 32B          | 48.20%     | 31.50%    | 50.10%    | 63.04%    | 72.40%    | 82.57%      | 4.1         |

### Multilingual Evals

| Model                          | Average    | European   | East Asian | Middle Eastern |
|--------------------------------|------------|------------|------------|----------------|
| **Small 3.1 24B Instruct**     | **71.18%** | **75.30%** | **69.17%** | 69.08%         |
| Gemma 3 27B IT                 | 70.19%     | 74.14%     | 65.65%     | 70.76%         |
| GPT4o Mini                     | 70.36%     | 74.21%     | 65.96%     | **70.90%**     |
| Claude 3.5 Haiku               | 70.16%     | 73.45%     | 67.05%     | 70.00%         |
| Cohere Aya-Vision 32B          | 62.15%     | 64.70%     | 57.61%     | 64.12%         |

### Long Context Evals

| Model                          | LongBench v2    | RULER 32K   | RULER 128K |
|--------------------------------|-----------------|-------------|------------|
| **Small 3.1 24B Instruct**     | **37.18%**      | **93.96%**  | 81.20%     |
| Gemma 3 27B IT                 | 34.59%          | 91.10%      | 66.00%     |
| GPT4o Mini                     | 29.30%          | 90.20%      | 65.8%      |
| Claude 3.5 Haiku               | 35.19%          | 92.60%      | **91.90%** |

## Basic Instruct Template (V7-Tekken)

```
<s>[SYSTEM_PROMPT]<system prompt>[/SYSTEM_PROMPT][INST]<user message>[/INST]<assistant response></s>[INST]<user message>[/INST]
```
*`<system_prompt>`, `<user message>` and `<assistant response>` are placeholders.*

***Please make sure to use [mistral-common](https://github.com/mistralai/mistral-common) as the source of truth***

## Usage

The model can be used with the following frameworks;
- [`vllm (recommended)`](https://github.com/vllm-project/vllm): See [here](#vllm)

**Note 1**: We recommend using a relatively low temperature, such as `temperature=0.15`.

**Note 2**: Make sure to add a system prompt to the model to best tailer it for your needs. If you want to use the model as a general assistant, we recommend the following 
system prompt:

```
system_prompt = """You are Mistral Small 3.1, a Large Language Model (LLM) created by Mistral AI, a French startup headquartered in Paris.
You power an AI assistant called Le Chat.
Your knowledge base was last updated on 2023-10-01.
The current date is {today}.

When you're not sure about some information, you say that you don't have the information and don't make up anything.
If the user's question is not clear, ambiguous, or does not provide enough context for you to accurately answer the question, you do not try to answer it right away and you rather ask the user to clarify their request (e.g. "What are some good restaurants around me?" => "Where are you?" or "When is the next flight to Tokyo" => "Where do you travel from?").
You are always very attentive to dates, in particular you try to resolve dates (e.g. "yesterday" is {yesterday}) and when asked about information at specific dates, you discard information that is at another date.
You follow these instructions in all languages, and always respond to the user in the language they use or request.
Next sections describe the capabilities that you have.

# WEB BROWSING INSTRUCTIONS

You cannot perform any web search or access internet to open URLs, links etc. If it seems like the user is expecting you to do so, you clarify the situation and ask the user to copy paste the text directly in the chat.

# MULTI-MODAL INSTRUCTIONS

You have the ability to read images, but you cannot generate images.
You cannot read nor transcribe audio files or videos."""
```

### vLLM (recommended)

We recommend using this model with the [vLLM library](https://github.com/vllm-project/vllm)
to implement production-ready inference pipelines.

**_Installation_**

Make sure you install [`vLLM >= 0.8.1`](https://github.com/vllm-project/vllm/releases/tag/v0.8.1):

```
pip install vllm --upgrade
```

Doing so should automatically install [`mistral_common >= 1.5.4`](https://github.com/mistralai/mistral-common/releases/tag/v1.5.4).

To check:
```
python -c "import mistral_common; print(mistral_common.__version__)"
```

You can also make use of a ready-to-go [docker image](https://github.com/vllm-project/vllm/blob/main/Dockerfile) or on the [docker hub](https://hub.docker.com/layers/vllm/vllm-openai/latest/images/sha256-de9032a92ffea7b5c007dad80b38fd44aac11eddc31c435f8e52f3b7404bbf39).

#### Server

We recommand that you use Mistral-Small-3.1-24B-Instruct-2503 in a server/client setting. 

1. Spin up a server:

```
vllm serve mistralai/Mistral-Small-3.1-24B-Instruct-2503 --tokenizer_mode mistral --config_format mistral --load_format mistral --tool-call-parser mistral --enable-auto-tool-choice --limit_mm_per_prompt 'image=10' --tensor-parallel-size 2
```

**Note:** Running Mistral-Small-3.1-24B-Instruct-2503 on GPU requires ~55 GB of GPU RAM in bf16 or fp16. 


2. To ping the client you can use a simple Python snippet.

```py
import requests
import json
from huggingface_hub import hf_hub_download
from datetime import datetime, timedelta

url = "http://<your-server-url>:8000/v1/chat/completions"
headers = {"Content-Type": "application/json", "Authorization": "Bearer token"}

model = "mistralai/Mistral-Small-3.1-24B-Instruct-2503"


def load_system_prompt(repo_id: str, filename: str) -> str:
    file_path = hf_hub_download(repo_id=repo_id, filename=filename)
    with open(file_path, "r") as file:
        system_prompt = file.read()
    today = datetime.today().strftime("%Y-%m-%d")
    yesterday = (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    model_name = repo_id.split("/")[-1]
    return system_prompt.format(name=model_name, today=today, yesterday=yesterday)


SYSTEM_PROMPT = load_system_prompt(model, "SYSTEM_PROMPT.txt")

image_url = "https://huggingface.co/datasets/patrickvonplaten/random_img/resolve/main/europe.png"

messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "Which of the depicted countries has the best food? Which the second and third and fourth? Name the country, its color on the map and one its city that is visible on the map, but is not the capital. Make absolutely sure to only name a city that can be seen on the map.",
            },
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
    },
]

data = {"model": model, "messages": messages, "temperature": 0.15}

response = requests.post(url, headers=headers, data=json.dumps(data))
print(response.json()["choices"][0]["message"]["content"])
# Determining the "best" food is highly subjective and depends on personal preferences. However, based on general popularity and recognition, here are some countries known for their cuisine:

# 1. **Italy** - Color: Light Green - City: Milan
#    - Italian cuisine is renowned worldwide for its pasta, pizza, and various regional specialties.

# 2. **France** - Color: Brown - City: Lyon
#    - French cuisine is celebrated for its sophistication, including dishes like coq au vin, bouillabaisse, and pastries like croissants and éclairs.

# 3. **Spain** - Color: Yellow - City: Bilbao
#    - Spanish cuisine offers a variety of flavors, from paella and tapas to jamón ibérico and churros.

# 4. **Greece** - Not visible on the map
#    - Greek cuisine is known for dishes like moussaka, souvlaki, and baklava. Unfortunately, Greece is not visible on the provided map, so I cannot name a city.

# Since Greece is not visible on the map, I'll replace it with another country known for its good food:

# 4. **Turkey** - Color: Light Green (east part of the map) - City: Istanbul
#    - Turkish cuisine is diverse and includes dishes like kebabs, meze, and baklava.
```

### Function calling

Mistral-Small-3.1-24-Instruct-2503 is excellent at function / tool calling tasks via vLLM. *E.g.:*

<details>
  <summary>Example</summary>

```py
import requests
import json
from huggingface_hub import hf_hub_download
from datetime import datetime, timedelta

url = "http://<your-url>:8000/v1/chat/completions"
headers = {"Content-Type": "application/json", "Authorization": "Bearer token"}

model = "mistralai/Mistral-Small-3.1-24B-Instruct-2503"


def load_system_prompt(repo_id: str, filename: str) -> str:
    file_path = hf_hub_download(repo_id=repo_id, filename=filename)
    with open(file_path, "r") as file:
        system_prompt = file.read()
    today = datetime.today().strftime("%Y-%m-%d")
    yesterday = (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    model_name = repo_id.split("/")[-1]
    return system_prompt.format(name=model_name, today=today, yesterday=yesterday)


SYSTEM_PROMPT = load_system_prompt(model, "SYSTEM_PROMPT.txt")


tools = [
    {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "Get the current weather in a given location",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "The city to find the weather for, e.g. 'San Francisco'",
                    },
                    "state": {
                        "type": "string",
                        "description": "The state abbreviation, e.g. 'CA' for California",
                    },
                    "unit": {
                        "type": "string",
                        "description": "The unit for temperature",
                        "enum": ["celsius", "fahrenheit"],
                    },
                },
                "required": ["city", "state", "unit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rewrite",
            "description": "Rewrite a given text for improved clarity",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The input text to rewrite",
                    }
                },
            },
        },
    },
]

messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {
        "role": "user",
        "content": "Could you please make the below article more concise?\n\nOpenAI is an artificial intelligence research laboratory consisting of the non-profit OpenAI Incorporated and its for-profit subsidiary corporation OpenAI Limited Partnership.",
    },
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "bbc5b7ede",
                "type": "function",
                "function": {
                    "name": "rewrite",
                    "arguments": '{"text": "OpenAI is an artificial intelligence research laboratory consisting of the non-profit OpenAI Incorporated and its for-profit subsidiary corporation OpenAI Limited Partnership."}',
                },
            }
        ],
    },
    {
        "role": "tool",
        "content": '{"action":"rewrite","outcome":"OpenAI is a FOR-profit company."}',
        "tool_call_id": "bbc5b7ede",
        "name": "rewrite",
    },
    {
        "role": "assistant",
        "content": "---\n\nOpenAI is a FOR-profit company.",
    },
    {
        "role": "user",
        "content": "Can you tell me what the temperature will be in Dallas, in Fahrenheit?",
    },
]

data = {"model": model, "messages": messages, "tools": tools, "temperature": 0.15}

response = requests.post(url, headers=headers, data=json.dumps(data))
print(response.json()["choices"][0]["message"]["tool_calls"])
# [{'id': '8PdihwL6d', 'type': 'function', 'function': {'name': 'get_current_weather', 'arguments': '{"city": "Dallas", "state": "TX", "unit": "fahrenheit"}'}}]
```

</details>

#### Offline

```py
from vllm import LLM
from vllm.sampling_params import SamplingParams
from datetime import datetime, timedelta

SYSTEM_PROMPT = "You are a conversational agent that always answers straight to the point, always end your accurate response with an ASCII drawing of a cat."

user_prompt = "Give me 5 non-formal ways to say 'See you later' in French."

messages = [
    {
        "role": "system",
        "content": SYSTEM_PROMPT
    },
    {
        "role": "user",
        "content": user_prompt
    },
]
model_name = "mistralai/Mistral-Small-3.1-24B-Instruct-2503"
# note that running this model on GPU requires over 60 GB of GPU RAM
llm = LLM(model=model_name, tokenizer_mode="mistral")

sampling_params = SamplingParams(max_tokens=512, temperature=0.15)
outputs = llm.chat(messages, sampling_params=sampling_params)

print(outputs[0].outputs[0].text)
# Here are five non-formal ways to say "See you later" in French:

# 1. **À plus tard** - Until later
# 2. **À toute** - See you soon (informal)
# 3. **Salut** - Bye (can also mean hi)
# 4. **À plus** - See you later (informal)
# 5. **Ciao** - Bye (informal, borrowed from Italian)

# ```
#  /\_/\
# ( o.o )
#  > ^ <
# ```
```

### Transformers (untested)

Transformers-compatible model weights are also uploaded (thanks a lot @cyrilvallez). 
However the transformers implementation was **not throughly tested**, but only on "vibe-checks".
Hence, we can only ensure 100% correct behavior when using the original weight format with vllm (see above).