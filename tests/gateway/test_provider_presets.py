from __future__ import annotations

import asyncio
import json

import httpx
import jsonschema
import pytest

from core.models import KemoRequest
from core.provider_contract import ProviderEventKind as Kind, RequestContext
from gateway.adapters.builtin.cohere_chat import CohereChat
from gateway.domain import Upstream
from gateway.extensions import read_manifest
from gateway.runtime import BUILTINS, RuntimeUpstream
from gateway.transport import Transport
from tests.support.paths import PROJECT_ROOT
from tests.support.gateway import extension_zip, install_extension, instance, mutate, state


PRESETS = PROJECT_ROOT / "templates" / "provider_presets"
ADAPTERS = {adapter.manifest.id: adapter.manifest for adapter in BUILTINS}
P0 = {"openai", "anthropic", "gemini", "openai-responses"}
P1 = {"deepseek", "zhipu", "dashscope", "moonshot", "volcengine", "xai"}


def test_priority_presets_exist():
    assert P0 | P1 <= {path.name for path in PRESETS.iterdir() if path.is_dir()}


def test_template_defaults_and_adapter_consistency(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, _):
        package = extension_zip("provider_templates", identity="local-native", template={
            "adapter_id": "openai-chat", "auth_scheme": "none", "name": "Local template",
            "base_url": "http://127.0.0.1:8881", "timeout": 345, "note": "Local test",
            "allow_private_network": True,
        })
        assert install_extension(client, "provider_templates", package).status_code == 200
        assert mutate(client, "/extensions/provider_templates/local-native", {"enabled": True}, method="PATCH").status_code == 200
        result = mutate(client, "/upstreams", {"id": "local", "template_id": "local-native"})
        assert result.status_code == 200, result.text
        upstream = result.json()["upstream"]
        assert upstream["timeout"] == 345 and upstream["note"] == "Local test"
        assert upstream["allow_private_network"] is True and upstream["auth_scheme"] == "none"
        before = state(client)["revision"]
        result = mutate(client, "/upstreams/local", {"adapter_id": "openai-responses"}, method="PATCH")
        assert result.status_code == 422
        assert state(client)["revision"] == before
        result = mutate(client, "/upstreams", {
            "id": "override", "template_id": "local-native", "timeout": 99, "note": "Override",
        })
        assert result.status_code == 200
        assert result.json()["upstream"]["timeout"] == 99 and result.json()["upstream"]["note"] == "Override"


def test_console_presets_never_infer_verified_from_structure(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, _):
        templates = state(client)["templates"]
        assert len(templates) == 47
        assert {item["id"] for item in templates if item["verification"] == "verified"} == {"deepseek", "command-code"}
        assert all(item["verification"] == "declared" for item in templates if item["id"] not in {"deepseek", "command-code"})
        assert {item["id"] for item in templates if item["category"] == "coding_service"} == {"opencode", "command-code"}


@pytest.mark.parametrize("path", sorted(path for path in PRESETS.iterdir() if path.is_dir()),
                         ids=lambda path: path.name)
def test_provider_preset_contract(path):
    assert {entry.name for entry in path.iterdir()} == {"manifest.yaml", "template.yaml", "README.md"}
    manifest = read_manifest(path, "provider_templates")
    assert manifest["id"] == path.name
    assert manifest["contract_version"] == "1.0"
    template = manifest["template"]
    adapter = ADAPTERS[template["adapter_id"]]
    assert template["auth_scheme"] in {scheme.id for scheme in adapter.auth_schemes}
    if manifest["id"] == "codexmanager-images":
        assert template["catalog"] == ["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"]
        assert manifest["verification"] == "declared"
    else:
        assert template["catalog"] == []
    jsonschema.validate(template["profile"], adapter.config_schema)
    if adapter.id != "openai-chat":
        assert template["profile"] == {}
    else:
        assert template["profile"]["max_tokens_field"] in {"max_tokens", "max_completion_tokens"}
    # TemplateDefinition does not validate headers; use the actual upstream boundary.
    Upstream(id=manifest["id"], **{field: template[field] for field in (
        "name", "adapter_id", "base_url", "auth_scheme", "headers", "profile",
    )})


@pytest.mark.parametrize(("stream", "location"), [
    (False, "usage"), (False, "meta"), (True, "usage"), (True, "meta"), (True, "event_meta"),
])
def test_cohere_usage_envelopes(stream, location):
    billed = {"billed_units": {"input_tokens": 17, "output_tokens": 12},
              "tokens": {"input_tokens": 215, "output_tokens": 24}}
    request = KemoRequest.model_validate({
        "protocol_version": "2.0", "request_id": "req_usage", "model": "public", "stream": stream,
        "input": [{"type": "message", "id": "msg_user", "role": "user",
                   "content": [{"type": "text", "text": "hello"}]}],
    })
    context = RequestContext("tenant", "subject", "req_usage", "resp_usage", "trace_usage")
    upstream = RuntimeUpstream(
        "offline", "cohere-chat", "https://cohere.invalid", 10, True, False,
        "none", {}, {}, (), "{}",
    )
    def handler(http_request):
        assert http_request.url.path == "/v2/chat"
        if not stream:
            return httpx.Response(200, json={
                "id": "cohere-usage", "message": {"content": [{"type": "text", "text": "hello"}]},
                "finish_reason": "COMPLETE", location: billed,
            })
        end = {"type": "message-end", "delta": {"finish_reason": "COMPLETE"}}
        if location == "event_meta":
            end["meta"] = billed
        else:
            end["delta"][location] = billed
        frames = [
            {"type": "message-start", "id": "cohere-usage"},
            {"type": "content-delta", "delta": {"message": {"content": {"text": "hello"}}}},
            end,
        ]
        return httpx.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames),
                              headers={"content-type": "text/event-stream"})
    async def run():
        transport = Transport(http_transport=httpx.MockTransport(handler))
        try:
            adapter = CohereChat(transport)
            if stream:
                events = [event async for event in adapter.stream(request, context, "command", upstream, {})]
                assert [event.kind for event in events] == [Kind.TEXT_DELTA, Kind.USAGE, Kind.COMPLETED]
                assert events[-2].usage.total_tokens == 29
                return events[-1].result
            return await adapter.execute(request, context, "command", upstream, {})
        finally:
            await transport.close()
    result = asyncio.run(run())
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.total_tokens) == (17, 12, 29)
    assert result.usage.measurement.exact
