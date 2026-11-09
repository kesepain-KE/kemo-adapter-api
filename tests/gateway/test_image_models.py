from __future__ import annotations

import base64
from email.parser import BytesParser
from email.policy import default
import hashlib
import json

import httpx
import pytest

from gateway.model_probe import probe_image
from tests.support.gateway import SECRET, add_upstream, instance, key, mutate, register, state


PNG = base64.b64decode(probe_image()[1].split(",", 1)[1])
CAPABILITIES = {
    "input_modalities": ["text", "image"], "output_modalities": ["image"], "streaming": False,
    "extensions": {"operations": {"image_generation": True, "image_edit": True}},
}


def image_response():
    return {"data": [{"b64_json": base64.b64encode(PNG).decode(), "revised_prompt": "A blue square."}],
            "usage": {"input_tokens": 39, "output_tokens": 472, "total_tokens": 511}}


def image_request(identity, *, image=None):
    blocks = [{"type": "text", "text": "Draw a square."}]
    if image is not None:
        blocks.append(image)
    return {
        "protocol_version": "2.0", "request_id": "req_" + identity, "model": "pictures", "stream": False,
        "metadata": {"capability": "image_edit" if image else "image_generation"},
        "output": {"modalities": ["image"], "image": {"format": "png", "size": "1024x1024"}},
        "input": [{"type": "message", "id": "msg_user", "role": "user", "content": blocks}],
    }


def test_unlisted_image_model_registration_uses_image_endpoints_and_owned_assets(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers["x-api-key"] == SECRET
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "text-only"}]})
        if request.url.path.endswith("/images/edits"):
            assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
            envelope = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content,
            )
            parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                     for part in envelope.iter_parts()}
            assert parts["model"] == b"gpt-image-2"
            assert parts["image[]"] == PNG
            assert parts["n"] == b"1"
        else:
            assert request.url.path.endswith("/images/generations")
            assert json.loads(request.content)["model"] == "gpt-image-2"
        return httpx.Response(200, json=image_response())

    with instance(tmp_path, monkeypatch, handler) as (client, app):
        add_upstream(client, identity="images", adapter="openai-images", auth_scheme="api-key")
        assert client.post("/admin/api/upstreams/images/discover").json()["models"] == ["text-only"]
        response = mutate(client, "/upstreams/images/register", {"models": [{
            "upstream_model": "gpt-image-2", "public_model": "pictures",
        }]})
        assert response.status_code == 200, response.text
        model = state(client)["models"][0]
        assert model["verification"] == "verified"
        assert model["capabilities"]["input_modalities"] == ["text", "image"]
        assert model["capabilities"]["output_modalities"] == ["image"]
        assert model["capabilities"]["extensions"]["operations"] == {
            "image_generation": True, "image_edit": True,
        }
        checks = response.json()["detection"][0]["checks"]
        assert checks["image_generation"]["status"] == checks["image_edit"]["status"] == "supported"
        assert not any("/responses" in str(call.url) or "/chat/completions" in str(call.url) for call in calls)
        assert len(state(client)["attempts"]) == 2
        deployment = state(client)["deployments"][0]
        tested = mutate(client, f"/deployments/{deployment['id']}/test?first_token=true")
        assert tested.status_code == 200, tested.text
        assert tested.json()["status"] == "supported"
        assert tested.json()["streaming"] is False and tested.json()["first_token_ms"] is None
        assert len(tested.json()["output_images"]) == 1
        token = key(client)["token"]
        headers = {"Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0",
                   "Idempotency-Key": "req_image-public"}
        response = client.post("/model/responses", headers=headers, json=image_request("image-public"))
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "completed"
        image = result["output"][0]["content"][0]
        assert image["type"] == "image" and image["checksum_sha256"] == hashlib.sha256(PNG).hexdigest()
        assert result["usage"]["total_tokens"] == 511 and result["usage"]["media"]["output_images"] == 1
        # Output is a real Asset owned by the caller, rather than embedded base64.
        download = client.get(f"/assets/{image['asset_id']}/content", headers=headers)
        assert download.status_code == 200, download.text
        assert download.content == PNG
        attempts = app.state.registry.store.attempts(1)
        detail = app.state.registry.store.attempt_detail(attempts[0]["request_id"], 1)
        assert SECRET not in json.dumps(detail)


@pytest.mark.parametrize("status,expected", [(400, "unsupported"), (401, "unknown"), (502, "unknown")])
def test_image_probe_preserves_unknown_for_authentication_and_temporary_failures(tmp_path, monkeypatch, status, expected):
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(status, json={"error": {"message": "server_is_overloaded"}})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="openai-images")
        response = mutate(client, "/upstreams/local/register", {"models": [{
            "upstream_model": "unlisted", "public_model": "pictures",
        }]})
        assert response.status_code == 200, response.text
        check = response.json()["detection"][0]["checks"]["image_generation"]
        assert check["status"] == expected and check["provider_status"] == status
        assert state(client)["models"][0]["verification"] == "unknown"
        assert not any(path.endswith("/images/edits") for path in paths)


@pytest.mark.parametrize("raw", [
    {"data": []},
    {"data": [{"b64_json": "not valid base64 !"}]},
    {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]},
    {"data": [{"url": "http://169.254.169.254/private"}]},
])
def test_invalid_image_responses_do_not_publish_fake_assets_or_fetch_urls(tmp_path, monkeypatch, raw):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=raw)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="openai-images")
        register(client, model="pictures", capabilities=CAPABILITIES)
        token = key(client)["token"]
        response = client.post("/model/responses", headers={
            "Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0",
            "Idempotency-Key": "req_bad-image",
        }, json=image_request("bad-image"))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "failed"
        assert response.json()["error"]["code"] == "PROVIDER_BAD_RESPONSE"
        assert response.json()["error"]["retryable"] is False
        assert len(calls) == 1
        assert not list((tmp_path / "data" / "assets").rglob("content"))


def test_discovery_separates_unverified_template_references_from_upstream_models(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch, lambda _: httpx.Response(
        200, json={"data": [{"id": "text-only"}]},
    )) as (client, _):
        response = mutate(client, "/upstreams", {"id": "images", "template_id": "codexmanager-images",
                                               "keys": [{"value": SECRET}]})
        assert response.status_code == 200, response.text
        response = client.post("/admin/api/upstreams/images/discover")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["models"] == ["text-only"] and result["source"] == "upstream"
        assert result["reference_models"] == ["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"]
        assert result["reference_verification"] == "unverified"
        assert state(client)["models"] == []
