from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import asyncio
import zlib

import httpx
import pytest

from core.models import Usage
from core.config import PrincipalConfig, Settings
from gateway.keys import KeyPolicyError, KeyService
from gateway.security import SecretVault
from gateway.store import Store, dump
from gateway.token_counting import count_text, estimate_usage, select_tokenizer
from gateway.tracing import AttemptTrace, current_trace
from tests.support.gateway import SECRET, add_upstream, chat_response, instance, mutate, register, state


def create_key(client):
    result = mutate(client, "/keys", {"name": "integration caller"}, key=True)
    assert result.status_code == 200, result.text
    return result.json()


def invoke(client, token, identity, *, text="hello", stream=False):
    return client.post("/model/responses", headers={
        "Authorization": f"Bearer {token}", "X-Kemo-Protocol-Version": "2.0", "Idempotency-Key": identity,
    }, json={
        "protocol_version": "2.0", "request_id": identity, "model": "stable", "stream": stream,
        "input": [{"type": "message", "id": "msg_user", "role": "user",
                   "content": [{"type": "text", "text": text}]}],
    })


def test_downstream_key_encrypted_copy_delete_and_conflict(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        add_upstream(client)
        register(client)
        key = create_key(client)
        current = state(client)
        assert current["keys"][0]["can_copy"]
        assert key["token"] not in json.dumps(current)
        copied = mutate(client, f"/keys/{key['id']}/reveal", key=True)
        assert copied.status_code == 200 and copied.json()["token"] == key["token"]
        assert "no-store" in copied.headers["cache-control"].split(", ")
        assert invoke(client, copied.json()["token"], "req_before_delete").json()["status"] == "completed"
        assert mutate(client, f"/keys/{key['id']}", {"name": "renamed"}, method="PATCH", key=True).status_code == 200
        assert mutate(client, f"/keys/{key['id']}", method="DELETE", key=True,
                      expected=current["key_revision"]).status_code == 409
        assert mutate(client, f"/keys/{key['id']}", method="DELETE", key=True).status_code == 200
        assert invoke(client, key["token"], "req_after_delete").status_code == 401
        assert len(client.get("/admin/api/logs").json()["items"]) == 1
        assert key["token"].encode() not in app.state.registry.store.path.read_bytes()
        with app.state.registry.store._lock:
            actions = [r[0] for r in app.state.registry.store._db.execute("SELECT action FROM audit")]
        assert "copy-key" in actions and "delete-key" in actions


def test_hash_only_database_migrates_without_losing_legacy_key(tmp_path):
    db = sqlite3.connect(tmp_path / "gateway.sqlite3")
    db.execute("""CREATE TABLE downstream_keys(
        id TEXT PRIMARY KEY,prefix TEXT NOT NULL,key_hash TEXT NOT NULL UNIQUE,
        body TEXT NOT NULL,created_at TEXT NOT NULL)""")
    db.execute("INSERT INTO downstream_keys VALUES(?,?,?,?,?)",
               ("legacy", "old-prefix", "old-hash", dump({"name": "old"}), "2026-11-01T00:00:00+00:00"))
    db.commit()
    db.close()
    store = Store(tmp_path)
    try:
        keys = KeyService(store, SecretVault(tmp_path))
        assert keys.list()[0]["id"] == "legacy" and keys.list()[0]["can_copy"] is False
        with pytest.raises(KeyPolicyError, match="哈希"):
            keys.reveal("legacy")
        keys.patch("legacy", {"enabled": False})
        assert keys.list()[0]["enabled"] is False
    finally:
        store.close()


def test_secret_copy_requires_owner_and_cookie_csrf(tmp_path, monkeypatch):
    settings = Settings(host="0.0.0.0", web_token="owner-login", api_keys={
        "read-admin": PrincipalConfig("gateway", "admin", frozenset({"admin:web"})),
        "key-owner": PrincipalConfig("gateway", "owner", frozenset({"owner"})),
    })
    with instance(tmp_path, monkeypatch, settings=settings) as (client, app):
        identity, token = app.state.registry.keys.create({"name": "private"})
        headers = {"Authorization": "Bearer key-owner"}
        current = client.get("/admin/api/console", headers=headers).json()
        body = {"expected_revision": current["key_revision"], "value": {}}
        path = f"/admin/api/keys/{identity}/reveal"
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json=body, headers={"Authorization": "Bearer read-admin"}).status_code == 403
        assert client.post(path, json=body, headers=headers).json()["token"] == token
        session = client.post("/admin/api/auth/token", json={"token": "owner-login"}).json()
        assert client.post(path, json=body).status_code == 403
        assert client.post(path, json=body, headers={"X-CSRF-Token": session["csrf_token"]}).json()["token"] == token
        assert client.post(path, json=body, headers={
            "X-CSRF-Token": session["csrf_token"], "Origin": "http://another.example",
        }).status_code == 403


