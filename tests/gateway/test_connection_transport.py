import asyncio
import json
from pathlib import Path
import sqlite3

import httpx
import pytest
import yaml

from core.provider_contract import ProviderEventKind as Kind
from gateway.auth import RequestHeaders
from gateway.domain import Upstream
from gateway.store import Store
from gateway.transport import Transport
from tests.support.gateway import SECRET, add_upstream, chat_response, instance, mutate, register, state


@pytest.mark.parametrize("exception,code", [
    (httpx.ConnectError, "PROVIDER_UNAVAILABLE"),
    (httpx.ReadTimeout, "PROVIDER_TIMEOUT"),
    (httpx.WriteError, "PROVIDER_BAD_RESPONSE"),
    (httpx.RemoteProtocolError, "PROVIDER_BAD_RESPONSE"),
])
def test_transport_errors_keep_safe_type_and_phase(exception, code):
    from core.provider_contract import ProviderException

    def fail(request):
        raise exception(f"{SECRET} https://private.invalid/?api_key={SECRET}", request=request)

    async def scenario():
        transport = Transport(http_transport=httpx.MockTransport(fail))
        upstream = Upstream(id="offline", name="Offline", adapter_id="openai-chat",
                            base_url="https://provider.invalid", auth_scheme="none")
        try:
            with pytest.raises(ProviderException) as caught:
                await transport.json(upstream, "/chat/completions", {}, body={})
            error = caught.value.error
            assert error.code == code
            assert error.details["transport_exception"] == exception.__name__
            assert error.details["transport_phase"] == "response_headers"
            assert SECRET not in error.model_dump_json()
            assert "private.invalid" not in error.model_dump_json()
        finally:
            await transport.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
def test_response_body_disconnect_keeps_phase_and_closes_stream(stream):
    from core.provider_contract import ProviderException

    closed = []

    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"text":"partial"}\n\n' if stream else b'{"text":"partial'
            raise httpx.ReadError(f"connection interrupted: {SECRET}")

        async def aclose(self):
            closed.append(True)

    async def scenario():
        transport = Transport(http_transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=BrokenBody()),
        ))
        upstream = Upstream(id="offline", name="Offline", adapter_id="openai-chat",
                            base_url="https://provider.invalid", auth_scheme="none")
        try:
            with pytest.raises(ProviderException) as caught:
                if stream:
                    events = transport.events(upstream, "/chat/completions", {}, {})
                    assert await anext(events) == {"text": "partial"}
                    await anext(events)
                else:
                    await transport.json(upstream, "/chat/completions", {}, body={})
            error = caught.value.error
            assert error.details["transport_exception"] == "ReadError"
            assert error.details["transport_phase"] == "response_body"
            assert SECRET not in error.model_dump_json()
            assert closed == [True]
        finally:
            await transport.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("scheme,adapter,header", [
    ("azure-key", "openai-chat", "api-key"),
    ("api-key", "openai-chat", "x-api-key"),
    ("gemini-query", "gemini", None),
])
def test_connection_auth_and_extra_parameters_reach_wire(tmp_path, monkeypatch, stream, scheme, adapter, header):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.params["api-version"] == "offline"
        if header:
            assert request.headers[header] == SECRET
        else:
            assert request.url.params["key"] == SECRET
            assert "x-goog-api-key" not in request.headers
        assert json.loads(request.content)["enable_search"] is True
        if adapter == "gemini":
            raw = {"candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}]}
            frames = [raw]
        else:
            raw = chat_response()
            frames = [{"choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}]}]
        return httpx.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames),
                              headers={"Content-Type": "text/event-stream"}) if stream else httpx.Response(200, json=raw)

    with instance(tmp_path, monkeypatch, handler) as (client, app):
        add_upstream(client, adapter=adapter, auth_scheme=scheme)
        assert mutate(client, "/upstreams/local", {"profile": {
            "extra_body": {"enable_search": True}, "extra_query": {"api-version": "offline"},
        }}, method="PATCH").status_code == 200
        register(client)
        from tests.support.gateway import invoke, key
        response = invoke(client, key(client)["token"], stream=stream)
        assert response.status_code == 200, response.text
        assert len(seen) == 1
        detail = app.state.registry.store.attempt_detail(
            app.state.registry.store.attempts(1)[0]["request_id"], 1,
        )
        assert SECRET not in json.dumps(detail)


@pytest.mark.parametrize("profile,field", [
    ({"extra_body": {"model": "wrong"}}, "profile.extra_body"),
    ({"extra_body": {"temperature": 1}}, "profile.extra_body"),
    ({"extra_query": {"key": "secret"}}, "profile.extra_query"),
])
def test_conflicting_parameters_rejected_when_saved(tmp_path, monkeypatch, profile, field):
    with instance(tmp_path, monkeypatch) as (client, _):
        add_upstream(client)
        before = state(client)["revision"]
        response = mutate(client, "/upstreams/local", {"profile": profile}, method="PATCH")
        assert response.status_code == 422
        assert response.json()["detail"]["field"] == field
        assert state(client)["revision"] == before


def test_query_credential_is_redacted_when_upstream_echoes_encoded_url(tmp_path, monkeypatch):
    from urllib.parse import quote, quote_plus
    from tests.support.gateway import invoke, key
    secret = "fake/key+with=punct&spaces here"

    def handler(request):
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}],
            "diagnostic": str(request.url),
        })

    with instance(tmp_path, monkeypatch, handler) as (client, app):
        add_upstream(client, adapter="gemini", auth_scheme="gemini-query")
        assert mutate(client, "/upstreams/local", {"keys": [{"id": "primary", "value": secret}]}, method="PATCH").status_code == 200
        assert mutate(client, "/upstreams/local", {"profile": {"extra_query": {"api-version": "test"}}}, method="PATCH").status_code == 200
        register(client)
        response = invoke(client, key(client)["token"])
        assert response.json()["status"] == "completed"
        detail = json.dumps(app.state.registry.store.attempt_detail(
            app.state.registry.store.attempts(1)[0]["request_id"], 1,
        ))
        assert "[REDACTED]" in detail
        assert all(value not in detail for value in (secret, quote(secret, safe=""), quote_plus(secret, safe="")))


