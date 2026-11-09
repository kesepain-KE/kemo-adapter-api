from __future__ import annotations

import asyncio
import io
from pathlib import Path
import zipfile

import pytest
import yaml

from tests.support.gateway import add_upstream, install_extension, instance, key, mutate, register
from tests.support.media import WAV, MP4, assert_asset, call
from core.assets import AssetStore
from core.models import KemoRequest
from core.provider_contract import RequestContext
from gateway.auth import RequestHeaders
from gateway.domain import Upstream
from gateway.extensions import WorkerAdapter, read_manifest


@pytest.mark.parametrize("kind,mime,content,operation", [
    ("audio", "audio/wav", WAV, "speech_to_speech"),
    ("video", "video/mp4", MP4, "video_generation"),
    ("file", "text/plain", b"user-created-file\n" * 180000, "conversation"),
], ids=["audio", "video", "file"])
def test_user_worker_media_crosses_process_as_owned_assets(tmp_path, monkeypatch, kind, mime, content, operation):
    modalities = ["text", "file"] if kind == "file" else [kind]
    manifest = {
        "id": "user-media", "name": "User media", "version": "1.0.0", "contract_version": "1.0",
        "operations": ["llm"], "discovery": False, "tools": False, "streaming": True,
        "auth_schemes": [{"id": "none", "location": "none"}], "usage_semantics": "unknown",
        "media": {"input": ["text", kind], "output": modalities},
    }
    code = '''
from core.provider_contract import ProviderEvent as Event, ProviderEventKind as Kind, ProviderResult
class Adapter:
    async def execute(self, request, context, model, upstream, headers):
        block = next(block for block in request.input[0].content if block.type != "text")
        asset = context.assets.resolve(block.asset_id)
        async def chunks():
            with asset.path.open("rb") as source:
                while chunk := source.read(65536):
                    yield chunk
        output = await context.assets.store_output(
            filename=asset.descriptor.filename, mime_type=asset.descriptor.mime_type, content=chunks())
        return ProviderResult("completed", [{
            "id": "msg_media", "type": "message", "role": "assistant",
            "content": ([{"type": "text", "text": "File generated."}] if "text" in request.output.modalities else []) +
                        [{"type": block.type, "asset_id": output.id,
                         "mime_type": output.mime_type, "checksum_sha256": output.checksum_sha256}]}])
    async def stream(self, *args):
        result = await self.execute(*args)
        yield Event(Kind.MEDIA_COMPLETED, item_id="msg_media", content_index=0, item=result.output[0])
        yield Event(Kind.COMPLETED, result=result)
def create_adapter(transport):
    return Adapter()
'''
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.yaml", yaml.safe_dump(manifest))
        archive.writestr("adapter.py", code)
    with instance(tmp_path, monkeypatch) as (client, _):
        installed = install_extension(client, "protocol_adapters", package.getvalue())
        assert installed.status_code == 200, installed.text
        enabled = mutate(client, "/extensions/protocol_adapters/user-media", {"enabled": True}, method="PATCH")
        assert enabled.status_code == 200, enabled.text
        add_upstream(client, adapter="user-media", auth_scheme="none")
        capabilities = {"streaming": True, "input_modalities": ["text", kind], "output_modalities": modalities}
        if operation != "conversation":
            capabilities["extensions"] = {"operations": {operation: True}}
        register(client, model="media", capabilities=capabilities)
        token = key(client)["token"]
        upload = client.post("/assets", headers={
            "Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0", "Idempotency-Key": "upload_media",
        }, files={"file": ("input." + {"audio": "wav", "video": "mp4", "file": "txt"}[kind], content, mime)})
        assert upload.status_code == 201, upload.text
        config = {kind: {"format": {"audio": "wav", "video": "mp4"}.get(kind)}} if kind != "file" else {"file": {}}
        blocks = [{"type": kind, "mime_type": mime, "asset_id": upload.json()["id"]}]
        response = call(client, token, "req_user_media", operation, modalities, blocks=blocks, config=config)
        assert response.status_code == 200, response.text
        complete = response.json()
        assert complete["status"] == "completed", complete.get("error")
        output = complete["output"][0]["content"][-1]
        assert output["asset_id"] != upload.json()["id"]
        assert_asset(client, token, output, content)
        response = call(client, token, "req_user_media_stream", operation, modalities, blocks=blocks,
                        config=config, stream=True)
        assert "event: output_media.completed" in response.text
        assert "event: response.completed" in response.text, response.text
        assert "kemo-worker-media-" not in response.text


def test_cancelled_media_worker_releases_files_before_temporary_directory_cleanup(tmp_path):
    (tmp_path / "manifest.yaml").write_text(yaml.safe_dump({
        "id": "cancel-media", "name": "Media cancellation", "version": "1.0.0", "contract_version": "1.0",
        "operations": ["llm"], "discovery": False, "tools": False, "streaming": True,
        "usage_semantics": "unknown", "media": {"input": ["text"], "output": ["file"]},
    }), encoding="utf-8")
    (tmp_path / "adapter.py").write_text('''
import asyncio
from pathlib import Path
from core.provider_contract import ProviderEvent as Event, ProviderEventKind as Kind
class Adapter:
    async def execute(self, *args): pass
    async def stream(self, request, context, model, upstream, headers):
        asset = await context.assets.store_output(filename="pending.txt", mime_type="text/plain", content=b"pending")
        path = context.assets.resolve(asset.id).path
        Path(__file__).with_name("temporary_path").write_text(str(path), encoding="utf-8")
        with path.open("rb"):
            try:
                yield Event(Kind.TEXT_DELTA, item_id="msg_wait", content_index=0, delta="waiting")
                await asyncio.Future()
            finally:
                Path(__file__).with_name("cancelled").touch()
def create_adapter(transport): return Adapter()
''', encoding="utf-8")

    async def scenario():
        adapter = await WorkerAdapter.start(tmp_path, read_manifest(tmp_path, "protocol_adapters"))
        stream = adapter.stream(
            KemoRequest.create(model="media", stream=True, input=[{
                "type": "message", "id": "msg_user", "role": "user",
                "content": [{"type": "text", "text": "create a file"}]}]),
            RequestContext("tenant", "user", "req_media", "resp_media", "trace_media",
                           assets=AssetStore(tmp_path / "assets").bind("tenant", "user")),
            "native", Upstream(id="media", name="Media", adapter_id="cancel-media",
                               base_url="https://example.com").model_dump(mode="json"), RequestHeaders(),
        )
        try:
            assert (await anext(stream)).delta == "waiting"
            temporary = Path((tmp_path / "temporary_path").read_text(encoding="utf-8"))
            assert temporary.exists()
            await stream.aclose()
            assert (tmp_path / "cancelled").exists()
            assert not temporary.exists()
            assert adapter.process.returncode is None
        finally:
            await stream.aclose()
            await adapter.close()
    asyncio.run(scenario())
