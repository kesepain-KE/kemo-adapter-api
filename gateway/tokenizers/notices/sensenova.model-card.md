---
base_model:
- OpenGVLab/InternVL3-8B
license: apache-2.0
pipeline_tag: image-text-to-text
library_name: transformers
---

**EN** | [中文](README_CN.md) 

# SenseNova-SI: Scaling Spatial Intelligence with Multimodal Foundation Models

<a href="https://github.com/OpenSenseNova/SenseNova-SI" target="_blank">
    <img alt="Code" src="https://img.shields.io/badge/SenseNova_SI-Code-100000?style=flat-square&logo=github&logoColor=white" height="20" />
</a>
  <a href="https://arxiv.org/abs/2511.13719" target="_blank">
      <img alt="arXiv" src="https://img.shields.io/badge/arXiv-SenseNova_SI-red?logo=arxiv" height="20" />
  </a>
<a href="https://github.com/EvolvingLMMs-Lab/EASI" target="_blank">
    <img alt="Code" src="https://img.shields.io/badge/EASI-Code-100000?style=flat-square&logo=github&logoColor=white" height="20" />
</a>
<a href="https://easi.lmms-lab.com/leaderboard" target="_blank">
    <img alt="Leaderboard" src="https://img.shields.io/badge/%F0%9F%A4%97%20_EASI-Leaderboard-ffc107?color=ffc107&logoColor=white" height="20" />
</a>

## Overview
Despite remarkable progress, multimodal foundation models still exhibit surprising deficiencies in spatial intelligence.
In this work, we explore scaling up multimodal foundation models to cultivate spatial intelligence within the **SenseNova-SI family**,
built upon established multimodal foundations including visual understanding models (i.e., Qwen3-VL and InternVL3) and unified understanding and generation models (i.e., Bagel).
We take a principled approach to constructing high-performing and robust spatial intelligence by systematically curating SenseNova-SI-8M:
eight million diverse data samples under a rigorous taxonomy of spatial capabilities.
SenseNova-SI demonstrates unprecedented performance across a broad range of spatial intelligence benchmarks, while maintaining strong general multimodal understanding.
More importantly, we analyze the impact of data scaling, discuss early signs of emergent generalization capabilities enabled by diverse data training,
analyze the risk of overfitting and language shortcuts, present a preliminary study on spatial chain-of-thought reasoning, and validate the potential downstream application. SenseNova-SI is an ongoing project, and this report will be updated continuously.
All newly trained multimodal foundation models are publicly released to facilitate further research in this direction.
*In the future, SenseNova-SI will be integrated with larger-scale in-house models.*

