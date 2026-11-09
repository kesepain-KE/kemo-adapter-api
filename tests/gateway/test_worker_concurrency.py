import asyncio
import json
from pathlib import Path

import yaml
import pytest

from core.models import KemoRequest
from core.provider_contract import ProviderEventKind as Kind, RequestContext
from gateway.auth import RequestHeaders
from gateway.domain import Upstream
from gateway.extensions import WorkerAdapter, read_manifest
from tests.support.gateway import extension_zip, install_extension, invoke, key
from tests.support.gateway import SECRET, add_upstream, instance, mutate, register


@pytest.mark.parametrize("error_type", ["programming", "provider"])
def test_worker_error_is_redacted_preserved_and_never_retried(tmp_path, monkeypatch, error_type):
    code = '''
from core.models import ErrorObject
from core.provider_contract import ProviderException
from pathlib import Path
class Adapter:
    async def execute(self, request, context, model, upstream, headers):
        with Path(__file__).with_name("calls.txt").open("a") as output:
            output.write("called\\n")
        raise FAILURE
def create_adapter(transport):
    return Adapter()
'''
    failure = 'ValueError("deterministic error " + headers["Authorization"])'
    if error_type == "provider":
        failure = '''ProviderException(ErrorObject(
            type="validation", code="VALIDATION_ERROR", message="Invalid native parameter",
            retryable=False, provider_status=400,
            details={"reason":"INVALID_NATIVE_PARAMETER","echo":headers["Authorization"]},
        ))'''
    code = code.replace("FAILURE", failure)
    import io
    import zipfile
    manifest = {
        "id": "native-error", "name": "Native error", "version": "1.0.0", "contract_version": "1.0",
        "operations": ["llm"], "discovery": False, "streaming": False,
        "usage_semantics": "unknown", "auth_schemes": [
            {"id": "bearer", "location": "header", "name": "Authorization", "prefix": "Bearer "},
        ],
    }
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.yaml", yaml.safe_dump(manifest))
        archive.writestr("adapter.py", code)
    with instance(tmp_path, monkeypatch) as (client, app):
        assert install_extension(client, "protocol_adapters", package.getvalue()).status_code == 200
        assert mutate(client, "/extensions/protocol_adapters/native-error", {"enabled": True}, method="PATCH").status_code == 200
        add_upstream(client, adapter="native-error")
        register(client, capabilities={"streaming": False})
        # More than one eligible credential exposes accidental Router retries.
        assert mutate(client, "/upstreams/local", {"keys": [
            {"id": "primary"}, {"id": "secondary", "value": "second-fake-secret"},
        ]}, method="PATCH").status_code == 200
        response = invoke(client, key(client)["token"])
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "failed"
        assert response.json()["error"]["retryable"] is False
        assert SECRET not in response.text
        if error_type == "provider":
            error = response.json()["error"]
            assert error["type"] == "validation" and error["code"] == "VALIDATION_ERROR"
            assert error["provider_status"] == 400
            assert error["details"]["reason"] == "INVALID_NATIVE_PARAMETER"
        path = tmp_path / "extensions/protocol_adapters/native-error/calls.txt"
        assert path.read_text().splitlines() == ["called"]
        logs = client.get("/admin/api/extensions/protocol_adapters/native-error/logs")
        assert logs.status_code == 200
        text = "\n".join(logs.json()["lines"])
        assert "Traceback" in text and ("ValueError" if error_type == "programming" else "ProviderException") in text
        assert SECRET not in text and "second-fake-secret" not in text


