"""可运行的 Kemo 声明/请求配对样例，不包含网络 Client 或真实厂商配置。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from core.models import EmbeddingRequest, KemoRequest, ModelCapabilities, RerankRequest
from template.provider.capabilities import MODEL_CAPABILITIES


# 这些是 Kemo 操作需要的模态，不是上游支持证明。
OPERATION_MODALITIES = {
    "conversation": (["text"], ["text"]),
    "vision": (["text", "image"], ["text"]),
    "image_generation": (["text"], ["image"]),
    "image_edit": (["text", "image"], ["image"]),
    "audio_transcription": (["audio"], ["text"]),
    "speech_generation": (["text"], ["audio"]),
    "speech_to_speech": (["audio"], ["audio"]),
    "video_understanding": (["video"], ["text"]),
    "video_generation": (["text"], ["video"]),
}


def add_verified_operations(
    original: ModelCapabilities, operations: list[str]
) -> ModelCapabilities:
    """在一个模型上增量增加操作；只能在目标适配器实现且验证后用于真实声明。"""
    if original.task != "llm":
        raise ValueError("媒体操作属于 llm 合同，不能写进 embedding/rerank")
    data = original.model_dump(mode="json")
    declared = data["extensions"].setdefault("operations", {})
    for operation in operations:
        if operation not in OPERATION_MODALITIES:
            raise ValueError("未知 Kemo 操作")
        inputs, outputs = OPERATION_MODALITIES[operation]
        for field, modalities in (("input_modalities", inputs), ("output_modalities", outputs)):
            data[field] = list(dict.fromkeys([*data[field], *modalities]))
        declared[operation] = {"supported": True}
    # 不用 model_copy(update=...) 绕过 Pydantic 校验；不修改 original 或其他模型。
    return ModelCapabilities.model_validate(data)


def capability_example(operation: str) -> ModelCapabilities:
    data = MODEL_CAPABILITIES["example-model-name"].model_dump(mode="json")
    data["model"] = "example-demo-model"
    data["metadata"]["upstream_model"] = "demo-model"
    data["metadata"]["example_only"] = True
    # 每个示例只演示对应操作；真实模型可以通过上面的函数合并多种已验证操作。
    data["input_modalities"], data["output_modalities"] = deepcopy(OPERATION_MODALITIES[operation])
    data["extensions"]["operations"] = {
        name: {"supported": name == operation} for name in OPERATION_MODALITIES
    }
    return ModelCapabilities.model_validate(data)


def request_example(operation: str) -> KemoRequest:
    """URL 只是离线结构样例；真实调用必须替换为已授权媒体或当前主体的 asset_id。"""
    inputs, outputs = OPERATION_MODALITIES[operation]
    content: list[dict[str, Any]] = []
    media = {
        "image": ("image/png", "input.png"),
        "audio": ("audio/wav", "input.wav"),
        "video": ("video/mp4", "input.mp4"),
    }
    for modality in inputs:
        if modality == "text":
            content.append({"type": "text", "text": "执行本次操作"})
        else:
            mime, filename = media[modality]
            content.append({
                "type": modality, "mime_type": mime,
                "source": {"kind": "url", "uri": f"https://media.example.invalid/{filename}"},
            })
    output: dict[str, Any] = {"modalities": list(outputs)}
    configs = {
        "image": {"format": "png", "size": "1024x1024"},
        "audio": {"format": "wav", "voice": "default"},
        "video": {"format": "mp4"},
    }
    for modality in outputs:
        if modality in configs:
            output[modality] = configs[modality]
    return KemoRequest.model_validate({
        "protocol_version": "1.0", "request_id": f"demo-{operation}", "attempt": 1,
        "model": "example-demo-model", "stream": False, "system_prompt": "",
        "generation": {"max_output_tokens": 64, "parallel_tool_calls": False},
        "output": output, "tools": [],
        "input": [{"id": "msg_demo_user", "type": "message", "role": "user",
                   "status": "completed", "content": content}],
        "provider_options": {}, "metadata": {"capability": operation}, "extensions": {},
    })


def reasoning_example() -> ModelCapabilities:
    """模拟只有三档的上游；不是通用厂商映射，不能直接当真实配置发布。"""
    data = capability_example("conversation").model_dump(mode="json")
    effort_map = {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "max": "high"}
    data["reasoning"].update(supported=True, efforts=list(effort_map))
    data["extensions"]["reasoning_effort_map"] = effort_map
    data["extensions"]["reasoning_policy"] = {
        "mode": "mapped", "logical_efforts": list(effort_map),
        "upstream_parameter": "thinking_level", "collapsed": True,
    }
    return ModelCapabilities.model_validate(data)


def reasoning_payload_example(effort: str) -> dict[str, str]:
    """演示逐档映射；真实 protocol.py 还要处理开关、统一字段优先和回放状态。"""
    mapping = reasoning_example().extensions["reasoning_effort_map"]
    if effort not in mapping:
        raise ValueError("未声明的推理档位")
    return {"thinking_level": mapping[effort]}


def retrieval_example(task: str) -> tuple[ModelCapabilities, EmbeddingRequest | RerankRequest]:
    """模拟检索模型；维度/批量数字仅用于离线测试，不是生产额度。"""
    data = MODEL_CAPABILITIES["example-model-name"].model_dump(mode="json")
    data.update(model=f"example-demo-{task}", task=task)
    data["metadata"] = {"upstream_model": f"demo-{task}", "example_only": True}
    data["extensions"].pop("operations")
    base = {"protocol_version": "1.0", "request_id": f"demo-{task}", "model": data["model"]}
    if task == "embedding":
        data["output_modalities"] = ["embedding"]
        data["embedding"] = {
            "input_types": ["query", "document"], "default_dimensions": 3,
            "supported_dimensions": [3], "max_batch_size": 2, "normalization": "unknown",
        }
        request = EmbeddingRequest.model_validate({
            **base, "input_type": "query", "inputs": [{"id": "doc_1", "text": "测试文本"}],
        })
    elif task == "rerank":
        data["output_modalities"] = ["score"]
        data["rerank"] = {"max_documents": 2, "supports_return_documents": False}
        request = RerankRequest.model_validate({
            **base, "query": "测试问题", "documents": [{"id": "doc_1", "text": "候选内容"}], "top_n": 1,
        })
    else:
        raise ValueError("检索示例仅支持 embedding/rerank")
    return ModelCapabilities.model_validate(data), request