## Release Information
Currently, we build SenseNova-SI upon popular open-source foundation models to maximize compatibility with existing research pipelines.
In this release, we present 
[**SenseNova-SI-1.3-InternVL3-8B**](https://huggingface.co/sensenova/SenseNova-SI-1.3-InternVL3-8B),
[**SenseNova-SI-1.2-InternVL3-8B**](https://huggingface.co/sensenova/SenseNova-SI-1.2-InternVL3-8B),
[**SenseNova-SI-1.1-Qwen2.5-VL-3B**](https://huggingface.co/sensenova/SenseNova-SI-1.1-Qwen2.5-VL-3B),
[**SenseNova-SI-1.1-Qwen2.5-VL-7B**](https://huggingface.co/sensenova/SenseNova-SI-1.1-Qwen2.5-VL-7B), and
[**SenseNova-SI-1.1-Qwen3-VL-8B**](https://huggingface.co/sensenova/SenseNova-SI-1.1-Qwen3-VL-8B), of which **SenseNova-SI-1.3-InternVL3-8B** achieve state-of-the-art performance among open-source models of comparable size across eight recent spatial intelligence benchmarks: 
**VSI**, **MMSI**, **MindCube**, **ViewSpatial**, **SITE**, **BLINK**, **3DSRBench**, **EmbSpatial-Bench**. It also improves open-ended spatial question-answering capabilities compared to previous versions.

<table>
  <thead>
    <tr>
      <th>Model</th>
      <th>VSI</th>
      <th>MMSI</th>
      <th>MindCube-Tiny</th>
      <th>ViewSpatial</th>
      <th>SITE</th>
      <th>BLINK</th>
      <th>3DSRBench</th>
      <th>EmbSpatial-Bench</th>
    </tr>
  </thead>
  <tbody>
    <tr style="background:#F2F0EF;font-weight:700;text-align:center;">
      <td colspan="9"><em>Open-source Models (~2B)</em></td>
    </tr>
    <tr>
      <td>InternVL3-2B</td><td>32.9</td><td>26.5</td><td>37.5</td><td>32.5</td><td>30.0</td><td>50.8</td><td>47.7</td><td>60.1</td>
    </tr>
    <tr>
      <td>Qwen3-VL-2B-Instruct</td><td>50.3</td><td>28.9</td><td>34.5</td><td>36.9</td><td>35.6</td><td>53.2</td><td>47.5</td><td>70.1</td>
    </tr>
    <tr>
      <td>MindCube-3B-RawQA-SFT</td><td>17.2</td><td>1.7</td><td>51.7</td><td>24.1</td><td>6.3</td><td>35.1</td><td>2.8</td><td>37.0</td>
    </tr>
    <tr>
      <td>SpatialLadder-3B</td><td>44.8</td><td>27.4</td><td>43.4</td><td>39.8</td><td>27.9</td><td>43.0</td><td>42.8</td><td>58.2</td>
    </tr>
    <tr>
      <td>SpatialMLLM-4B</td><td>46.3</td><td>26.1</td><td>33.4</td><td>34.6</td><td>18.0</td><td>40.5</td><td>36.2</td><td>50.0</td>
    </tr>
    <tr>
      <td>VST-3B-SFT</td><td>57.9</td><td>30.2</td><td>35.9</td><td>52.8</td><td>35.8</td><td>58.8</td><td>54.1</td><td>69.0</td>
    </tr>
    <tr>
      <td>Cambrian-S-3B</td><td>57.3</td><td>25.2</td><td>32.5</td><td>39.0</td><td>28.3</td><td>37.7</td><td>50.9</td><td>63.5</td>
    </tr>
    <tr style="background:#F2F0EF;font-weight:700;text-align:center;">
      <td colspan="9"><em>Open-source Models (~8B)</em></td>
    </tr>
    <tr>
      <td>InternVL3-8B</td><td>42.1</td><td>28.0</td><td>41.5</td><td>38.6</td><td>41.1</td><td>53.5</td><td>44.3</td><td>76.4</td>
    </tr>
    <tr>
      <td>Qwen3-VL-8B-Instruct</td><td>57.9</td><td>31.1</td><td>29.4</td><td>42.2</td><td>45.8</td><td>66.7</td><td>53.9</td><td>77.7</td>
    </tr>
    <tr>
      <td>BAGEL-7B-MoT</td><td>31.4</td><td>31.0</td><td>34.7</td><td>41.3</td><td>37.0</td><td>63.7</td><td>50.2</td><td>73.1</td>
    </tr>
    <tr>
      <td>SpaceR-7B</td><td>41.5</td><td>27.4</td><td>37.9</td><td>35.8</td><td>34.2</td><td>49.6</td><td>40.5</td><td>66.9</td>
    </tr>
    <tr>
      <td>ViLaSR-7B</td><td>44.6</td><td>30.2</td><td>35.1</td><td>35.7</td><td>38.7</td><td>51.4</td><td>46.6</td><td>67.3</td>
    </tr>
    <tr>
      <td>VST-7B-SFT</td><td>60.6</td><td>32.0</td><td>39.7</td><td>50.5</td><td>39.6</td><td>61.9</td><td>54.6</td><td>73.7</td>
    </tr>
    <tr>
      <td>Cambrian-S-7B</td><td>67.5</td><td>25.8</td><td>39.6</td><td>40.9</td><td>33.0</td><td>37.9</td><td>54.8</td><td>72.8</td>
    </tr>
    <tr>
      <td><strong>SenseNova-SI-1.3-InternVL3-8B</strong></td>
      <td><strong>68.6</strong></td>
      <td><strong>42.5</strong></td>
      <td><strong>89.9</strong></td>
      <td><strong>61.3</strong></td>
      <td><strong>47.5</strong></td>
      <td><strong>68.0</strong></td>
      <td><strong>62.4</strong></td>
      <td><strong>81.0</strong></td>
    </tr>
    <tr style="background:#F2F0EF;color:#6b7280;font-weight:600;text-align:center;">
      <td colspan="9"><em>Proprietary Models</em></td>
    </tr>
    <tr style="color:#6b7280;">
      <td>Gemini-2.5-pro-2025-06</td><td>53.5</td><td>38.0</td><td>57.6</td><td>46.0</td><td>57.0</td><td>73.5</td><td>59.3</td><td>78.9</td>
    </tr>
    <tr style="color:#6b7280;">
      <td>Grok-4-2025-07-09</td><td>47.9</td><td>37.8</td><td>63.5</td><td>43.2</td><td>47.0</td><td>56.4</td><td>54.9</td><td>75.7</td>
    </tr>
    <tr style="color:#6b7280;">
      <td>GPT-5-2025-08-07</td><td>55.0</td><td>41.8</td><td>56.3</td><td>45.5</td><td>61.8</td><td>68.0</td><td>60.3</td><td>81.6</td>
    </tr>
  </tbody>
</table>

## 🛠️ QuickStart

### Installation

We recommend using [uv](https://docs.astral.sh/uv/) to manage the environment.

> uv installation guide: <https://docs.astral.sh/uv/getting-started/installation/#installing-uv>

```bash
git clone git@github.com:OpenSenseNova/SenseNova-SI.git
cd SenseNova-SI/
uv sync --extra cu124 # or one of [cu118|cu121|cu124|cu126|cu128|cu129], depending on your CUDA version
uv sync
source .venv/bin/activate
```

#### Hello World

A simple image-free test to verify environment setup and download the model.

```bash
python example.py \
  --question "Hello" \
  --model_path sensenova/SenseNova-SI-1.3-InternVL3-8B
```

### Examples

#### Example 1

This example is from [SITE-Bench](https://github.com/wenqi-wang20/SITE-Bench):


```bash
python example.py \
  --image_paths examples/Q1_1.png \
  --question "Consider the real-world 3D locations of the objects. Which is closer to the sink, the toilet paper or the towel?\nOptions: \nA. toilet paper\nB. towel\nGive me the answer letter directly. The best answer is:" \
  --model_path sensenova/SenseNova-SI-1.3-InternVL3-8B
# --model_path sensenova/SenseNova-SI-1.1-Qwen3-VL-8B
```



<!-- Example 1 -->
<details open>
  <summary><strong>Details of Example 1</strong></summary>
  <p><strong>Q:</strong>Consider the real-world 3D locations of the objects. Which is closer to the sink, the toilet paper or the towel?\nOptions: \nA. toilet paper\nB. towel\nGive me the answer letter directly. The best answer is:</p>
  <table>
    <tr>
      <td align="center" width="50%" style="padding:4px;">
        <img src="./examples/Q1_1.png" alt="First image" width="100%">
      </td>
    </tr>
  </table>
  <p><strong>GT: A</strong></p>
</details>


#### Example 2

This example is from [MMSI-Bench](https://github.com/InternRobotics/MMSI-Bench):


```bash
python example.py \
  --image_paths examples/Q2_1.png examples/Q2_2.png \
  --question "If the landscape painting is on the east side of the bedroom, where is the window located in the bedroom?\nOptions: A. North side, B. South side, C. West side, D. East side\nAnswer with the option's letter from the given choices directly. Enclose the option's letter within ``." \
  --model_path sensenova/SenseNova-SI-1.3-InternVL3-8B 
# --model_path sensenova/SenseNova-SI-1.1-Qwen3-VL-8B
```

<!-- Example 2 -->
<details open>
  <summary><strong>Details of Example 2</strong></summary>
  <p><strong>Q:</strong>If the landscape painting is on the east side of the bedroom, where is the window located in the bedroom?\nOptions: A. North side, B. South side, C. West side, D. East side\nAnswer with the option's letter from the given choices directly. Enclose the option's letter within ``.</p>
  <table>
    <tr>
      <td align="center" width="50%" style="padding:4px;">
        <img src="./examples/Q2_1.png" alt="First image" width="100%">
      </td>
      <td align="center" width="50%" style="padding:4px;">
        <img src="./examples/Q2_2.png" alt="Second image" width="100%">
      </td>
    </tr>
  </table>
  <p><strong>GT: C</strong></p>
</details>


#### Example 3

This example is from [MMSI-Bench](https://github.com/InternRobotics/MMSI-Bench) and test the model's capability in open-ended short-answer questions:

```bash
python example.py \
  --image_paths examples/Q3_1.png examples/Q3_2.png examples/Q3_3.png \
  --question "The robot is making tea. What is the order in which the pictures were taken?" \
  --model_path sensenova/SenseNova-SI-1.3-InternVL3-8B
```

<!-- Example 3 -->
<details open>
  <summary><strong>Details of Example 3</strong></summary>
  <p><strong>Q:</strong>The robot is making tea. What is the order in which the pictures were taken?</p>
  <table>
    <tr>
      <td align="center" width="33%" style="padding:4px;">
        <img src="./examples/Q3_1.png" alt="First image" width="100%">
      </td>
      <td align="center" width="33%" style="padding:4px;">
        <img src="./examples/Q3_2.png" alt="Second image" width="100%">
      </td>
      <td align="center" width="33%" style="padding:4px;">
        <img src="./examples/Q3_3.png" alt="Third image" width="100%">
      </td>
    </tr>
  </table>
  <p><strong>GT: Second, first, third</strong></p>
</details>


#### Test Multiple Questions in a Single Run

Prepare a file similar to [examples/examples.jsonl](examples/examples.jsonl), where each line represents a single question.

The model is loaded once and processes questions sequentially. The questions remain independent of each other.

> For more details on the `jsonl` format, refer to the documentation for [Single-Image Data](https://internvl.readthedocs.io/en/latest/get_started/chat_data_format.html#single-image-data) and [Multi-Image Data](https://internvl.readthedocs.io/en/latest/get_started/chat_data_format.html#multi-image-data).


```bash
python example.py \
  --jsonl_path examples/examples.jsonl \
  --model_path sensenova/SenseNova-SI-1.3-InternVL3-8B 
# --model_path OpenGVLab/InternVL3-8B 
```

### Evaluation

To reproduce the benchmark results above, please refer to [EASI](https://github.com/EvolvingLMMs-Lab/EASI) to evaluate SenseNova-SI on mainstream spatial intelligence benchmarks.


## 🖊️ Citation

```bib
@InProceedings{sensenova-si,
  title = {Scaling Spatial Intelligence with Multimodal Foundation Models},
  author = {Cai, Zhongang and Wang, Ruisi and Gu, Chenyang and Pu, Fanyi and Xu, Junxiang and Wang, Yubo and Yin, Wanqi and Yang, Zhitao and Wei, Chen and Sun, Qingping and Zhou, Tongxi and Li, Jiaqi and Pang, Hui En and Qian, Oscar and Wei, Yukun and Lin, Zhiqian and Shi, Xuanke and Deng, Kewang and Han, Xiaoyang and Chen, Zukai and Fan, Xiangyu and Deng, Hanming and Lu, Lewei and Pan, Liang and Li, Bo and Liu, Ziwei and Wang, Quan and Lin, Dahua and Yang, Lei},
  booktitle = {Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)},
  year = {2026}
}
```