def test_credential_priority_rotation_and_actual_health(tmp_path, monkeypatch):
    used = []
    def handler(request):
        value = request.headers["Authorization"].removeprefix("Bearer ")
        used.append(value)
        return httpx.Response(401 if value == "invalid-primary" else 200, json=chat_response())
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        register(client)
        assert state(client)["upstreams"][0]["credentials"][0]["preview"] == f"{SECRET[:10]}…{SECRET[-5:]}"
        assert mutate(client, "/upstreams/local", {"keys": [
            {"id": "bad", "value": "invalid-primary", "priority": 20},
            {"id": "good", "value": "working-primary", "priority": 10},
            {"id": "reserve", "value": "working-reserve", "priority": -10},
        ]}, method="PATCH").status_code == 200
        assert state(client)["credential_health"]["local"]["bad"]["status"] == "unknown"
        token = create_key(client)["token"]
        assert invoke(client, token, "req_failover").json()["status"] == "completed"
        assert used == ["invalid-primary", "working-primary"]
        health = state(client)["credential_health"]["local"]
        assert health["bad"]["status"] == "invalid" and health["good"]["status"] == "healthy"
        assert health["bad"]["checked_at"] and health["reserve"]["status"] == "unknown"
        assert invoke(client, token, "req_next").json()["status"] == "completed"
        assert used[-1] == "working-primary"
        assert mutate(client, "/upstreams/local", {"keys": [
            {"id": "good", "priority": 10}, {"id": "reserve", "priority": 10},
        ]}, method="PATCH").status_code == 200
        used.clear()
        for index in range(2):
            assert invoke(client, token, f"req_rotation_{index}").json()["status"] == "completed"
        assert set(used) == {"working-primary", "working-reserve"}
        logs = client.get("/admin/api/logs").json()["items"]
        assert {item["credential_id"] for item in logs} == {"bad", "good", "reserve"}
        assert state(client)["upstreams"][0]["credentials"][0]["preview"] == "***"  # Short secrets stay masked.


def test_logs_full_payload_vendor_cache_filters_and_price(tmp_path, monkeypatch):
    answer = "complete answer 世界 " * 400
    def handler(request):
        payload = chat_response(answer)
        payload["usage"] = {"prompt_tokens": 10, "completion_tokens": 2, "prompt_cache_hit_tokens": 6}
        return httpx.Response(200, json=payload)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        register(client)
        deployment = state(client)["deployments"][0]
        assert mutate(client, f"/deployments/{deployment['id']}", {"price": {
            "input_per_million": 2, "output_per_million": 4, "cached_input_per_million": .2,
        }}, method="PATCH").status_code == 200
        key = create_key(client)
        prompt = "complete prompt 输入 " * 400
        result = invoke(client, key["token"], "req_full", text=prompt)
        assert result.status_code == 200
        item = client.get("/admin/api/logs").json()["items"][0]
        assert item["usage"]["cached_input_tokens"] == 6 and item["source"] == "exact"
        assert item["cost"] == pytest.approx(.0000172) and item["complete"]
        assert item["task"] == "llm" and item["log_type"] == "request"
        assert item["key_name"] == "integration caller" and item["credential_id"] == "primary"
        detail = client.get(f"/admin/api/logs/{item['request_id']}/1")
        assert "no-store" in detail.headers["cache-control"].split(", ")
        body = detail.json()
        assert body["detail"]["request"]["input"][0]["content"][0]["text"] == prompt
        exchange = body["detail"]["upstream_exchanges"][0]
        assert exchange["request"]["messages"][0]["content"][0]["text"] == prompt
        assert json.loads(exchange["response"])["choices"][0]["message"]["content"] == answer
        assert body["detail"]["response"]["output"][0]["content"][0]["text"] == answer
        assert SECRET not in detail.text and "Authorization" not in detail.text
        at = datetime.fromisoformat(item["started_at"])
        params = {"start": (at - timedelta(seconds=1)).astimezone(timezone(timedelta(hours=8))).isoformat(),
                  "end": (at + timedelta(seconds=1)).isoformat(), "task": "llm",
                  "model": "stable", "status": "completed", "log_type": "request"}
        assert len(client.get("/admin/api/logs", params=params).json()["items"]) == 1
        for field, value in (("task", "embedding"), ("log_type", "model-test"), ("status", "failed")):
            assert client.get("/admin/api/logs", params={**params, field: value}).json()["items"] == []
        assert client.get("/admin/api/logs", params={"start": params["end"], "end": params["start"]}).status_code == 422
        assert client.get("/admin/api/logs", params={"start": "2026-11-08T12:00:00"}).status_code == 422
        assert client.get("/admin/api/logs/missing/1").status_code == 404


