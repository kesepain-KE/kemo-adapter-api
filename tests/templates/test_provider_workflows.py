"""文档样例与模板门禁：全离线，不加载 providers/，不读取真实密钥。"""

from __future__ import annotations

import json
import asyncio
import re
from copy import deepcopy
from pathlib import Path

import pytest

from core.capability_validation import validate_llm_request_capabilities
from core.models import KemoRequest, ModelCapabilities
from core.live_config import LiveConfigManager
from core.provider_keys import normalize_provider_keys
from core.provider_contract import ProviderException
from template.examples.model_workflows import (
    OPERATION_MODALITIES, add_verified_operations, capability_example,
    reasoning_example, reasoning_payload_example, request_example, retrieval_example,
)
from template.provider.capabilities import MODEL_CAPABILITIES
from template.provider.catalog_contract import manifest_entry, validate_catalog


from tests.support.paths import PROJECT_ROOT as ROOT


def manifest_for(capabilities: dict[str, ModelCapabilities]) -> dict:
    return {"provider_id": "example", "models": {model: manifest_entry(cap) for model, cap in capabilities.items()}}


def test_template_catalog_is_conservative_and_matches_example_manifest() -> None:
    manifest = json.loads((ROOT / "template/provider/manifest.json.example").read_text(encoding="utf-8"))
    validate_catalog("example", MODEL_CAPABILITIES, MODEL_CAPABILITIES, manifest)
    for cap in MODEL_CAPABILITIES.values():
        assert cap.streaming is False
        assert cap.reasoning.supported is False
        assert cap.tools.function_calling is False
        assert cap.extensions["limits"] == {}
        assert cap.extensions["asset_limits"] == {}


@pytest.mark.parametrize("field", [
    "task", "upstream_model", "input_modalities", "output_modalities", "streaming",
    "reasoning", "tools", "structured_output", "operations", "limits", "asset_limits",
    "probe", "reasoning_effort_map", "reasoning_policy",
])
def test_catalog_detects_missing_or_drifted_fields(field: str) -> None:
    manifest = manifest_for(MODEL_CAPABILITIES)
    del manifest["models"]["example-model-name"][field]
    with pytest.raises(ValueError, match=field):
        validate_catalog("example", MODEL_CAPABILITIES, MODEL_CAPABILITIES, manifest)


def test_catalog_checks_all_models_and_removed_capabilities() -> None:
    added = capability_example("vision")
    catalog = {**MODEL_CAPABILITIES, added.model: added}
    manifest = manifest_for(catalog)
    validate_catalog("example", catalog, catalog, manifest)
    with pytest.raises(ValueError, match="集合"):
        validate_catalog("example", MODEL_CAPABILITIES, catalog, manifest)
    manifest["models"][added.model]["embedding"] = {"default_dimensions": 3}
    with pytest.raises(ValueError, match="embedding"):
        validate_catalog("example", catalog, catalog, manifest)


def test_catalog_rejects_provider_and_upstream_name_drift() -> None:
    manifest = manifest_for(MODEL_CAPABILITIES)
    with pytest.raises(ValueError, match="provider_id"):
        validate_catalog("other", MODEL_CAPABILITIES, MODEL_CAPABILITIES, manifest)
    data = MODEL_CAPABILITIES["example-model-name"].model_dump()
    data["metadata"]["upstream_model"] = "wrong"
    cap = ModelCapabilities.model_validate(data)
    with pytest.raises(ValueError, match="前缀"):
        validate_catalog("example", [cap.model], {cap.model: cap}, manifest)


def test_generated_manifest_does_not_share_mutable_capability_data() -> None:
    cap = reasoning_example()
    original = cap.model_dump()
    entry = manifest_entry(cap)
    entry["reasoning_effort_map"]["max"] = "changed"
    entry["operations"]["conversation"]["supported"] = False
    entry["reasoning"]["efforts"].clear()
    assert cap.model_dump() == original


def test_manifest_boolean_must_not_be_a_number() -> None:
    manifest = manifest_for(MODEL_CAPABILITIES)
    manifest["models"]["example-model-name"]["streaming"] = 0
    with pytest.raises(ValueError, match="streaming"):
        validate_catalog("example", MODEL_CAPABILITIES, MODEL_CAPABILITIES, manifest)


@pytest.mark.parametrize("operation", OPERATION_MODALITIES)
def test_nine_operation_examples_pass_real_kemo_validation(operation: str) -> None:
    cap = capability_example(operation)
    req = KemoRequest.model_validate_json(request_example(operation).model_dump_json())
    validate_llm_request_capabilities(req, cap)
    validate_catalog("example", [cap.model], {cap.model: cap}, manifest_for({cap.model: cap}))


@pytest.mark.parametrize("operation", [name for name in OPERATION_MODALITIES if name != "conversation"])
def test_operation_cannot_be_enabled_by_only_adding_a_model_name(operation: str) -> None:
    with pytest.raises(ProviderException):
        validate_llm_request_capabilities(request_example(operation), capability_example("conversation"))


