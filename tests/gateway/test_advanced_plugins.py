"""Real ZIP -> process RPC -> Registry -> Kemo protocol; no paid upstream."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
import hashlib
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import stat
import threading
import zipfile

from fastapi.testclient import TestClient
import pytest
import yaml

from api.server import create_app
from core.config import PrincipalConfig, Settings
from core.models import KemoRequest
from gateway.domain import Catalog, PublicModel
from gateway.plugins.packages import unpack
from gateway.plugins.schema import PluginError
from tests.support.paths import PROJECT_ROOT

OWNER = "plugin-owner-test"
INVOKER = "plugin-invoker-test"
MODEL = "local-text-transform"


def package(*, version="1.0.0", name="local_text", secret=False, broken=False, slow=False, identity=None, bad_caps=False):
    source = PROJECT_ROOT / "examples/advanced_plugins" / name
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            content = path.read_bytes()
            if path.name == "plugin.yaml":
                manifest = yaml.safe_load(content)
                manifest["version"] = version
                if identity:
                    manifest["id"] = identity
                if secret:
                    manifest["config_schema"]["properties"]["credential"] = (
                        {"type": "string", "writeOnly": True} if secret is True else secret
                    )
                content = yaml.safe_dump(manifest, allow_unicode=True).encode()
            if path.name == "main.py" and broken:
                content = b"raise RuntimeError('bad candidate')\n"
            if path.name == "main.py" and slow:
                content = content.replace(b'result = await self.models_execute(params)',
                                          b'await asyncio.sleep(0.3)\n        result = await self.models_execute(params)')
            if path.name == "main.py" and bad_caps:
                content = content.replace(b'"local-text", "transform"', b'"wrong-provider", "transform"')
            archive.writestr(path.relative_to(source).as_posix(), content)
    return buffer.getvalue()


@contextmanager
def instance(tmp_path, monkeypatch, *, settings=None):
    monkeypatch.setenv("KEMO_MASTER_KEY", base64.b64encode(b"P" * 32).decode())
    settings = settings or Settings(api_keys={
        OWNER: PrincipalConfig("tenant", "owner", frozenset({"owner"})),
        INVOKER: PrincipalConfig("tenant", "caller", frozenset({"model:invoke"})),
        "denied": PrincipalConfig("tenant", "denied", frozenset({"model:invoke"}), allowed_models=frozenset()),
    })
    app = create_app(settings, live_config_root=tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer " + OWNER, "X-Kemo-Protocol-Version": "2.0"}) as client:
        yield client, app


def inventory(client):
    return client.get("/admin/api/advanced-plugins").json()


def mutate(client, identity, operation, value=None, *, expected=None):
    path = "/admin/api/advanced-plugins/" + identity
    path += "" if operation == "uninstall" else "/" + operation
    return client.request("DELETE" if operation == "uninstall" else "PATCH" if operation == "config" else "POST",
                          path, json={"expected_revision": expected or inventory(client)["revision"], "value": value or {}})


def install(client, content, *, update=False):
    preview = client.post("/admin/api/advanced-plugins/preview", files={"file": ("plugin.zip", content)} )
    assert preview.status_code == 200, preview.text
    return client.post("/admin/api/advanced-plugins/install", files={"file": ("plugin.zip", content)},
                       data={"expected_revision": inventory(client)["revision"], "sha256": preview.json()["sha256"],
                             "update": str(update).lower()})


def enable(client, identity="local_text"):
    manifest = next(item for item in inventory(client)["plugins"] if item["id"] == identity)
    return mutate(client, identity, "enable", {"grants": manifest["permissions"]["requested"]})


def invoke(client, identity, *, stream=False, token=INVOKER):
    identity = "req_" + identity
    return client.post("/model/responses", headers={"Authorization": "Bearer " + token,
        "Idempotency-Key": identity, "X-Kemo-Protocol-Version": "2.0"}, json={
        "protocol_version": "2.0", "request_id": identity, "model": MODEL, "stream": stream,
        "input": [{"id": "msg_user", "type": "message", "role": "user",
                   "content": [{"type": "text", "text": "Hello Kemo"}]}],
    })


def test_install_grant_hot_invoke_stream_replay_and_persistence(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        before = client.get("/healthz").json()["instance_id"]
        assert install(client, package()).status_code == 200
        recorded = app.state.registry.store._db.execute(
            "SELECT target FROM audit WHERE action='plugin-install' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        audit = json.loads(recorded[0])
        assert (audit["plugin_id"], audit["version"], audit["result"]) == ("local_text", "1.0.0", "success")
        assert client.get("/model/models", headers={"Authorization": "Bearer " + INVOKER}).json()["count"] == 0
        assert mutate(client, "local_text", "enable", {"grants": []}).status_code == 403
        enabled = enable(client)
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["plugins"][0]["status"] == "ready"
        catalog = client.get("/model/models", headers={"Authorization": "Bearer " + INVOKER}).json()
        assert catalog["data"][0]["id"] == MODEL
        response = invoke(client, "plugin_call")
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["status"] == "completed" and value["output"][0]["content"][0]["text"] == "HELLO KEMO"
        assert value["usage"]["measurement"]["exact"] is False
        assert invoke(client, "plugin_call").json()["id"] == value["id"]
        stream = invoke(client, "plugin_stream", stream=True)
        assert stream.status_code == 200, stream.text
        assert "output_text.delta" in stream.text and stream.text.count("event: response.completed") == 1
        assert client.get("/healthz").json()["instance_id"] == before
        assert app.state.registry.store.attempts(10)[0]["upstream_id"] == "plugin_local_text"
        # Key policies apply before execution and catalog discovery.
        assert invoke(client, "denied_call", token="denied").status_code == 403
        assert client.get("/model/models", headers={"Authorization": "Bearer denied"}).json()["count"] == 0
    with instance(tmp_path, monkeypatch) as (client, app):
        assert inventory(client)["plugins"][0]["status"] == "ready"
        assert invoke(client, "after_restart").json()["status"] == "completed"


def test_config_hot_generation_failure_update_rollback_and_secrets(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        assert install(client, package(secret=True)).status_code == 200
        assert enable(client).status_code == 200
        first = inventory(client)["revision"]
        assert mutate(client, "local_text", "config", {"mode": "lowercase", "credential": "very-private-plugin-secret"}).status_code == 200
        assert invoke(client, "lower").json()["output"][0]["content"][0]["text"] == "hello kemo"
        config = client.get("/admin/api/advanced-plugins/local_text/config").json()
        assert config["secrets_set"]["credential"] and "credential" not in config["values"]
        diagnostics = client.get("/admin/api/advanced-plugins/local_text/diagnostics").json()
        assert diagnostics["runtime"]["mode"] == "lowercase" and diagnostics["runtime"]["network"] == "not used"
        assert "very-private-plugin-secret" not in app.state.advanced_plugins.path.read_text(encoding="utf-8")
        assert mutate(client, "local_text", "reload", expected=first).status_code == 409
        failed = install(client, package(version="1.1.0", secret=True, broken=True), update=True)
        assert failed.status_code in {502, 503}, failed.text
        assert invoke(client, "old_still_ready").json()["status"] == "completed"
        assert inventory(client)["plugins"][0]["version"] == "1.0.0"
        assert inventory(client)["plugins"][0]["status"] == "degraded"
        updated = install(client, package(version="1.1.0", secret=True), update=True)
        assert updated.status_code == 200, updated.text
        assert client.get("/admin/api/advanced-plugins/local_text/config").json()["secrets_set"]["credential"]
        assert mutate(client, "local_text", "rollback").status_code == 200
        assert inventory(client)["plugins"][0]["version"] == "1.0.0"
        assert mutate(client, "local_text", "disable").status_code == 200
        assert invoke(client, "disabled").status_code == 404
        assert mutate(client, "local_text", "uninstall").status_code == 200
        assert not inventory(client)["plugins"]
        assert install(client, package(secret=True)).status_code == 200
        assert client.get("/admin/api/advanced-plugins/local_text/config").json()["secrets_set"]["credential"]
        assert client.get("/admin/api/advanced-plugins/local_text/config").json()["values"]["mode"] == "lowercase"


def test_status_reports_plugin_lifecycle_without_private_config_or_errors(tmp_path, monkeypatch):
    settings = Settings(status_token="status-only-token", api_keys={
        OWNER: PrincipalConfig("tenant", "owner", frozenset({"owner"})),
    })
    headers = {"Authorization": "Bearer status-only-token"}
    with instance(tmp_path, monkeypatch, settings=settings) as (client, app):
        assert install(client, package(secret=True)).is_success
        assert mutate(client, "local_text", "config", {"credential": "private-plugin-credential"}).is_success
        assert enable(client).is_success
        assert install(client, package(name="codex_cli")).is_success
        assert enable(client, "codex_cli").status_code == 422
        response = client.get("/status", headers=headers)
        assert response.status_code == 200, response.text
        items = {item["id"]: item for item in response.json()["plugins"]["items"]}
        assert items["local_text"]["status"] == "ready" and items["local_text"]["version"] == "1.0.0"
        assert items["local_text"]["models"] == [MODEL] and items["local_text"]["model_count"] == 1
        assert items["codex_cli"]["status"] == "needs_config"
        assert items["codex_cli"]["error_code"] == "PLUGIN_NEEDS_CONFIG"
        assert {"model": MODEL, "provider_id": "local-text"} in response.json()["registry"]["enabled_models"]
        assert "private-plugin-credential" not in response.text
        assert "config_schema" not in response.json()["plugins"]["items"][0]
        assert install(client, package(version="1.1.0", secret=True, broken=True), update=True).status_code in {502, 503}
        response = client.get("/status", headers=headers)
        item = next(row for row in response.json()["plugins"]["items"] if row["id"] == "local_text")
        assert item["status"] == "degraded" and item["model_count"] == 1 and item["error_code"]
        assert "bad candidate" not in response.text and "Traceback" not in response.text
        assert mutate(client, "local_text", "disable").is_success
        payload = client.get("/status", headers=headers).json()
        item = next(row for row in payload["plugins"]["items"] if row["id"] == "local_text")
        assert item["status"] == "disabled" and not item["enabled"] and not item["models"]
        assert "local-text" not in payload["registry"]["registered_provider_ids"]


def test_old_generation_can_finish_after_disable_and_cancel(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        assert install(client, package(slow=True)).status_code == 200
        assert enable(client).status_code == 200
        expected = inventory(client)["revision"]

        async def scenario():
            registry = app.state.registry
            old = registry.acquire_active(MODEL)
            await app.state.advanced_plugins.change("local_text", expected, "disable")
            with pytest.raises(LookupError):
                registry.resolve(MODEL)
            assert old.available and id(old) in registry._retired
            context = app.state.executor.make_context(tenant_id="tenant", subject_id="caller", request_id="old_generation")
            request = KemoRequest(protocol_version="2.0", request_id="req_old_generation", model=MODEL,
                                  input=[{"id": "msg_user", "type": "message", "role": "user", "content": [{"type": "text", "text": "old"}]}])
            assert (await old.execute(request, context)).status == "completed"
            await registry.release_registered(old)
            assert not old.available and id(old) not in registry._retired
        # Operate on the actual application event loop, as HTTP requests do.
        client.portal.call(scenario)


@pytest.mark.parametrize("name,mode", [
    ("../escape.py", 0), ("/absolute.py", 0), ("backend\\evil.py", 0),
    ("ui/link", stat.S_IFLNK | 0o777), ("C:/evil.py", 0),
])
def test_package_rejects_paths_and_links(tmp_path, name, mode):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        item = zipfile.ZipInfo(name)
        item.external_attr = mode << 16
        archive.writestr(item, b"unsafe")
    with pytest.raises(PluginError):
        with unpack(tmp_path, buffer.getvalue(), "2.0.0"):
            pytest.fail("unsafe ZIP accepted")
    assert not (tmp_path.parent / "escape.py").exists()


def test_ui_host_acl_sandbox_and_cli_without_config_registers_no_model(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        assert install(client, package()).status_code == 200
        assert install(client, package(name="codex_cli")).status_code == 200
        cli = enable(client, "codex_cli")
        assert cli.status_code == 422 and cli.json()["detail"]["code"] == "PLUGIN_NEEDS_CONFIG"
        assert not app.state.registry.providers
        asset = client.get("/admin/api/advanced-plugins/local_text/ui/assets/ui/index.html")
        assert asset.status_code == 200
        assert "sandbox allow-scripts" in asset.headers["content-security-policy"]
        assert "connect-src 'none'" in asset.headers["content-security-policy"]
        assert client.get("/admin/api/advanced-plugins/local_text/ui/assets/backend/main.py").status_code == 404
        ui = "/admin/api/advanced-plugins/local_text/ui/manifest"
        assert client.get(ui, headers={"Authorization": ""}).status_code == 401
        base = client.get(ui).json()["asset_base"]
        # Opaque-origin sandbox subresources do not send admin cookies.
        for path in ("ui/index.html", "ui/style.css", "ui/plugin.js"):
            asset = client.get(base + path, headers={"Authorization": ""})
            assert asset.status_code == 200
            assert "sandbox allow-scripts" in asset.headers["content-security-policy"]
            assert "connect-src 'none'" in asset.headers["content-security-policy"]
        assert client.get(base + "backend/main.py", headers={"Authorization": ""}).status_code == 404
        ticket = base.rstrip("/").rsplit("/", 1)[1]
        assert client.get("/admin/api/advanced-plugins", headers={"Authorization": "Bearer " + ticket}).status_code == 401
        manager = app.state.advanced_plugins
        identity, digest, expiry = manager._ui_access[ticket]
        manager._ui_access[ticket] = identity, digest, 0
        assert client.get(base + "ui/index.html", headers={"Authorization": ""}).status_code == 403
        base = client.get(ui).json()["asset_base"]
        assert install(client, package(version="1.1.0"), update=True).status_code == 200
        assert client.get(base + "ui/index.html", headers={"Authorization": ""}).status_code == 403
        base = client.get(ui).json()["asset_base"]
        assert mutate(client, "local_text", "uninstall").status_code == 200
        assert client.get(base + "ui/index.html", headers={"Authorization": ""}).status_code == 404
        assert client.post("/admin/api/advanced-plugins/preview", headers={"Authorization": "Bearer " + INVOKER},
                           files={"file": ("plugin.zip", package())}).status_code == 403
        denied = client.get("/admin/api/advanced-plugins", headers={"Authorization": ""})
        assert denied.status_code == 401


def test_cookie_management_mutations_require_csrf(tmp_path, monkeypatch):
    settings = Settings(host="0.0.0.0", web_token="control-token")
    with instance(tmp_path, monkeypatch, settings=settings) as (client, app):
        client.headers.pop("Authorization")
        session = client.post("/admin/api/auth/token", json={"token": "control-token"})
        assert session.status_code == 200
        denied = client.post("/admin/api/advanced-plugins/preview", files={"file": ("p.zip", package())})
        assert denied.status_code == 403
        accepted = client.post("/admin/api/advanced-plugins/preview",
                               headers={"X-CSRF-Token": session.json()["csrf_token"]},
                               files={"file": ("p.zip", package())})
        assert accepted.status_code == 200


def test_rpc_timeout_cancel_and_crash_are_isolated(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        assert install(client, package(slow=True)).status_code == 200
        assert enable(client).status_code == 200

        async def scenario():
            plugin = app.state.registry._external["local_text"]
            params = {"request": {"input": [{"content": [{"type": "text", "text": "cancel"}]}]}}
            async def consume():
                return [frame async for frame in plugin.host.frames("models.stream", params)]
            # Cancelling a request that is still waiting for a process slot must
            # leave the two running streams and their shared generation alive.
            running = [asyncio.create_task(consume()) for _ in range(2)]
            await asyncio.sleep(.03)
            waiting = asyncio.create_task(consume())
            await asyncio.sleep(.03)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert all((await asyncio.gather(*running))) and plugin.available
            with pytest.raises(PluginError) as timed_out:
                await anext(plugin.host.frames("models.stream", params, timeout=.05))
            assert timed_out.value.code == "PLUGIN_TIMEOUT"
            assert not plugin.host.pending and plugin.available
            pending = asyncio.create_task(anext(plugin.host.frames("models.stream", params)))
            await asyncio.sleep(.03)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not plugin.host.pending and plugin.available
            await plugin.host.kill()
        client.portal.call(scenario)
        assert inventory(client)["plugins"][0]["status"] == "failed"
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/model/models", headers={"Authorization": "Bearer " + INVOKER}).json()["count"] == 0
        assert mutate(client, "local_text", "reload").status_code == 200
        assert invoke(client, "recovered").json()["status"] == "completed"


@pytest.mark.parametrize("secret", [
    {"type": "string", "writeOnly": True, "default": "would-leak-through-manifest"},
    {"type": "object", "properties": {"nested": {"type": "string", "writeOnly": True}}},
    {"type": "number", "writeOnly": True},
    {"$ref": "http://127.0.0.1/private-schema"},
])
def test_invalid_config_schema_is_rejected_before_install(tmp_path, secret):
    with pytest.raises(PluginError) as rejected:
        with unpack(tmp_path, package(secret=secret), "2.0.0"):
            pytest.fail("unsupported secret schema accepted")
    assert rejected.value.code == "PLUGIN_INVALID_MANIFEST"


def test_duplicate_provider_and_bad_capabilities_keep_existing_routes(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        assert install(client, package()).status_code == 200
        assert enable(client).status_code == 200
        assert install(client, package(identity="duplicate")).status_code == 200
        assert enable(client, "duplicate").status_code == 409
        bad = install(client, package(version="1.1.0", bad_caps=True), update=True)
        assert bad.status_code == 502
        assert bad.json()["detail"]["code"] == "PLUGIN_PROTOCOL_ERROR"
        assert invoke(client, "survived_collision").json()["status"] == "completed"
        assert len(app.state.registry._external) == 1
        async def native_collision():
            registry = app.state.registry
            before = registry.current
            with pytest.raises(ValueError):
                await registry.publish(Catalog(models=(PublicModel(id="local-text"),)), before.revision, "test")
            assert registry.current is before
            assert registry.resolve(MODEL).provider_id == "local-text"
        client.portal.call(native_collision)


def test_real_http_catalog_preview_install_and_digest_validation(tmp_path, monkeypatch):
    content = package()
    resources = {}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            value = resources.get(self.path)
            self.send_response(200 if value else 404)
            self.end_headers()
            if value:
                self.wfile.write(value)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    resources["/plugin.zip"] = content
    rows = [{"id": "local_text", "name": "Local text", "version": "1.0.0", "category": "provider_extension",
             "url": base + "/plugin.zip", "sha256": hashlib.sha256(content).hexdigest()}]
    resources["/index.json"] = json.dumps({"schema_version": 1, "plugins": rows}).encode()
    try:
        with instance(tmp_path, monkeypatch) as (client, app):
            updated = client.put("/admin/api/advanced-plugins/sources", json={
                "expected_revision": inventory(client)["revision"],
                "value": {"sources": [{"id": "local-test", "name": "Local index", "url": base + "/index.json",
                                      "allow_private_network": True}]} })
            assert updated.status_code == 200, updated.text
            catalog = client.get("/admin/api/advanced-plugins/catalog")
            assert catalog.status_code == 200 and len(catalog.json()["items"]) == 1, catalog.text
            value = {"source_id": "local-test", "id": "local_text", "version": "1.0.0"}
            mutation = {"expected_revision": inventory(client)["revision"], "value": value}
            preview = client.post("/admin/api/advanced-plugins/catalog/preview", json=mutation)
            assert preview.status_code == 200 and preview.json()["manifest"]["id"] == "local_text"
            installed = client.post("/admin/api/advanced-plugins/catalog/install", json=mutation)
            assert installed.status_code == 200, installed.text
            assert enable(client).status_code == 200
            assert invoke(client, "catalog_call").json()["status"] == "completed"
            resources["/plugin.zip"] = package(version="1.1.0")
            mutation["expected_revision"] = inventory(client)["revision"]
            rejected = client.post("/admin/api/advanced-plugins/catalog/preview", json=mutation)
            assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "PLUGIN_INVALID_PACKAGE"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