def test_three_worker_streams_are_concurrent_and_cancel_is_local(tmp_path):
    manifest = {
        "id": "native-stream", "name": "Native stream", "version": "1.0.0", "contract_version": "1.0",
        "operations": ["llm"], "discovery": False, "streaming": True,
        "usage_semantics": "unknown", "auth_schemes": [{"id": "none", "location": "none"}],
    }
    (tmp_path / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (tmp_path / "adapter.py").write_text('''
import asyncio
from pathlib import Path
from core.provider_contract import ProviderEvent as Event, ProviderEventKind as Kind, ProviderResult
class Adapter:
    async def execute(self, *args):
        raise ValueError("stream only")
    async def stream(self, request, context, model, upstream, headers):
        root = Path(__file__).parent
        identity = context.subject_id
        (root / (identity + ".started")).touch()
        try:
            while len(list(root.glob("*.started"))) != 3:
                await asyncio.sleep(0.01)
            yield Event(Kind.TEXT_DELTA, item_id="msg_" + identity, content_index=0, delta=identity)
            while not (root / "release").exists():
                await asyncio.sleep(0.01)
            yield Event(Kind.COMPLETED, result=ProviderResult("completed", output=[{
                "id": "msg_" + identity, "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": identity}]}]))
        except asyncio.CancelledError:
            (root / (identity + ".cancelled")).touch()
            raise
def create_adapter(transport):
    return Adapter()
''', encoding="utf-8")

    async def scenario():
        adapter = await WorkerAdapter.start(tmp_path, read_manifest(tmp_path, "protocol_adapters"))
        upstream = Upstream(id="native", name="Native", adapter_id="native-stream",
                            base_url="https://example.com", auth_scheme="none").model_dump(mode="json")
        streams = [
            adapter.stream(KemoRequest.create(model="native", stream=True, input=[{
                "id": "msg_user", "type": "message", "role": "user", "content": [{"type": "text", "text": "hello"}],
            }]), RequestContext("tenant", str(index), f"req_{index}", f"resp_{index}", f"trace_{index}"),
                           "remote", upstream, RequestHeaders())
            for index in range(3)
        ]
        try:
            events = await asyncio.wait_for(asyncio.gather(*(anext(stream) for stream in streams)), timeout=10)
            assert [event.delta for event in events] == ["0", "1", "2"]
            await streams[0].aclose()
            async with asyncio.timeout(3):
                while not (tmp_path / "0.cancelled").exists():
                    await asyncio.sleep(0.01)
            assert adapter.process.returncode is None
            (tmp_path / "release").touch()
            results = await asyncio.wait_for(asyncio.gather(
                *(_collect(stream) for stream in streams[1:]),
            ), timeout=5)
            assert all(events[-1].kind == Kind.COMPLETED for events in results)
            assert not (tmp_path / "1.cancelled").exists() and not (tmp_path / "2.cancelled").exists()
        finally:
            for stream in streams:
                await stream.aclose()
            await adapter.close()

    asyncio.run(scenario())


async def _collect(stream):
    return [event async for event in stream]


def test_native_protocol_extension_requires_no_core_changes(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import io
    import threading
    import zipfile
    from tests.support.paths import PROJECT_ROOT

    seen = []

    class NativeServer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({"engines": ["native-v1"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(request)
            assert request["engine"] == "native-v1"
            assert "messages" not in request and "model" not in request
            body = (b'data: {"delta":"native answer"}\n\ndata: {"done":true,"usage":{"in":2,"out":3}}\n\n'
                    if request["stream"] else b'{"answer":"native answer","usage":{"in":2,"out":3}}')
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if request["stream"] else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), NativeServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest = yaml.safe_load((PROJECT_ROOT / "templates/protocol_adapter/manifest.yaml").read_text(encoding="utf-8"))
    manifest["id"] = "native-example"
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.yaml", yaml.safe_dump(manifest))
        archive.writestr("adapter.py", (PROJECT_ROOT / "templates/protocol_adapter/adapter.py").read_bytes())
    try:
        with instance(tmp_path, monkeypatch) as (client, _):
            assert install_extension(client, "protocol_adapters", package.getvalue()).status_code == 200
            assert mutate(client, "/extensions/protocol_adapters/native-example", {"enabled": True}, method="PATCH").status_code == 200
            response = mutate(client, "/upstreams", {
                "id": "native", "name": "Native", "adapter_id": "native-example", "auth_scheme": "none",
                "base_url": f"http://127.0.0.1:{server.server_port}", "allow_private_network": True,
            })
            assert response.status_code == 200, response.text
            assert client.post("/admin/api/upstreams/native/discover").json()["models"] == ["native-v1"]
            response = mutate(client, "/upstreams/native/register", {"models": [{
                "public_model": "stable", "upstream_model": "native-v1", "capabilities": {"streaming": True},
            }]})
            assert response.status_code == 200, response.text
            token = key(client)["token"]
            assert invoke(client, token).json()["output"][0]["content"][0]["text"] == "native answer"
            streamed = invoke(client, token, identity="req_native_stream", stream=True)
            assert "event: output_text.delta" in streamed.text and "event: response.completed" in streamed.text
            assert len(seen) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
