---
license: apache-2.0
pipeline_tag: text-generation
---
# InternLM 



<div align="center">
<img src="https://github.com/InternLM/InternLM/assets/22529082/b9788105-8892-4398-8b47-b513a292378e" width="200"/>

  <div>&nbsp;</div>
  <div align="center">
    <b><font size="5">InternLM</font></b>
    <sup>
      <a href="https://internlm.intern-ai.org.cn/">
        <i><font size="4">HOT</font></i>
      </a>
    </sup>
    <div>&nbsp;</div>
  </div>


[![evaluation](https://github.com/InternLM/InternLM/assets/22529082/f80a2a58-5ddf-471a-8da4-32ab65c8fd3b)](https://github.com/internLM/OpenCompass/)

[💻Github Repo](https://github.com/InternLM/InternLM) • [🤗Demo](https://huggingface.co/spaces/internlm/internlm3-8b-instruct) • [🤔Reporting Issues](https://github.com/InternLM/InternLM/issues/new) • [📜Technical Report](https://arxiv.org/abs/2403.17297)

</div>

<p align="center">
    👋 join us on <a href="https://discord.gg/xa29JuW87d" target="_blank">Discord</a> and <a href="https://github.com/InternLM/InternLM/assets/25839884/a6aad896-7232-4220-ac84-9e070c2633ce" target="_blank">WeChat</a>
</p>



## Introduction 

InternLM3 has open-sourced an 8-billion parameter instruction model, InternLM3-8B-Instruct, designed for general-purpose usage and advanced reasoning. This model has the following characteristics:

- **Enhanced performance at reduced cost**: 
State-of-the-art performance on reasoning and knowledge-intensive tasks surpass models like Llama3.1-8B and Qwen2.5-7B. Remarkably, InternLM3 is trained on only 4 trillion high-quality tokens, saving more than 75% of the training cost compared to other LLMs of similar scale. 
- **Deep thinking capability**:
InternLM3 supports both the deep thinking mode for solving complicated reasoning tasks via the long chain-of-thought and the normal response mode for fluent user interactions. 

## InternLM3-8B-Instruct

### Performance Evaluation

We conducted a comprehensive evaluation of InternLM using the open-source evaluation tool [OpenCompass](https://github.com/internLM/OpenCompass/). The evaluation covered five dimensions of capabilities: disciplinary competence, language competence, knowledge competence, inference competence, and comprehension competence. Here are some of the evaluation results, and you can visit the [OpenCompass leaderboard](https://rank.opencompass.org.cn) for more evaluation results.

|              | Benchmark                       | InternLM3-8B-Instruct | Qwen2.5-7B-Instruct | Llama3.1-8B-Instruct | GPT-4o-mini(closed source) |
| ------------ | ------------------------------- | --------------------- | ------------------- | -------------------- | -------------------------- |
| General      | CMMLU(0-shot)                   | **83.1**              | 75.8                | 53.9                 | 66.0                       |
|              | MMLU(0-shot)                    | 76.6                  | **76.8**            | 71.8                 | 82.7                       |
|              | MMLU-Pro(0-shot)                | **57.6**              | 56.2                | 48.1                 | 64.1                       |
| Reasoning    | GPQA-Diamond(0-shot)            | **37.4**              | 33.3                | 24.2                 | 42.9                       |
|              | DROP(0-shot)                    | **83.1**              | 80.4                | 81.6                 | 85.2                       |
|              | HellaSwag(10-shot)              | **91.2**              | 85.3                | 76.7                 | 89.5                       |
|              | KOR-Bench(0-shot)               | **56.4**              | 44.6                | 47.7                 | 58.2                       |
| MATH         | MATH-500(0-shot)                | **83.0***             | 72.4                | 48.4                 | 74.0                       |
|              | AIME2024(0-shot)                | **20.0***             | 16.7                | 6.7                  | 13.3                       |
| Coding       | LiveCodeBench(2407-2409 Pass@1) | **17.8**              | 16.8                | 12.9                 | 21.8                       |
|              | HumanEval(Pass@1)               | 82.3                  | **85.4**            | 72.0                 | 86.6                       |
| Instrunction | IFEval(Prompt-Strict)           | **79.3**              | 71.7                | 75.2                 | 79.7                       |
| Long Context | RULER(4-128K Average)           | 87.9                  | 81.4                | **88.5**             | 90.7                       |
| Chat         | AlpacaEval 2.0(LC WinRate)      | **51.1**              | 30.3                | 25.0                 | 50.7                       |
|              | WildBench(Raw Score)            | **33.1**              | 23.3                | 1.5                  | 40.3                       |
|              | MT-Bench-101(Score 1-10)        | **8.59**              | 8.49                | 8.37                 | 8.87                       |

- Values marked in bold indicate the **highest** in open source models
- The evaluation results were obtained from [OpenCompass](https://github.com/internLM/OpenCompass/) (some data marked with *, which means evaluating with Thinking Mode), and evaluation configuration can be found in the configuration files provided by [OpenCompass](https://github.com/internLM/OpenCompass/). 
- The evaluation data may have numerical differences due to the version iteration of [OpenCompass](https://github.com/internLM/OpenCompass/), so please refer to the latest evaluation results of [OpenCompass](https://github.com/internLM/OpenCompass/).

**Limitations:** Although we have made efforts to ensure the safety of the model during the training process and to encourage the model to generate text that complies with ethical and legal requirements, the model may still produce unexpected outputs due to its size and probabilistic generation paradigm. For example, the generated responses may contain biases, discrimination, or other harmful content. Please do not propagate such content. We are not responsible for any consequences resulting from the dissemination of harmful information.
### Requirements
```python
transformers >= 4.48
```


### Conversation Mode

#### Transformers inference

To load the InternLM3 8B Instruct model using Transformers, use the following code:

```python
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

model_dir = "internlm/internlm3-8b-instruct"
tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)
# Set `torch_dtype=torch.float16` to load model in float16, otherwise it will be loaded as float32 and might cause OOM Error.
model = AutoModelForCausalLM.from_pretrained(model_dir, trust_remote_code=True, torch_dtype=torch.bfloat16).cuda()
# (Optional) If on low resource devices, you can load model in 4-bit or 8-bit to further save GPU memory via bitsandbytes.
  # InternLM3 8B in 4bit will cost nearly 8GB GPU memory.
  # pip install -U bitsandbytes
  # 8-bit: model = AutoModelForCausalLM.from_pretrained(model_dir, device_map="auto", trust_remote_code=True, load_in_8bit=True)
  # 4-bit: model = AutoModelForCausalLM.from_pretrained(model_dir, device_map="auto", trust_remote_code=True, load_in_4bit=True)
model = model.eval()

system_prompt = """You are an AI assistant whose name is InternLM (书生·浦语).
- InternLM (书生·浦语) is a conversational language model that is developed by Shanghai AI Laboratory (上海人工智能实验室). It is designed to be helpful, honest, and harmless.
- InternLM (书生·浦语) can understand and communicate fluently in the language chosen by the user such as English and 中文."""
messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": "Please tell me five scenic spots in Shanghai"},
 ]
tokenized_chat = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, return_tensors="pt").to("cuda")

generated_ids = model.generate(tokenized_chat, max_new_tokens=1024, temperature=1, repetition_penalty=1.005, top_k=40, top_p=0.8)

generated_ids = [
    output_ids[len(input_ids):] for input_ids, output_ids in zip(tokenized_chat, generated_ids)
]
prompt = tokenizer.batch_decode(tokenized_chat)[0]
print(prompt)
response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]
print(response)
```

#### LMDeploy inference
LMDeploy is a toolkit for compressing, deploying, and serving LLM, developed by the MMRazor and MMDeploy teams.

```bash
pip install lmdeploy
```

You can run batch inference locally with the following python code:

```python
import lmdeploy
model_dir = "internlm/internlm3-8b-instruct"
pipe = lmdeploy.pipeline(model_dir)
response = pipe("Please tell me five scenic spots in Shanghai")
print(response)

```

Or you can launch an OpenAI compatible server with the following command:

```bash
lmdeploy serve api_server internlm/internlm3-8b-instruct --model-name internlm3-8b-instruct --server-port 23333 
```

Then you can send a chat request to the server:

```bash
curl http://localhost:23333/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
    "model": "internlm3-8b-instruct",
    "messages": [
    {"role": "user", "content": "Please tell me five scenic spots in Shanghai"}
    ]
    }'
```

Find more details in the [LMDeploy documentation](https://lmdeploy.readthedocs.io/en/latest/)



####  Ollama inference

First install ollama,

```python
# install ollama
curl -fsSL https://ollama.com/install.sh | sh
# fetch model
ollama pull internlm/internlm3-8b-instruct
# install 
pip install ollama
```

inference code,

```python
import ollama

system_prompt = """You are an AI assistant whose name is InternLM (书生·浦语).
- InternLM (书生·浦语) is a conversational language model that is developed by Shanghai AI Laboratory (上海人工智能实验室). It is designed to be helpful, honest, and harmless.
- InternLM (书生·浦语) can understand and communicate fluently in the language chosen by the user such as English and 中文."""

messages = [
    {
        "role": "system",
        "content": system_prompt,
    },
    {
        "role": "user",
        "content": "Please tell me five scenic spots in Shanghai"
    },
]

stream = ollama.chat(
    model='internlm/internlm3-8b-instruct',
    messages=messages,
    stream=True,
)

for chunk in stream:
  print(chunk['message']['content'], end='', flush=True)
```


#### vLLM inference

Refer to [installation](https://docs.vllm.ai/en/latest/getting_started/installation/index.html) to install the latest code of vllm

```python
pip install vllm --pre --extra-index-url https://wheels.vllm.ai/nightly
```

inference code:

```python
from vllm import LLM, SamplingParams

llm = LLM(model="internlm/internlm3-8b-instruct")
sampling_params = SamplingParams(temperature=1, repetition_penalty=1.005, top_k=40, top_p=0.8)

system_prompt = """You are an AI assistant whose name is InternLM (书生·浦语).
- InternLM (书生·浦语) is a conversational language model that is developed by Shanghai AI Laboratory (上海人工智能实验室). It is designed to be helpful, honest, and harmless.
- InternLM (书生·浦语) can understand and communicate fluently in the language chosen by the user such as English and 中文."""

prompts = [
    {
        "role": "system",
        "content": system_prompt,
    },
    {
        "role": "user",
        "content": "Please tell me five scenic spots in Shanghai"
    },
]
outputs = llm.chat(prompts,
                   sampling_params=sampling_params,
                   use_tqdm=False)
print(outputs)
```





### Thinking Mode
#### Thinking Demo

<img src="https://github.com/InternLM/InternLM/blob/017ba7446d20ecc3b9ab8e7b66cc034500868ab4/assets/solve_puzzle.png?raw=true" width="400"/>







#### Thinking system prompt
```python
thinking_system_prompt = """You are an expert mathematician with extensive experience in mathematical competitions. You approach problems through systematic thinking and rigorous reasoning. When solving problems, follow these thought processes:
## Deep Understanding
Take time to fully comprehend the problem before attempting a solution. Consider:
- What is the real question being asked?
- What are the given conditions and what do they tell us?
- Are there any special restrictions or assumptions?
- Which information is crucial and which is supplementary?
## Multi-angle Analysis
Before solving, conduct thorough analysis:
- What mathematical concepts and properties are involved?
- Can you recall similar classic problems or solution methods?
- Would diagrams or tables help visualize the problem?
- Are there special cases that need separate consideration?
## Systematic Thinking
Plan your solution path:
- Propose multiple possible approaches
- Analyze the feasibility and merits of each method
- Choose the most appropriate method and explain why
- Break complex problems into smaller, manageable steps
## Rigorous Proof
During the solution process:
- Provide solid justification for each step
- Include detailed proofs for key conclusions
- Pay attention to logical connections
- Be vigilant about potential oversights
## Repeated Verification
After completing your solution:
- Verify your results satisfy all conditions
- Check for overlooked special cases
- Consider if the solution can be optimized or simplified
- Review your reasoning process
Remember:
1. Take time to think thoroughly rather than rushing to an answer
2. Rigorously prove each key conclusion
3. Keep an open mind and try different approaches
4. Summarize valuable problem-solving methods
5. Maintain healthy skepticism and verify multiple times
Your response should reflect deep mathematical understanding and precise logical thinking, making your solution path and reasoning clear to others.
When you're ready, present your complete solution with:
- Clear problem understanding
- Detailed solution process
- Key insights
- Thorough verification
Focus on clear, logical progression of ideas and thorough explanation of your mathematical reasoning. Provide answers in the same language as the user asking the question, repeat the final answer using a '\\boxed{}' without any units, you have [[8192]] tokens to complete the answer.
"""
```
#### Transformers inference
```python
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

model_dir = "internlm/internlm3-8b-instruct"
tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)
# Set `torch_dtype=torch.float16` to load model in float16, otherwise it will be loaded as float32 and might cause OOM Error.
model = AutoModelForCausalLM.from_pretrained(model_dir, trust_remote_code=True, torch_dtype=torch.bfloat16).cuda()
# (Optional) If on low resource devices, you can load model in 4-bit or 8-bit to further save GPU memory via bitsandbytes.
  # InternLM3 8B in 4bit will cost nearly 8GB GPU memory.
  # pip install -U bitsandbytes
  # 8-bit: model = AutoModelForCausalLM.from_pretrained(model_dir, device_map="auto", trust_remote_code=True, load_in_8bit=True)
  # 4-bit: model = AutoModelForCausalLM.from_pretrained(model_dir, device_map="auto", trust_remote_code=True, load_in_4bit=True)
model = model.eval()

messages = [
    {"role": "system", "content": thinking_system_prompt},
    {"role": "user", "content": "Given the function\(f(x)=\mathrm{e}^{x}-ax - a^{3}\),\n(1) When \(a = 1\), find the equation of the tangent line to the curve \(y = f(x)\) at the point \((1,f(1))\).\n(2) If \(f(x)\) has a local minimum and the minimum value is less than \(0\), determine the range of values for \(a\)."},
 ]
tokenized_chat = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, return_tensors="pt").to("cuda")

generated_ids = model.generate(tokenized_chat, max_new_tokens=8192)

generated_ids = [
    output_ids[len(input_ids):] for input_ids, output_ids in zip(tokenized_chat, generated_ids)
]
prompt = tokenizer.batch_decode(tokenized_chat)[0]
print(prompt)
response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]
print(response)
```
#### LMDeploy inference

LMDeploy is a toolkit for compressing, deploying, and serving LLM.

```bash
pip install lmdeploy
```

You can run batch inference locally with the following python code:

```python
from lmdeploy import pipeline, GenerationConfig, ChatTemplateConfig
model_dir = "internlm/internlm3-8b-instruct"
chat_template_config = ChatTemplateConfig(model_name='internlm3')
pipe = pipeline(model_dir, chat_template_config=chat_template_config)

messages = [
        {"role": "system", "content": thinking_system_prompt},
        {"role": "user", "content": "Given the function\(f(x)=\mathrm{e}^{x}-ax - a^{3}\),\n(1) When \(a = 1\), find the equation of the tangent line to the curve \(y = f(x)\) at the point \((1,f(1))\).\n(2) If \(f(x)\) has a local minimum and the minimum value is less than \(0\), determine the range of values for \(a\)."},
]

response = pipe(messages, gen_config=GenerationConfig(max_new_tokens=2048))
print(response)
```

####  Ollama inference

First install ollama,

```python
# install ollama
curl -fsSL https://ollama.com/install.sh | sh
# fetch model
ollama pull internlm/internlm3-8b-instruct
# install
pip install ollama
```

inference code,

```python
import ollama

messages = [
    {
        "role": "system",
        "content": thinking_system_prompt,
    },
    {
        "role": "user",
        "content": "Given the function\(f(x)=\mathrm{e}^{x}-ax - a^{3}\),\n(1) When \(a = 1\), find the equation of the tangent line to the curve \(y = f(x)\) at the point \((1,f(1))\).\n(2) If \(f(x)\) has a local minimum and the minimum value is less than \(0\), determine the range of values for \(a\)."
    },
]

stream = ollama.chat(
    model='internlm/internlm3-8b-instruct',
    messages=messages,
    stream=True,
)

for chunk in stream:
  print(chunk['message']['content'], end='', flush=True)
```


#### 

#### vLLM inference

Refer to [installation](https://docs.vllm.ai/en/latest/getting_started/installation/index.html) to install the latest code of vllm

```python
pip install vllm --pre --extra-index-url https://wheels.vllm.ai/nightly
```

inference code


```python
from vllm import LLM, SamplingParams

llm = LLM(model="internlm/internlm3-8b-instruct")
sampling_params = SamplingParams(temperature=1, repetition_penalty=1.005, top_k=40, top_p=0.8, max_tokens=8192)

prompts = [
    {
        "role": "system",
        "content": thinking_system_prompt,
    },
    {
        "role": "user",
        "content": "Given the function\(f(x)=\mathrm{e}^{x}-ax - a^{3}\),\n(1) When \(a = 1\), find the equation of the tangent line to the curve \(y = f(x)\) at the point \((1,f(1))\).\n(2) If \(f(x)\) has a local minimum and the minimum value is less than \(0\), determine the range of values for \(a\)."
    },
]
outputs = llm.chat(prompts,
                   sampling_params=sampling_params,
                   use_tqdm=False)
print(outputs)
```



## Open Source License

Code and model weights are licensed under Apache-2.0. 

## Citation

```
@misc{cai2024internlm2,
      title={InternLM2 Technical Report},
      author={Zheng Cai and Maosong Cao and Haojiong Chen and Kai Chen and Keyu Chen and Xin Chen and Xun Chen and Zehui Chen and Zhi Chen and Pei Chu and Xiaoyi Dong and Haodong Duan and Qi Fan and Zhaoye Fei and Yang Gao and Jiaye Ge and Chenya Gu and Yuzhe Gu and Tao Gui and Aijia Guo and Qipeng Guo and Conghui He and Yingfan Hu and Ting Huang and Tao Jiang and Penglong Jiao and Zhenjiang Jin and Zhikai Lei and Jiaxing Li and Jingwen Li and Linyang Li and Shuaibin Li and Wei Li and Yining Li and Hongwei Liu and Jiangning Liu and Jiawei Hong and Kaiwen Liu and Kuikun Liu and Xiaoran Liu and Chengqi Lv and Haijun Lv and Kai Lv and Li Ma and Runyuan Ma and Zerun Ma and Wenchang Ning and Linke Ouyang and Jiantao Qiu and Yuan Qu and Fukai Shang and Yunfan Shao and Demin Song and Zifan Song and Zhihao Sui and Peng Sun and Yu Sun and Huanze Tang and Bin Wang and Guoteng Wang and Jiaqi Wang and Jiayu Wang and Rui Wang and Yudong Wang and Ziyi Wang and Xingjian Wei and Qizhen Weng and Fan Wu and Yingtong Xiong and Chao Xu and Ruiliang Xu and Hang Yan and Yirong Yan and Xiaogui Yang and Haochen Ye and Huaiyuan Ying and Jia Yu and Jing Yu and Yuhang Zang and Chuyu Zhang and Li Zhang and Pan Zhang and Peng Zhang and Ruijie Zhang and Shuo Zhang and Songyang Zhang and Wenjian Zhang and Wenwei Zhang and Xingcheng Zhang and Xinyue Zhang and Hui Zhao and Qian Zhao and Xiaomeng Zhao and Fengzhe Zhou and Zaida Zhou and Jingming Zhuo and Yicheng Zou and Xipeng Qiu and Yu Qiao and Dahua Lin},
      year={2024},
      eprint={2403.17297},
      archivePrefix={arXiv},
      primaryClass={cs.CL}
}
```



## 简介

### InternLM3-8B-Instruct

InternLM3，即书生·浦语大模型第3代，开源了80亿参数，面向通用使用与高阶推理的指令模型（InternLM3-8B-Instruct）。模型具备以下特点：

- **更低的代价取得更高的性能**:
在推理、知识类任务上取得同量级最优性能，超过Llama3.1-8B和Qwen2.5-7B。值得关注的是InternLM3只用了4万亿词元进行训练，对比同级别模型训练成本节省75%以上。
- **深度思考能力**:
InternLM3支持通过长思维链求解复杂推理任务的深度思考模式，同时还兼顾了用户体验更流畅的通用回复模式。

#### 性能评测

我们使用开源评测工具 [OpenCompass](https://github.com/internLM/OpenCompass/) 从学科综合能力、语言能力、知识能力、推理能力、理解能力五大能力维度对InternLM开展全面评测，部分评测结果如下表所示，欢迎访问[ OpenCompass 榜单 ](https://rank.opencompass.org.cn)获取更多的评测结果。

|              | 评测集\模型                     | InternLM3-8B-Instruct | Qwen2.5-7B-Instruct | Llama3.1-8B-Instruct | GPT-4o-mini(闭源) |
| ------------ | ------------------------------- | --------------------- | ------------------- | -------------------- | ----------------- |
| General      | CMMLU(0-shot)                   | **83.1**              | 75.8                | 53.9                 | 66.0              |
|              | MMLU(0-shot)                    | 76.6                  | **76.8**            | 71.8                 | 82.7              |
|              | MMLU-Pro(0-shot)                | **57.6**              | 56.2                | 48.1                 | 64.1              |
| Reasoning    | GPQA-Diamond(0-shot)            | **37.4**              | 33.3                | 24.2                 | 42.9              |
|              | DROP(0-shot)                    | **83.1**              | 80.4                | 81.6                 | 85.2              |
|              | HellaSwag(10-shot)              | **91.2**              | 85.3                | 76.7                 | 89.5              |
|              | KOR-Bench(0-shot)               | **56.4**              | 44.6                | 47.7                 | 58.2              |
| MATH         | MATH-500(0-shot)                | **83.0***             | 72.4                | 48.4                 | 74.0              |
|              | AIME2024(0-shot)                | **20.0***             | 16.7                | 6.7                  | 13.3              |
| Coding       | LiveCodeBench(2407-2409 Pass@1) | **17.8**              | 16.8                | 12.9                 | 21.8              |
|              | HumanEval(Pass@1)               | 82.3                  | **85.4**            | 72.0                 | 86.6              |
| Instrunction | IFEval(Prompt-Strict)           | **79.3**              | 71.7                | 75.2                 | 79.7              |
| LongContext  | RULER(4-128K Average)           | 87.9                  | 81.4                | **88.5**             | 90.7              |
| Chat         | AlpacaEval 2.0(LC WinRate)      | **51.1**              | 30.3                | 25.0                 | 50.7              |
|              | WildBench(Raw Score)            | **33.1**              | 23.3                | 1.5                  | 40.3              |
|              | MT-Bench-101(Score 1-10)        | **8.59**              | 8.49                | 8.37                 | 8.87              |

- 表中标粗的数值表示在对比的开源模型中的最高值。
- 以上评测结果基于 [OpenCompass](https://github.com/internLM/OpenCompass/) 获得(部分数据标注`*`代表使用深度思考模式进行评测)，具体测试细节可参见 [OpenCompass](https://github.com/internLM/OpenCompass/) 中提供的配置文件。
- 评测数据会因 [OpenCompass](https://github.com/internLM/OpenCompass/) 的版本迭代而存在数值差异，请以 [OpenCompass](https://github.com/internLM/OpenCompass/) 最新版的评测结果为主。

**局限性：** 尽管在训练过程中我们非常注重模型的安全性，尽力促使模型输出符合伦理和法律要求的文本，但受限于模型大小以及概率生成范式，模型可能会产生各种不符合预期的输出，例如回复内容包含偏见、歧视等有害内容，请勿传播这些内容。由于传播不良信息导致的任何后果，本项目不承担责任。

#### 依赖

```python
transformers >= 4.48
```




#### 常规对话模式

##### Transformers 推理

通过以下的代码加载  InternLM3 8B Instruct 模型

```python
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

model_dir = "internlm/internlm3-8b-instruct"
tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)
# Set `torch_dtype=torch.float16` to load model in float16, otherwise it will be loaded as float32 and might cause OOM Error.
model = AutoModelForCausalLM.from_pretrained(model_dir, trust_remote_code=True, torch_dtype=torch.bfloat16).cuda()
# (Optional) If on low resource devices, you can load model in 4-bit or 8-bit to further save GPU memory via bitsandbytes.
  # InternLM3 8B in 4bit will cost nearly 8GB GPU memory.
  # pip install -U bitsandbytes
  # 8-bit: model = AutoModelForCausalLM.from_pretrained(model_dir, device_map="auto", trust_remote_code=True, load_in_8bit=True)
  # 4-bit: model = AutoModelForCausalLM.from_pretrained(model_dir, device_map="auto", trust_remote_code=True, load_in_4bit=True)
model = model.eval()

system_prompt = """You are an AI assistant whose name is InternLM (书生·浦语).
- InternLM (书生·浦语) is a conversational language model that is developed by Shanghai AI Laboratory (上海人工智能实验室). It is designed to be helpful, honest, and harmless.
- InternLM (书生·浦语) can understand and communicate fluently in the language chosen by the user such as English and 中文."""
messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": "Please tell me five scenic spots in Shanghai"},
 ]
tokenized_chat = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, return_tensors="pt").to("cuda")

generated_ids = model.generate(tokenized_chat, max_new_tokens=1024, temperature=1, repetition_penalty=1.005, top_k=40, top_p=0.8)

generated_ids = [
    output_ids[len(input_ids):] for input_ids, output_ids in zip(tokenized_chat, generated_ids)
]
prompt = tokenizer.batch_decode(tokenized_chat)[0]
print(prompt)
response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]
print(response)
```

##### LMDeploy 推理

LMDeploy 是涵盖了 LLM 任务的全套轻量化、部署和服务解决方案。

```bash
pip install lmdeploy
```

你可以使用以下 python 代码进行本地批量推理:

```python
import lmdeploy
model_dir = "internlm/internlm3-8b-instruct"
pipe = lmdeploy.pipeline(model_dir)
response = pipe(["Please tell me five scenic spots in Shanghai"])
print(response)

```

或者你可以使用以下命令启动兼容 OpenAI API 的服务:

```bash
lmdeploy serve api_server internlm/internlm3-8b-instruct --model-name internlm3-8b-instruct --server-port 23333 
```

然后你可以向服务端发起一个聊天请求:

```bash
curl http://localhost:23333/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
    "model": "internlm3-8b-instruct",
    "messages": [
    {"role": "user", "content": "介绍一下深度学习。"}
    ]
    }'
```

更多信息请查看 [LMDeploy 文档](https://lmdeploy.readthedocs.io/en/latest/)



#####  Ollama 推理

准备工作

```python
# install ollama
curl -fsSL https://ollama.com/install.sh | sh
# fetch 模型
ollama pull internlm/internlm3-8b-instruct
# install python库
pip install ollama
```

推理代码

```python
import ollama

system_prompt = """You are an AI assistant whose name is InternLM (书生·浦语).
- InternLM (书生·浦语) is a conversational language model that is developed by Shanghai AI Laboratory (上海人工智能实验室). It is designed to be helpful, honest, and harmless.
- InternLM (书生·浦语) can understand and communicate fluently in the language chosen by the user such as English and 中文."""

messages = [
    {
        "role": "system",
        "content": system_prompt,
    },
    {
        "role": "user",
        "content": "Please tell me five scenic spots in Shanghai"
    },
]

stream = ollama.chat(
    model='internlm/internlm3-8b-instruct',
    messages=messages,
    stream=True,
)

for chunk in stream:
  print(chunk['message']['content'], end='', flush=True)
```


#### 

##### vLLM 推理

参考[文档](https://docs.vllm.ai/en/latest/getting_started/installation/index.html) 安装 vllm 最新代码

```bash
pip install vllm --pre --extra-index-url https://wheels.vllm.ai/nightly
```

推理代码

```python
from vllm import LLM, SamplingParams

llm = LLM(model="internlm/internlm3-8b-instruct")
sampling_params = SamplingParams(temperature=1, repetition_penalty=1.005, top_k=40, top_p=0.8)

system_prompt = """You are an AI assistant whose name is InternLM (书生·浦语).
- InternLM (书生·浦语) is a conversational language model that is developed by Shanghai AI Laboratory (上海人工智能实验室). It is designed to be helpful, honest, and harmless.
- InternLM (书生·浦语) can understand and communicate fluently in the language chosen by the user such as English and 中文."""

prompts = [
    {
        "role": "system",
        "content": system_prompt,
    },
    {
        "role": "user",
        "content": "Please tell me five scenic spots in Shanghai"
    },
]
outputs = llm.chat(prompts,
                   sampling_params=sampling_params,
                   use_tqdm=False)
print(outputs)
```

#### 深度思考模式

##### 深度思考 Demo

<img src="https://github.com/InternLM/InternLM/blob/017ba7446d20ecc3b9ab8e7b66cc034500868ab4/assets/solve_puzzle.png?raw=true" width="400"/>





##### 深度思考 system prompt

```python
thinking_system_prompt = """You are an expert mathematician with extensive experience in mathematical competitions. You approach problems through systematic thinking and rigorous reasoning. When solving problems, follow these thought processes:
## Deep Understanding
Take time to fully comprehend the problem before attempting a solution. Consider:
- What is the real question being asked?
- What are the given conditions and what do they tell us?
- Are there any special restrictions or assumptions?
- Which information is crucial and which is supplementary?
## Multi-angle Analysis
Before solving, conduct thorough analysis:
- What mathematical concepts and properties are involved?
- Can you recall similar classic problems or solution methods?
- Would diagrams or tables help visualize the problem?
- Are there special cases that need separate consideration?
## Systematic Thinking
Plan your solution path:
- Propose multiple possible approaches
- Analyze the feasibility and merits of each method
- Choose the most appropriate method and explain why
- Break complex problems into smaller, manageable steps
## Rigorous Proof
During the solution process:
- Provide solid justification for each step
- Include detailed proofs for key conclusions
- Pay attention to logical connections
- Be vigilant about potential oversights
## Repeated Verification
After completing your solution:
- Verify your results satisfy all conditions
- Check for overlooked special cases
- Consider if the solution can be optimized or simplified
- Review your reasoning process
Remember:
1. Take time to think thoroughly rather than rushing to an answer
2. Rigorously prove each key conclusion
3. Keep an open mind and try different approaches
4. Summarize valuable problem-solving methods
5. Maintain healthy skepticism and verify multiple times
Your response should reflect deep mathematical understanding and precise logical thinking, making your solution path and reasoning clear to others.
When you're ready, present your complete solution with:
- Clear problem understanding
- Detailed solution process
- Key insights
- Thorough verification
Focus on clear, logical progression of ideas and thorough explanation of your mathematical reasoning. Provide answers in the same language as the user asking the question, repeat the final answer using a '\\boxed{}' without any units, you have [[8192]] tokens to complete the answer.
"""
```

##### Transformers 推理


```python
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

model_dir = "internlm/internlm3-8b-instruct"
tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)
# Set `torch_dtype=torch.float16` to load model in float16, otherwise it will be loaded as float32 and might cause OOM Error.
model = AutoModelForCausalLM.from_pretrained(model_dir, trust_remote_code=True, torch_dtype=torch.bfloat16).cuda()
# (Optional) If on low resource devices, you can load model in 4-bit or 8-bit to further save GPU memory via bitsandbytes.
  # InternLM3 8B in 4bit will cost nearly 8GB GPU memory.
  # pip install -U bitsandbytes
  # 8-bit: model = AutoModelForCausalLM.from_pretrained(model_dir, device_map="auto", trust_remote_code=True, load_in_8bit=True)
  # 4-bit: model = AutoModelForCausalLM.from_pretrained(model_dir, device_map="auto", trust_remote_code=True, load_in_4bit=True)
model = model.eval()

messages = [
    {"role": "system", "content": thinking_system_prompt},
    {"role": "user", "content": "已知函数\(f(x)=\mathrm{e}^{x}-ax - a^{3}\)。\n（1）当\(a = 1\)时，求曲线\(y = f(x)\)在点\((1,f(1))\)处的切线方程；\n（2）若\(f(x)\)有极小值，且极小值小于\(0\)，求\(a\)的取值范围。"},
 ]
tokenized_chat = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, return_tensors="pt").to("cuda")

generated_ids = model.generate(tokenized_chat, max_new_tokens=8192)

generated_ids = [
    output_ids[len(input_ids):] for input_ids, output_ids in zip(tokenized_chat, generated_ids)
]
prompt = tokenizer.batch_decode(tokenized_chat)[0]
print(prompt)
response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]
print(response)
```
##### LMDeploy 推理

LMDeploy is a toolkit for compressing, deploying, and serving LLM, developed by the MMRazor and MMDeploy teams.

```bash
pip install lmdeploy
```

You can run batch inference locally with the following python code:

```python
from lmdeploy import pipeline, GenerationConfig, ChatTemplateConfig
model_dir = "internlm/internlm3-8b-instruct"
chat_template_config = ChatTemplateConfig(model_name='internlm3')
pipe = pipeline(model_dir, chat_template_config=chat_template_config)

messages = [
        {"role": "system", "content": thinking_system_prompt},
        {"role": "user", "content": "已知函数\(f(x)=\mathrm{e}^{x}-ax - a^{3}\)。\n（1）当\(a = 1\)时，求曲线\(y = f(x)\)在点\((1,f(1))\)处的切线方程；\n（2）若\(f(x)\)有极小值，且极小值小于\(0\)，求\(a\)的取值范围。"},
]

response = pipe(messages, gen_config=GenerationConfig(max_new_tokens=2048))
print(response)
```

#####  Ollama 推理

准备工作

```python
# install ollama
curl -fsSL https://ollama.com/install.sh | sh
# fetch 模型
ollama pull internlm/internlm3-8b-instruct
# install python库
pip install ollama
```

inference code,

```python
import ollama

messages = [
    {
        "role": "system",
        "content": thinking_system_prompt,
    },
    {
        "role": "user",
        "content": "Given the function\(f(x)=\mathrm{e}^{x}-ax - a^{3}\),\n(1) When \(a = 1\), find the equation of the tangent line to the curve \(y = f(x)\) at the point \((1,f(1))\).\n(2) If \(f(x)\) has a local minimum and the minimum value is less than \(0\), determine the range of values for \(a\)."
    },
]

stream = ollama.chat(
    model='internlm/internlm3-8b-instruct',
    messages=messages,
    stream=True,
)

for chunk in stream:
  print(chunk['message']['content'], end='', flush=True)
```


#### 

##### vLLM 推理

参考[文档](https://docs.vllm.ai/en/latest/getting_started/installation/index.html) 安装 vllm 最新代码

```bash
pip install vllm --pre --extra-index-url https://wheels.vllm.ai/nightly
```

推理代码

```python
from vllm import LLM, SamplingParams

llm = LLM(model="internlm/internlm3-8b-instruct")
sampling_params = SamplingParams(temperature=1, repetition_penalty=1.005, top_k=40, top_p=0.8, max_tokens=8192)

prompts = [
    {
        "role": "system",
        "content": thinking_system_prompt,
    },
    {
        "role": "user",
        "content": "已知函数\(f(x)=\mathrm{e}^{x}-ax - a^{3}\)。\n（1）当\(a = 1\)时，求曲线\(y = f(x)\)在点\((1,f(1))\)处的切线方程；\n（2）若\(f(x)\)有极小值，且极小值小于\(0\)，求\(a\)的取值范围。"
    },
]
outputs = llm.chat(prompts,
                   sampling_params=sampling_params,
                   use_tqdm=False)
print(outputs)
```









## 开源许可证

本仓库的代码和权重依照 Apache-2.0 协议开源。

## 引用

```
@misc{cai2024internlm2,
      title={InternLM2 Technical Report},
      author={Zheng Cai and Maosong Cao and Haojiong Chen and Kai Chen and Keyu Chen and Xin Chen and Xun Chen and Zehui Chen and Zhi Chen and Pei Chu and Xiaoyi Dong and Haodong Duan and Qi Fan and Zhaoye Fei and Yang Gao and Jiaye Ge and Chenya Gu and Yuzhe Gu and Tao Gui and Aijia Guo and Qipeng Guo and Conghui He and Yingfan Hu and Ting Huang and Tao Jiang and Penglong Jiao and Zhenjiang Jin and Zhikai Lei and Jiaxing Li and Jingwen Li and Linyang Li and Shuaibin Li and Wei Li and Yining Li and Hongwei Liu and Jiangning Liu and Jiawei Hong and Kaiwen Liu and Kuikun Liu and Xiaoran Liu and Chengqi Lv and Haijun Lv and Kai Lv and Li Ma and Runyuan Ma and Zerun Ma and Wenchang Ning and Linke Ouyang and Jiantao Qiu and Yuan Qu and Fukai Shang and Yunfan Shao and Demin Song and Zifan Song and Zhihao Sui and Peng Sun and Yu Sun and Huanze Tang and Bin Wang and Guoteng Wang and Jiaqi Wang and Jiayu Wang and Rui Wang and Yudong Wang and Ziyi Wang and Xingjian Wei and Qizhen Weng and Fan Wu and Yingtong Xiong and Chao Xu and Ruiliang Xu and Hang Yan and Yirong Yan and Xiaogui Yang and Haochen Ye and Huaiyuan Ying and Jia Yu and Jing Yu and Yuhang Zang and Chuyu Zhang and Li Zhang and Pan Zhang and Peng Zhang and Ruijie Zhang and Shuo Zhang and Songyang Zhang and Wenjian Zhang and Wenwei Zhang and Xingcheng Zhang and Xinyue Zhang and Hui Zhao and Qian Zhao and Xiaomeng Zhao and Fengzhe Zhou and Zaida Zhou and Jingming Zhuo and Yicheng Zou and Xipeng Qiu and Yu Qiao and Dahua Lin},
      year={2024},
      eprint={2403.17297},
      archivePrefix={arXiv},
      primaryClass={cs.CL}
}
```