def test_save_does_not_resolve_dns_and_reports_template_placeholder(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, _):
        value = {"id": "offline", "name": "Offline", "adapter_id": "openai-chat",
                 "base_url": "https://does-not-exist.invalid", "auth_scheme": "none"}
        assert mutate(client, "/upstreams", value).status_code == 200
        response = mutate(client, "/upstreams/offline", {"base_url": "https://{region}.example.com"}, method="PATCH")
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "TEMPLATE_PLACEHOLDER_UNRESOLVED"
        assert response.json()["detail"]["details"]["placeholders"] == ["region"]


@pytest.mark.parametrize("status,expected", [(401, 502), (403, 502), (404, 200), (200, 200)])
def test_discovery_never_hides_authentication_failure(tmp_path, monkeypatch, status, expected):
    def handler(request):
        return httpx.Response(status, json={"data": []})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        response = client.post("/admin/api/upstreams/local/discover")
        assert response.status_code == expected
        if expected == 502:
            assert response.json()["detail"]["provider_status"] == status
            assert "models" not in response.json()
        else:
            assert response.json()["verification"] == "unverified"


def test_legacy_auth_conversion_is_once_and_keeps_credentials(tmp_path, caplog):
    store = Store(tmp_path)
    with store.transaction() as db:
        for identity, adapter, scheme in [("relay", "openai-chat", "api-key"), ("native", "gemini", "gemini-query")]:
            document = {"id": identity, "name": identity, "adapter_id": adapter,
                        "base_url": "https://example.com", "auth_type": "api-key",
                        "credentials": [{"id": "saved", "encrypted": "unchanged", "preview": "***"}]}
            db.execute("INSERT INTO upstreams VALUES(?,?)", (identity, yaml.safe_dump(document)))
    store.close()
    store = Store(tmp_path)
    store._migrate_auth_schemes()
    try:
        assert {item.id: item.auth_scheme for item in store.catalog().upstreams} == {"relay": "api-key", "native": "gemini-query"}
        assert all(item.credentials[0].encrypted == "unchanged" for item in store.catalog().upstreams)
        assert store._db.execute("SELECT COUNT(*) FROM audit WHERE action='auth-scheme-conversion'").fetchone()[0] == 1
    finally:
        store.close()
    store = Store(tmp_path)
    store._migrate_auth_schemes()
    assert store._db.execute("SELECT COUNT(*) FROM audit WHERE action='auth-scheme-conversion'").fetchone()[0] == 1
    store.close()


def test_proxy_clients_are_separate_and_targets_are_still_checked(monkeypatch):
    async def scenario():
        received = [[], []]

        async def proxy(reader, writer, index):
            head = await reader.readuntil(b"\r\n\r\n")
            received[index].append(head.split(b"\r\n")[0])
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"ok":true}')
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        servers = [await asyncio.start_server(lambda r, w, i=i: proxy(r, w, i), "127.0.0.1", 0) for i in range(2)]
        import gateway.transport as module
        original = module.safe_addresses

        async def addresses(url, allowed):
            if url.startswith("http://target.example"):
                return ("93.184.216.34",)
            return await original(url, allowed)

        monkeypatch.setattr(module, "safe_addresses", addresses)
        transport = Transport()
        try:
            for server in servers:
                upstream = Upstream(id="relay", name="Relay", adapter_id="openai-chat",
                                    base_url="http://target.example", proxy=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
                assert await transport.json(upstream, "/test", {}, method="GET") == {"ok": True}
            assert len(transport._proxy_clients) == 2
            assert all(len(rows) == 1 and b"93.184.216.34" in rows[0] for rows in received)
            upstream = Upstream(id="bad", name="Bad", adapter_id="openai-chat",
                                base_url="http://169.254.169.254", proxy=f"http://127.0.0.1:{servers[0].sockets[0].getsockname()[1]}")
            with pytest.raises(ValueError):
                await transport.json(upstream, "/test", {}, method="GET")
            assert len(received[0]) == 1
        finally:
            await transport.close()
            for server in servers:
                server.close()
                await server.wait_closed()

    asyncio.run(scenario())
