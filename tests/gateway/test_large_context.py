"""Large request boundaries, upstream timeouts and bounded diagnostic storage."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import socket
import sqlite3
import time

import httpx
import pytest
from fastapi import FastAPI

from api.request_limits import RequestBodyLimitMiddleware
from api.server import create_app
from core.config import Settings
from core.provider_contract import ProviderException
from gateway.domain import Upstream
from gateway.tracing import AttemptTrace
from gateway.transport import Transport
from tests.support.gateway import SECRET, add_upstream, chat_response, instance, mutate, register


MIB = 1024 * 1024


@pytest.mark.parametrize("size", [2 * MIB + 10, 32 * MIB, 32 * MIB + 1])
@pytest.mark.parametrize("chunked", [False, True])
def test_request_size_boundary_before_json_parsing(size, chunked):
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=32 * MIB,
                       paths=frozenset({"/model/responses"}))

    @app.post("/model/responses")
    async def receive(payload: dict):
        return {"length": len(payload["text"])}

    async def scenario():
        body = b'{"text":"' + b"x" * (size - 11) + b'"}'
        assert len(body) == size

        async def chunks():
            for offset in range(0, size, MIB):
                yield body[offset:offset + MIB]

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as client:
            response = await client.post("/model/responses", content=chunks() if chunked else body,
                                         headers={"Content-Type": "application/json"})
        if size <= 32 * MIB:
            assert response.status_code == 200
            assert response.json()["length"] == size - 11
        else:
            assert response.status_code == 413
            error = response.json()
            assert error["object"] == "kemo.error"
            assert error["error"]["code"] == "REQUEST_TOO_LARGE"
            assert error["error"]["details"]["limit_bytes"] == 33554432

    asyncio.run(scenario())


@pytest.mark.parametrize(("size", "limit", "accepted"), [
    (9 * MIB, None, True), (32 * MIB, None, True), (32 * MIB + 1, None, False),
    (33 * MIB, 34 * MIB, True), (1025, 1024, False),
])
def test_upstream_json_default_and_configurable_boundary(size, limit, accepted):
    async def scenario():
        body = b'{"text":"' + b"x" * (size - 11) + b'"}'
        transport = Transport(http_transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=body)),
            **({"json_max_bytes": limit} if limit else {}))
        upstream = Upstream(id="local", name="Local", adapter_id="openai-chat", base_url="http://127.0.0.1",
                            auth_scheme="none", allow_private_network=True)
        try:
            if accepted:
                result = await transport.json(upstream, "/chat", {})
                assert len(result["text"]) == size - 11
            else:
                with pytest.raises(ProviderException) as caught:
                    await transport.json(upstream, "/chat", {})
                assert caught.value.error.code == "PROVIDER_BAD_RESPONSE"
                assert caught.value.error.details["reason"] == "UPSTREAM_RESPONSE_TOO_LARGE"
        finally:
            await transport.close()

    asyncio.run(scenario())


def test_http_connect_timeout_is_short_and_read_timeout_is_provider_timeout():
    async def scenario():
        async def handler(request):
            assert request.extensions["timeout"] == {
                "connect": 15.0, "read": 1100, "write": 1100, "pool": 1100,
            }
            raise httpx.ReadTimeout("upstream did not respond", request=request)

        transport = Transport(http_transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ProviderException) as caught:
                await transport.json(Upstream(id="slow", name="Slow", adapter_id="openai-chat",
                                              base_url="http://127.0.0.1", timeout=1100), "/chat", {})
            assert caught.value.error.code == "PROVIDER_TIMEOUT"
            assert caught.value.error.retryable
        finally:
            await transport.close()

    asyncio.run(scenario())


def test_dns_failure_is_provider_unavailable(monkeypatch):
    async def scenario():
        async def failed_dns(*args, **kwargs):
            raise socket.gaierror("offline DNS failure")

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", failed_dns)
        transport = Transport()
        try:
            with pytest.raises(ProviderException) as caught:
                await transport.json(Upstream(id="dns", name="DNS", adapter_id="openai-chat",
                                              base_url="https://unresolvable.invalid"), "/chat", {})
            assert caught.value.error.code == "PROVIDER_UNAVAILABLE"
            assert caught.value.error.retryable
        finally:
            await transport.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["a" * 2000, '中文"\\\n' * 1000])
def test_trace_truncation_includes_metadata_escaping_and_utf8_in_budget(text):
    trace = AttemptTrace({"text": SECRET + text}, secrets=(SECRET,))
    complete = trace.payload(64 * 1024)
    assert complete["request"]["text"] == "[REDACTED]" + text
    truncated = trace.payload(256)
    encoded = json.dumps(truncated, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 256
    assert truncated["truncated"] is True
    assert truncated["original_bytes"] == len(json.dumps(complete, ensure_ascii=False).encode("utf-8"))
    assert SECRET not in truncated["body"]
    assert "[REDACTED]" in truncated["body"]
    assert truncated["body"].encode("utf-8").decode("utf-8") == truncated["body"]


def test_details_stay_in_memory_until_finish_and_preserve_response_and_usage(tmp_path, monkeypatch):
    seen_start = []
    output = "untruncated response " * 200

    def handler(_):
        with app.state.registry.store._lock:
            row = app.state.registry.store._db.execute(
                "SELECT body FROM attempt_details ORDER BY rowid DESC LIMIT 1").fetchone()
        assert row is None
        attempt = app.state.registry.store.attempts(1)[0]
        body = app.state.registry.router.metering.detail(attempt["request_id"], 1)
        seen_start.append(body)
        assert len(json.dumps(body, ensure_ascii=False).encode("utf-8")) <= 1024
        return httpx.Response(200, json=chat_response(SECRET + output))

    with instance(tmp_path, monkeypatch, handler, Settings(log_detail_max_bytes=1024)) as (client, app):
        add_upstream(client)
        register(client, capabilities={"limits": {"context_window": 3_000_000}})
        token = mutate(client, "/keys", {"name": "large-context"}, key=True).json()["token"]
        response = client.post("/model/responses", headers={
            "Authorization": f"Bearer {token}", "X-Kemo-Protocol-Version": "2.0",
            "Idempotency-Key": "req_large_details",
        }, json={
            "protocol_version": "2.0", "request_id": "req_large_details",
            "model": "stable", "stream": False,
            "input": [{"type": "message", "id": "msg_user", "role": "user",
                       "content": [{"type": "text", "text": "large prompt " * 2000}]}],
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "completed"
        assert body["output"][0]["content"][0]["text"] == SECRET + output
        assert body["usage"]["input_tokens"] == 3 and body["usage"]["output_tokens"] == 2
        assert seen_start[0]["truncated"]
        row = client.get("/admin/api/logs").json()["items"][0]
        detail = client.get(f"/admin/api/logs/{row['request_id']}/1").json()
        assert detail["detail"]["truncated"]
        assert SECRET not in json.dumps(detail)
        with app.state.registry.store._lock:
            stored = app.state.registry.store._db.execute("SELECT body FROM attempt_details").fetchone()[0]
        assert len(stored.encode("utf-8")) <= 1024
        assert row["source"] == "exact" and row["usage"]["total_tokens"] == 5
        assert not app.state.registry.router.metering._active


def test_new_limits_are_injected_by_application(tmp_path, monkeypatch):
    settings = Settings(upstream_json_max_bytes=64 * MIB, log_detail_max_bytes=2048)
    app = create_app(settings, live_config_root=tmp_path)
    transport = app.state.registry.transport_factory()
    try:
        assert transport.json_max_bytes == 64 * MIB
        assert app.state.registry.router.metering.log_detail_max_bytes == 2048
    finally:
        asyncio.run(transport.close())
        app.state.registry.store.close()


def test_new_defaults_and_startup_overrides(monkeypatch, tmp_path):
    names = ("REQUEST_JSON_MAX_BYTES", "UPSTREAM_JSON_MAX_BYTES", "LOG_DETAIL_MAX_BYTES",
             "LOG_RETENTION_DAYS", "MODEL_EXECUTION_TIMEOUT_SECONDS")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    defaults = Settings.from_env()
    assert defaults.request_json_max_bytes == defaults.upstream_json_max_bytes == 32 * MIB
    assert defaults.log_detail_max_bytes == 4 * MIB
    assert defaults.log_retention_days == 30 and defaults.execution_retention_hours == 720
    assert defaults.model_execution_timeout_seconds == 1200
    from web.backend.startup_config import config_view, save_config
    save_config(tmp_path, config_view(tmp_path)["expected_revision"], """
