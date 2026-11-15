from pathlib import Path

import pytest
import yaml

from core.models import ModelCapabilities
from gateway.adapters.contract import AdapterManifest
from gateway.domain import Upstream
from gateway.extensions import read_manifest
from gateway.runtime import RuntimeRegistry
from tests.support.gateway import SECRET, add_upstream, instance, mutate, register, state


def test_extension_media_uses_the_shared_asset_contract(tmp_path: Path):
    manifest = AdapterManifest(
        id="native-example", name="Native example", operations=("llm",),
        media={"input": ["text", "image"]}, usage_semantics="unknown",
    )
    (tmp_path / "manifest.yaml").write_text(yaml.safe_dump(manifest.model_dump(mode="json")), encoding="utf-8")
    loaded = read_manifest(tmp_path, "protocol_adapters")
    assert loaded.media.input == ("text", "image") and not loaded.builtin


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


@pytest.mark.parametrize("capabilities,message", [
    ({"structured_output": {"supported": True, "schema_subset": ["json_schema"]}},
     "当前适配器未开放结构化输出"),
    ({"input_modalities": ["text", "video"]},
     "模型声明的输入或输出模态超出适配器已实现的范围"),
])
def test_catalog_capability_errors_are_specific_and_leave_published_state_unchanged(
    tmp_path, monkeypatch, capabilities, message,
):
    with instance(tmp_path, monkeypatch) as (client, app):
        add_upstream(client, adapter="cloudflare-workers-ai")
        register(client, capabilities={"streaming": False})
        before = state(client)
        generation = app.state.registry.current
        response = mutate(client, "/models/stable", {"capabilities": capabilities}, method="PATCH")
        assert response.status_code == 422
        assert response.json()["detail"] == {"code": "INVALID_CATALOG", "message": message}
        after = state(client)
        assert after["revision"] == before["revision"]
        assert after["models"] == before["models"]
        assert app.state.registry.current is generation


def test_untyped_catalog_failure_does_not_expose_secrets(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        add_upstream(client)
        register(client)
        before = state(client)

        def failed_decryption(*_args):
            raise ValueError(f"Invalid credential: {SECRET}")

        with monkeypatch.context() as fault:
            fault.setattr(app.state.registry.vault, "decrypt", failed_decryption)
            response = client.patch("/admin/api/models/stable", json={
                "expected_revision": before["revision"], "value": {"display_name": "Changed"},
            })
        assert response.status_code == 422
        assert response.json()["error"]["message"] == "候选目录校验失败，请检查实体引用、能力、适配器 Profile 或密钥配置"
        assert SECRET not in response.text
        assert state(client)["revision"] == before["revision"]