@pytest.mark.parametrize("mode", ["full", "off"])
def test_stream_trace_first_token_complete_sse_and_estimated_usage(tmp_path, monkeypatch, mode):
    frames = [
        {"id": "stream", "choices": [{"index": 0, "delta": {"content": "Hello "}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "world"}, "finish_reason": "stop"}]},
    ]
    raw = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
    with instance(tmp_path, monkeypatch, lambda _: httpx.Response(200, text=raw),
                  Settings(log_detail_mode=mode)) as (client, _):
        add_upstream(client)
        register(client)
        token = create_key(client)["token"]
        response = invoke(client, token, "req_stream_estimate", stream=True)
        assert "response.completed" in response.text
        item = client.get("/admin/api/logs").json()["items"][0]
        assert 0 <= item["first_token_ms"] <= item["latency_ms"]
        assert item["source"] == "estimate" and item["tokenizer"] == "deepseek"
        assert item["usage"]["input_tokens"] > 0 and item["usage"]["output_tokens"] > 0
        assert not item["usage"]["measurement"]["exact"] and item["usage"]["cached_input_tokens"] is None
        body = client.get(f"/admin/api/logs/{item['request_id']}/1").json()["detail"]
        if mode == "off":
            assert body is None
        else:
            assert body["upstream_exchanges"][0]["response"] == raw
            assert body["upstream_exchanges"][0]["complete"]
            assert body["response"]["output"][0]["content"][0]["text"] == "Hello world"


def test_concurrent_attempt_traces_do_not_mix_requests(tmp_path, monkeypatch):
    async def handler(request):
        import asyncio
        prompt = json.loads(request.content)["messages"][0]["content"][0]["text"]
        await asyncio.sleep(.02)
        return httpx.Response(200, json=chat_response(prompt))
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        register(client)
        token = create_key(client)["token"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda index: invoke(client, token, f"req_parallel_{index}", text=f"prompt-{index}"), range(2)))
        assert all(result.json()["status"] == "completed" for result in results)
        for item in client.get("/admin/api/logs").json()["items"]:
            body = client.get(f"/admin/api/logs/{item['request_id']}/1").json()["detail"]
            prompt = body["request"]["input"][0]["content"][0]["text"]
            assert json.loads(body["upstream_exchanges"][0]["response"])["choices"][0]["message"]["content"] == prompt


def test_stream_trace_can_be_closed_by_another_async_context():
    async def scenario():
        trace = AttemptTrace({})
        closed = False
        async def source():
            nonlocal closed
            try:
                assert current_trace.get() is trace
                yield "delta"
                assert current_trace.get() is trace
                yield "terminal"
            finally:
                closed = True
        events = trace.events(source())
        assert await anext(events) == "delta"
        assert current_trace.get() is None
        assert await anext(events) == "terminal"
        assert current_trace.get() is None
        await asyncio.create_task(events.aclose())
        assert closed and current_trace.get() is None
    asyncio.run(scenario())

@pytest.mark.parametrize("status", [200, 401])
def test_body_preserved_in_admin_trace_with_credentials_redacted(tmp_path, monkeypatch, status):
    error = SECRET + " upstream diagnostic " * 500
    def handler(_):
        return httpx.Response(status, json=chat_response(error)) if status == 200 else httpx.Response(status, text=error)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        register(client)
        assert invoke(client, create_key(client)["token"], "req_failure").json()["status"] == ("completed" if status == 200 else "failed")
        item = client.get("/admin/api/logs").json()["items"][0]
        assert item["source"] == ("exact" if status == 200 else "unknown") and item["cost"] is None
        detail = client.get(f"/admin/api/logs/{item['request_id']}/1").json()
        body = detail["detail"]
        saved = body["upstream_exchanges"][0]["response"]
        if status == 200:
            saved = json.loads(saved)["choices"][0]["message"]["content"]
        assert len(saved) > 4096 and saved.endswith(" upstream diagnostic ")
        assert SECRET not in json.dumps(detail) and "[REDACTED]" in saved


