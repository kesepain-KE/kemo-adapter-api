"""可随厂商包复制的离线目录检查；不读取密钥，也不访问上游。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from core.models import ModelCapabilities


KEMO_EFFORTS = ("minimal", "low", "medium", "high", "max")


def manifest_entry(capability: ModelCapabilities) -> dict[str, Any]:
    """生成单个模型的静态字段；调用者仍须保留 manifest 的其他模型和自定义字段。"""
    fields = (
        "task", "input_modalities", "output_modalities", "streaming",
        "reasoning", "tools", "structured_output",
    )
    data = capability.model_dump(mode="json")
    entry = {field: data[field] for field in fields}
    entry["upstream_model"] = capability.metadata.get("upstream_model")
    for field in ("embedding", "rerank"):
        if data[field] is not None:
            entry[field] = data[field]
    for field in (
        "limits", "asset_limits", "operations", "probe",
        "reasoning_effort_map", "reasoning_policy",
    ):
        if field in capability.extensions:
            entry[field] = data["extensions"][field]
    return entry


def validate_catalog(
    provider_id: str,
    models: Iterable[str],
    capabilities: Mapping[str, ModelCapabilities],
    manifest: Mapping[str, Any],
) -> None:
    """不一致时只报告模型/字段位置，不把配置值写入异常。"""
    if manifest.get("provider_id") != provider_id:
        raise ValueError("manifest.provider_id 与运行时不一致")
    entries = manifest.get("models")
    if not isinstance(entries, Mapping):
        raise ValueError("manifest.models 必须是对象")
    registered = set(models)
    if not registered or registered != set(capabilities) or registered != set(entries):
        raise ValueError("provider.models / MODEL_CAPABILITIES / manifest.models 集合不一致或为空")
    for model in sorted(registered):
        capability = ModelCapabilities.model_validate(capabilities[model].model_dump())
        upstream = capability.metadata.get("upstream_model")
        if not isinstance(upstream, str) or not upstream.strip():
            raise ValueError(f"{model}: metadata.upstream_model 缺失")
        if capability.model != model or model != f"{provider_id}-{upstream}":
            raise ValueError(f"{model}: 模型名/厂商前缀/upstream_model 不一致")
        expected = manifest_entry(capability)
        entry = entries[model]
        if not isinstance(entry, Mapping):
            raise ValueError(f"{model}: manifest 模型项必须是对象")
        # 比较两边已知字段的并集，避免删除能力后清单还残留旧字段。
        for field in set(expected) | (set(entry) & {
            "embedding", "rerank", "limits", "asset_limits", "operations", "probe",
            "reasoning_effort_map", "reasoning_policy",
        }):
            if field not in entry or json.dumps(entry[field], sort_keys=True) != json.dumps(expected.get(field), sort_keys=True):
                raise ValueError(f"{model}: manifest.{field} 与能力声明不一致")
        reasoning = capability.reasoning
        efforts = reasoning.efforts
        if len(efforts) != len(set(efforts)) or set(efforts) - set(KEMO_EFFORTS):
            raise ValueError(f"{model}: reasoning.efforts 非法或重复")
        if not reasoning.supported and (efforts or reasoning.summary or reasoning.persisted_state):
            raise ValueError(f"{model}: 不支持推理却声明了推理能力")
        effort_map = capability.extensions.get("reasoning_effort_map", {})
        if not isinstance(effort_map, Mapping) or set(effort_map) != set(efforts):
            raise ValueError(f"{model}: reasoning_effort_map 未覆盖声明档位")
        policy = capability.extensions.get("reasoning_policy", {})
        if not isinstance(policy, Mapping) or policy.get("logical_efforts") != efforts:
            raise ValueError(f"{model}: reasoning_policy.logical_efforts 不一致")
        if not reasoning.supported and policy.get("mode") != "unsupported":
            raise ValueError(f"{model}: 不支持推理时 policy.mode 必须为 unsupported")
        if reasoning.supported and policy.get("mode") not in {"native", "mapped", "provider_default"}:
            raise ValueError(f"{model}: 推理 policy.mode 无效")