def test_single_model_multimodal_patch_preserves_other_models_and_fields() -> None:
    first = capability_example("conversation")
    first.metadata["keep"] = "custom metadata"
    first.extensions["keep"] = {"nested": True}
    original = first.model_dump()
    other = deepcopy(MODEL_CAPABILITIES["example-model-name"])
    patched = add_verified_operations(first, ["vision", "speech_generation"])
    assert first.model_dump() == original
    assert other == MODEL_CAPABILITIES["example-model-name"]
    assert patched.model == first.model
    assert patched.metadata == first.metadata
    assert patched.extensions["keep"] == {"nested": True}
    assert patched.extensions["operations"]["conversation"]["supported"] is True
    assert patched.input_modalities == ["text", "image"]
    assert patched.output_modalities == ["text", "audio"]
    assert patched.streaming is False
    assert patched.tools.function_calling is False
    for operation in ("conversation", "vision", "speech_generation"):
        validate_llm_request_capabilities(request_example(operation), patched)
    with pytest.raises(ValueError, match="未知"):
        add_verified_operations(first, ["made_up_operation"])


@pytest.mark.parametrize("effort,upstream", [
    ("minimal", "low"), ("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "high"),
])
def test_five_logical_efforts_are_validated_and_mapped(effort: str, upstream: str) -> None:
    cap = reasoning_example()
    data = request_example("conversation").model_dump(mode="json")
    data["reasoning"] = {"enabled": True, "effort": effort}
    validate_llm_request_capabilities(KemoRequest.model_validate(data), cap)
    assert reasoning_payload_example(effort) == {"thinking_level": upstream}
    validate_catalog("example", [cap.model], {cap.model: cap}, manifest_for({cap.model: cap}))


@pytest.mark.parametrize("effort", ["ultra", "xhigh", "none", ""])
def test_reasoning_example_rejects_unmapped_efforts(effort: str) -> None:
    with pytest.raises(ValueError, match="未声明"):
        reasoning_payload_example(effort)


def test_reasoning_map_must_cover_every_advertised_effort() -> None:
    cap = reasoning_example()
    del cap.extensions["reasoning_effort_map"]["max"]
    with pytest.raises(ValueError, match="未覆盖"):
        validate_catalog("example", [cap.model], {cap.model: cap}, manifest_for({cap.model: cap}))


@pytest.mark.parametrize("task", ["embedding", "rerank"])
def test_retrieval_examples_use_their_own_contracts(task: str) -> None:
    cap, req = retrieval_example(task)
    assert req.model == cap.model
    assert type(req).model_validate_json(req.model_dump_json()) == req
    validate_catalog("example", [cap.model], {cap.model: cap}, manifest_for({cap.model: cap}))
    with pytest.raises(ValueError, match="llm"):
        add_verified_operations(cap, ["vision"])


def test_key_examples_match_the_real_loaders_without_real_secrets(tmp_path: Path) -> None:
    upstream = json.loads((ROOT / "template/provider/secrets.json.example").read_text(encoding="utf-8"))
    entries = normalize_provider_keys(upstream)
    assert entries and any(enabled for _, _, enabled in entries)
    caller = json.loads((ROOT / "template/examples/gateway-keys.json.example").read_text(encoding="utf-8"))
    path = tmp_path / "api" / "keys.json"
    path.parent.mkdir()
    path.write_text(json.dumps(caller), encoding="utf-8")
    manager = LiveConfigManager(tmp_path)
    snapshot = asyncio.run(manager.refresh())
    assert manager.last_error is None
    assert len(snapshot.api_keys) == 1
    principal = next(iter(snapshot.api_keys.values()))
    assert principal.key_id == "demo-agent"
    assert principal.scopes == frozenset({"model:invoke"})
    assert principal.allowed_models == frozenset({"example-demo-model"})


def test_recipe_links_and_minimal_python_example_stay_valid() -> None:
    documents = [
        ROOT / "agent_control.md", ROOT / "template/README.md",
        ROOT / "template/provider/README.md", ROOT / "template/examples/README.md",
        *sorted((ROOT / "ADD_DIY").glob("*.md")),
    ]
    for document in documents:
        content = document.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)]+)\)", content):
            if "://" in target or target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            assert (document.parent / path).exists(), f"{document.name}: {path}"
    # 这段无网络/无密钥的文档示例也是验收对象，防止代码改动后说明失效。
    content = (ROOT / "ADD_DIY/model-maintenance.md").read_text(encoding="utf-8")
    snippet = re.search(r"```python\n(.*?)\n```", content, re.DOTALL)
    assert snippet is not None
    namespace: dict = {}
    exec(compile(snippet.group(1), "model-maintenance.md", "exec"), namespace)
    cap = namespace["new_model"]
    manifest = {"provider_id": "demo_vendor", "models": {cap.model: manifest_entry(cap)}}
    validate_catalog("demo_vendor", [cap.model], {cap.model: cap}, manifest)
