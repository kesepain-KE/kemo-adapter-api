"""Protocol fixtures run through the actual catalog, router and generation leases."""
from __future__ import annotations

import base64
import io
import zipfile
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from api.server import create_app
from core.config import Settings
from gateway.adapters.contract import AdapterManifest
from gateway.domain import Catalog, Deployment, PublicModel, RuntimeOptions, Upstream
from gateway.runtime import BUILTINS, RuntimeRegistry
from gateway.security import SecretVault
from gateway.store import Store, revision
from gateway.transport import Transport


SECRET = "fake-upstream-key-for-offline-tests"


def chat_response(text="hello"):
    return {"id": "upstream-response", "choices": [{
        "index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop",
    }], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}


def default_upstream(request):
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "remote"}]})
    return httpx.Response(200, json=chat_response())


@contextmanager
def instance(tmp_path, monkeypatch, handler=default_upstream, settings=None):
    monkeypatch.setenv("KEMO_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    app = create_app(settings or Settings(), live_config_root=tmp_path)
    app.state.registry.transport_factory = lambda: Transport(http_transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        yield client, app


def state(client):
    response = client.get("/admin/api/console")
    assert response.status_code == 200, response.text
    return response.json()


def mutate(client, path, value=None, method="POST", key=False, expected=None):
    current = state(client)
    return client.request(method, "/admin/api" + path, json={
        "expected_revision": expected or current["key_revision" if key else "revision"],
        "value": value or {},
    })


def add_upstream(client, identity="local", adapter="openai-chat", auth_scheme="bearer"):
    if auth_scheme == "api-key" and adapter in {"gemini", "anthropic-messages"}:
        auth_scheme = "gemini-query" if adapter == "gemini" else "anthropic-key"
    response = mutate(client, "/upstreams", {
        "id": identity, "name": identity, "adapter_id": adapter,
        "base_url": f"http://127.0.0.1:8881/{identity}", "allow_private_network": True,
        "auth_scheme": auth_scheme, "keys": [{"id": "primary", "value": SECRET}],
    })
    assert response.status_code == 200, response.text


def register(client, identity="local", model="stable", capabilities=None, task="llm"):
    response = mutate(client, f"/upstreams/{identity}/register", {"models": [{
        "public_model": model, "upstream_model": "remote", "task": task,
        "capabilities": capabilities or {"streaming": True},
    }]})
    assert response.status_code == 200, response.text


def install_fixtures(registry, *fixtures, disabled=(), system_prompt=""):
    if hasattr(registry, "_protocol_fixtures"):
        registry._protocol_fixtures.extend(fixtures)
        return
    registry._protocol_fixtures = list(fixtures)
    build = registry.build
    adapters = []

    def adapter_for(fixture, capabilities):
        class FixtureAdapter:
            manifest = AdapterManifest(
                id="fixture-" + fixture.provider_id.replace("_", "-"),
                name=fixture.provider_id, operations=("llm", "embedding", "rerank"),
                streaming=True, discovery=False, builtin=False,
                media={
                    "input": sorted({modality for caps in capabilities for modality in caps.input_modalities}),
                    "output": sorted({modality for caps in capabilities for modality in caps.output_modalities}),
                },
                multiple_choices=any(caps.supports_multiple_choices for caps in capabilities),
                structured_output=any(caps.structured_output.supported for caps in capabilities),
                reasoning_returns=tuple({mode for caps in capabilities for mode in caps.reasoning.returns}),
                reasoning_levels=tuple({effort for caps in capabilities for effort in caps.reasoning.efforts}),
                persisted_reasoning=any(caps.reasoning.persisted_state for caps in capabilities),
                multimodal_tool_results=any(caps.tools.multimodal_results for caps in capabilities),
                prediction=any(caps.supports_prediction for caps in capabilities),
                service_tiers=any(caps.service_tiers for caps in capabilities),
                logprobs=any(caps.decoding.logprobs for caps in capabilities),
                verbosity=any(caps.decoding.verbosity for caps in capabilities),
                usage_semantics="Offline protocol fixture with explicitly supplied usage.",
            )

            def __init__(self, transport):
                self.transport = transport

            async def execute(self, request, context, model, upstream, headers):
                return await fixture.execute(request, context)

            def stream(self, request, context, model, upstream, headers):
                return fixture.stream(request, context)

            async def embed(self, request, context, model, upstream, headers):
                return await fixture.embed(request, context)

            async def rerank(self, request, context, model, upstream, headers):
                return await fixture.rerank(request, context)
        return FixtureAdapter

    async def build_candidate(catalog):
        with patch("gateway.runtime.BUILTINS", BUILTINS + tuple(adapters)):
            return await build(catalog)

    async def initialize():
        upstreams, models, deployments = [], [], []
        for fixture in registry._protocol_fixtures:
            fixture_models = sorted(fixture.models)
            capabilities = [await fixture.capabilities(model) for model in fixture_models]
            adapter = adapter_for(fixture, capabilities)
            adapters.append(adapter)
            identity = adapter.manifest.id
            upstreams.append(Upstream(id=identity, name=fixture.provider_id,
                                     adapter_id=identity, base_url="http://127.0.0.1",
                                     auth_scheme="none", allow_private_network=True))
            for model, caps in zip(fixture_models, capabilities):
                document = caps.model_dump(mode="json")
                for key in ("protocol_version", "model", "provider_id", "provider_model", "task"):
                    document.pop(key)
                models.append(PublicModel(id=model, task=caps.task, capabilities=document, enabled=model not in disabled))
                deployments.append(Deployment(id="fixture_" + str(len(deployments)), upstream_id=identity,
                                              upstream_model=model, public_model=model))
        candidate = Catalog(upstreams=tuple(upstreams), models=tuple(models), deployments=tuple(deployments),
                            settings=RuntimeOptions(system_prompt=system_prompt))
        registry.current = await build_candidate(candidate)
        registry.store.save(candidate, revision(registry.store.catalog()), "protocol-fixture")

    registry.build = build_candidate
    registry.initialize = initialize


@asynccontextmanager
async def fixture_registry(project_root, *fixtures):
    store = Store(project_root / "data")
    registry = RuntimeRegistry(project_root, store, SecretVault(project_root / "data"))
    install_fixtures(registry, *fixtures)
    try:
        await registry.initialize()
        yield registry
    finally:
        await registry.close()
        store.close()


def key(client, **policy):
    response = mutate(client, "/keys", {"name": "test", **policy}, key=True)
    assert response.status_code == 200, response.text
    return response.json()



def invoke(client, token, identity="req_one", stream=False, model="stable"):
    return client.post("/model/responses", headers={
        "Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0",
        "Idempotency-Key": identity,
    }, json={"protocol_version": "2.0", "request_id": identity, "model": model, "stream": stream,
             "input": [{"type": "message", "id": "msg_user", "role": "user",
                        "content": [{"type": "text", "text": "hello"}]}]})



def extension_zip(kind, *, identity="echo", code=None, template=None):
    import yaml
    manifest = {"id": identity, "name": identity, "version": "1.0.0", "contract_version": "1.0"}
    if kind == "protocol_adapters":
        manifest.update(operations=["llm"], auth_schemes=[{"id": "none", "location": "none"}], discovery=False,
                        usage_semantics="No usage evidence is available.")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.yaml", yaml.safe_dump(manifest))
        if kind == "protocol_adapters":
            archive.writestr("adapter.py", code)
        else:
            archive.writestr("template.yaml", yaml.safe_dump(template))
    return output.getvalue()



def install_extension(client, kind, content):
    return client.post(f"/admin/api/extensions/{kind}/install",
                       data={"expected_revision": state(client)["revision"]},
                       files={"file": ("extension.zip", content, "application/zip")})
