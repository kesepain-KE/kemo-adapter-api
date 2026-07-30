from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from api.routes.responses import _heartbeat_stream
from core.config import PrincipalConfig, Settings
from core.models import SSEEvent
from core.runtime_state import (
    GatewayOverloadedError,
    GatewayRuntimeState,
)
from tests.test_provider_boundary import FakeProvider, request as provider_request


def test_sse_heartbeat_keeps_idle_stream_alive_without_protocol_event() -> None:
    async def scenario() -> None:
        async def delayed_events():
            await asyncio.sleep(0.04)
            yield SSEEvent(
                type="response.created",
                event_id="evt_1",
                sequence=0,
                request_id="req_1",
                response_id="resp_1",
                timestamp="2026-07-30T00:00:00Z",
            )

        chunks = [
            chunk
            async for chunk in _heartbeat_stream(
                delayed_events(), heartbeat_seconds=0.01
            )
        ]
        assert chunks[0] == b": kemo-heartbeat\n\n"
        assert any(chunk.startswith(b"id: evt_1\n") for chunk in chunks)
        assert all(b'"type":"kemo-heartbeat"' not in chunk for chunk in chunks)

    asyncio.run(scenario())


def test_runtime_capacity_rejects_excess_work_and_recovers_after_release() -> None:
    async def scenario() -> None:
        state = GatewayRuntimeState(max_concurrent_executions=1)
        await state.mark_running()
        first = await state.admit_execution()
        with pytest.raises(GatewayOverloadedError):
            await state.admit_execution()
        await first.release()
        second = await state.admit_execution()
        assert state.active_executions == 1
        await second.release()
        assert state.active_executions == 0

    asyncio.run(scenario())


def test_invalid_last_event_id_is_rejected_before_sse_headers(tmp_path: Path) -> None:
    settings = Settings(
        api_keys={
            "gateway-token": PrincipalConfig(
                tenant_id="tenant-1",
                subject_id="subject-1",
                scopes=frozenset({"model:invoke"}),
            )
        }
    )
    app = create_app(
        settings,
        live_config_root=tmp_path,
        statistics_root=tmp_path / "statistics",
        asset_root=tmp_path / "assets",
        discover_providers=False,
    )
    app.state.registry.register(FakeProvider())
    body = provider_request(stream=True).model_dump(mode="json")
    with TestClient(app) as client:
        response = client.post(
            "/model/responses",
            headers={
                "Authorization": "Bearer gateway-token",
                "X-Kemo-Protocol-Version": "1.0",
                "Idempotency-Key": body["request_id"],
                "Last-Event-ID": "evt_missing",
                "Accept": "text/event-stream",
            },
            json=body,
        )
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "STREAM_RESUME_CONFLICT"