def test_body_retention_keeps_historical_summary(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        add_upstream(client)
        register(client)
        with app.state.registry.store.transaction() as db:
            db.execute("""INSERT INTO attempts(request_id,attempt_no,response_id,deployment_id,upstream_id,
                model,started_at,status,price) VALUES('old',1,'old','removed','removed','old',
                '2000-01-01T00:00:00+00:00','completed',?)""", (dump({}),))
            db.execute("INSERT INTO attempt_details VALUES('old',1,?)", (dump({"request": "old body"}),))
        assert invoke(client, create_key(client)["token"], "req_prune").json()["status"] == "completed"
        assert len(client.get("/admin/api/logs").json()["items"]) == 2
        assert client.get("/admin/api/logs/old/1").json()["detail"] is None


@pytest.mark.parametrize("encoding", ["yaml", "json", "compressed"])
def test_detail_storage_reads_legacy_and_compressed_bodies_after_reopen(tmp_path, encoding):
    detail = {"request": {"text": "中文日志\n" * 200}, "response": {"text": "完整响应"}}
    body = dump(detail) if encoding == "yaml" else json.dumps(detail, ensure_ascii=False)
    if encoding == "compressed":
        body = zlib.compress(body.encode("utf-8"))
    store = Store(tmp_path)
    try:
        with store.transaction() as db:
            db.execute("""INSERT INTO attempts(request_id,attempt_no,response_id,deployment_id,upstream_id,
                model,started_at,status,price) VALUES('saved',1,'saved','upstream','upstream','model',
                '2026-11-10T00:00:00+00:00','completed',?)""", (dump({}),))
            db.execute("INSERT INTO attempt_details VALUES('saved',1,?)", (body,))
    finally:
        store.close()
    reopened = Store(tmp_path)
    try:
        assert reopened.attempt_detail("saved", 1) == detail
    finally:
        reopened.close()


@pytest.mark.parametrize("mode,status,recorded", [
    ("full", 200, True), ("full", 401, True),
    ("errors", 200, False), ("errors", 401, True),
    ("off", 200, False), ("off", 401, False),
])
def test_detail_modes_preserve_summaries_and_metering(tmp_path, monkeypatch, mode, status, recorded):
    traces = []

    def handler(_):
        traces.extend(app.state.registry.router.metering._active.values())
        return httpx.Response(status, json=chat_response("mode response"))

    with instance(tmp_path, monkeypatch, handler, Settings(log_detail_mode=mode)) as (client, app):
        add_upstream(client)
        register(client)
        result = invoke(client, create_key(client)["token"], "req_log_mode")
        assert result.json()["status"] == ("completed" if status == 200 else "failed")
        item = client.get("/admin/api/logs").json()["items"][0]
        assert item["status"] == result.json()["status"]
        assert item["usage"]["total_tokens"] == (5 if status == 200 else None)
        body = client.get(f"/admin/api/logs/{item['request_id']}/1").json()
        assert (body["detail"] is not None) is recorded
        if not recorded:
            assert body["gateway_response"] is None
        with app.state.registry.store._lock:
            assert app.state.registry.store._db.execute(
                "SELECT COUNT(*) FROM attempt_details",
            ).fetchone()[0] == int(recorded)
            assert app.state.registry.store._db.execute("SELECT COUNT(*) FROM cost_records").fetchone()[0] == 1
            assert app.state.registry.store._db.execute("SELECT finished FROM admission").fetchone()[0] == 1
        assert not app.state.registry.router.metering._active
        if mode == "off":
            assert traces and all(not trace.exchanges and not trace.stream_bodies for trace in traces)


def test_detail_mode_validates_startup_values(monkeypatch):
    monkeypatch.setenv("LOG_DETAIL_MODE", "errors")
    assert Settings.from_env().log_detail_mode == "errors"
    monkeypatch.setenv("LOG_DETAIL_MODE", "invalid")
    with pytest.raises(ValueError, match="LOG_DETAIL_MODE"):
        Settings.from_env()

@pytest.mark.parametrize(("model", "expected"), [
    ("gpt-4o", "o200k_base"), ("text-embedding-3-small", "cl100k_base"),
    ("Qwen/Qwen3-8B", "qwen"), ("unknown-model", "deepseek"),
])
def test_bundled_vendor_tokenizers_and_deepseek_default(model, expected):
    name = select_tokenizer("auto", model)
    assert name == expected
    assert count_text("hello", name) == 1
    assert count_text("Hello 世界，这是一个测试。", name) == {
        "o200k_base": 6, "cl100k_base": 11, "qwen": 7, "deepseek": 7,
    }[name]


def test_estimates_preserve_provider_evidence_and_unknown_media():
    usage = Usage.model_validate({
        "input_tokens": 123, "cached_input_tokens": 17,
        "measurement": {"mode": "provider", "exact": False, "exact_fields": ["input_tokens", "cached_input_tokens"]},
    })
    result, name = estimate_usage(usage, {"input": []}, {"output": [{"text": "hello"}]},
                                  model="gpt-4o", setting="auto", task="llm")
    assert name == "o200k_base" and result.input_tokens == 123 and result.cached_input_tokens == 17
    assert result.output_tokens == 1 and result.total_tokens == 124
    assert result.measurement.mode == "mixed" and not result.measurement.exact
    assert "cached_input_tokens" not in result.measurement.estimated_fields
    media, _ = estimate_usage(Usage(), {"input": [{"type": "image"}]}, {"output": [{"text": "hello"}]},
                              model="unknown", setting="auto", task="llm")
    assert media.input_tokens is None and media.total_tokens is None and media.output_tokens == 1