UPSTREAM_JSON_MAX_BYTES: 67108864
LOG_DETAIL_MAX_BYTES: 2097152
RESTART_DRAIN_TIMEOUT: 1300
MODEL_EXECUTION_TIMEOUT_SECONDS: 1200
LOG_RETENTION_DAYS: 30
""", {})
    assert "67108864" in config_view(tmp_path)["yaml"]
    monkeypatch.setenv("UPSTREAM_JSON_MAX_BYTES", str(64 * MIB))
    monkeypatch.setenv("LOG_DETAIL_MAX_BYTES", str(2 * MIB))
    assert Settings.from_env().upstream_json_max_bytes == 64 * MIB
    assert Settings.from_env().log_detail_max_bytes == 2 * MIB
    monkeypatch.setenv("LOG_DETAIL_MAX_BYTES", "255")
    with pytest.raises(ValueError, match="LOG_DETAIL_MAX_BYTES"):
        Settings.from_env()


def test_thirty_day_retention_covers_details_statistics_executions_and_sse(tmp_path, monkeypatch):
    frames = 'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
    frames += 'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
    with instance(tmp_path, monkeypatch, lambda _: httpx.Response(200, text=frames)) as (client, app):
        add_upstream(client)
        register(client)
        token = mutate(client, "/keys", {"name": "retention"}, key=True).json()["token"]
        for label in ("kept", "expired"):
            identity = "req_" + label
            response = client.post("/model/responses", headers={
                "Authorization": f"Bearer {token}", "X-Kemo-Protocol-Version": "2.0",
                "Idempotency-Key": identity,
            }, json={
                "protocol_version": "2.0", "request_id": identity, "model": "stable", "stream": True,
                "input": [{"type": "message", "id": "msg_user", "role": "user",
                           "content": [{"type": "text", "text": "hello"}]}],
            })
            assert "response.completed" in response.text
        now = datetime.now(timezone.utc)
        stats = app.state.statistics
        paths = {}
        for label, age in (("kept", 28), ("expired", 31)):
            at = now - timedelta(days=age)
            with closing(sqlite3.connect(app.state.executions.path)) as db, db:
                db.execute("UPDATE executions SET updated_at=? WHERE request_id=?",
                           (time.time() - age * 86400, "req_" + label))
                response_id = db.execute("SELECT response_id FROM executions WHERE request_id=?",
                                         ("req_" + label,)).fetchone()[0]
            with app.state.registry.store.transaction() as db:
                db.execute("UPDATE attempts SET started_at=? WHERE request_id=?", (at.isoformat(), response_id))
            day = at.astimezone(stats.timezone).date().isoformat()
            paths[label] = stats._path_for_day(day)
            stats._initialize_database(paths[label])
        with app.state.registry.store.transaction() as db:
            app.state.registry.store._next_details_cleanup = 0
            app.state.registry.store.prune_details(db)
            assert db.execute("SELECT COUNT(*) FROM attempt_details").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2
        client.portal.call(stats.cleanup_expired)
        client.portal.call(app.state.executions.cleanup_expired)
        assert paths["kept"].exists() and not paths["expired"].exists()
        with closing(sqlite3.connect(app.state.executions.path)) as db:
            assert db.execute("SELECT request_id FROM executions").fetchall() == [("req_kept",)]
            kept = db.execute("SELECT response_id FROM executions").fetchone()[0]
            assert db.execute("SELECT DISTINCT response_id FROM execution_events").fetchall() == [(kept,)]
