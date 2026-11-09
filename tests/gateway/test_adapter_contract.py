from pathlib import Path

import pytest
import yaml

from core.models import ModelCapabilities
from gateway.adapters.contract import AdapterManifest
from gateway.domain import Upstream
from gateway.extensions import read_manifest
from gateway.runtime import RuntimeRegistry


def test_extension_media_requires_cross_process_asset_contract(tmp_path: Path):
    manifest = AdapterManifest(
        id="native-example", name="Native example", operations=("llm",),
        media={"input": ["text", "image"]}, usage_semantics="unknown",
    )
    (tmp_path / "manifest.yaml").write_text(yaml.safe_dump(manifest.model_dump(mode="json")), encoding="utf-8")
    with pytest.raises(ValueError, match="跨进程媒体"):
        read_manifest(tmp_path, "protocol_adapters")


def test_custom_adapter_declarations_cannot_exceed_implemented_features():
    class Adapter:
        manifest = AdapterManifest(
            id="native-example", name="Native example", operations=("llm",),
            usage_semantics="unknown", builtin=False,
        )

    upstream = Upstream(id="native", name="Native", adapter_id="native-example", base_url="https://example.com")
    caps = ModelCapabilities(protocol_version="2.0", model="native", provider_id="gateway", provider_model="native")
    RuntimeRegistry.validate_declaration(Adapter(), caps, upstream)
    with pytest.raises(ValueError, match="结构化输出"):
        RuntimeRegistry.validate_declaration(
            Adapter(), caps.model_copy(update={"structured_output": caps.structured_output.model_copy(update={"supported": True})}),
            upstream,
        )
