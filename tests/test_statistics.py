from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from api.server import create_app
from core.config import Settings
from core.executor import GatewayExecutor
from core.models import EmbeddingRequest, RerankRequest, Usage, UsageMeasurement
from core.registry import ProviderRegistry
from core.retrieval_executor import RetrievalExecutor
from core.stores import InMemoryExecutionStore
from storage.statistics import StatisticsStore
from tests.test_live_config import project
from tests.test_provider_boundary import FakeProvider, request
from tests.test_retrieval_api import (
    FakeRetrievalProvider,
    embedding_body,
    rerank_body,
)


def test_daily_store_rollups_nullable_usage_rankings_and_replays(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = StatisticsStore(tmp_path / "storage")
        await store.initialize()
        completed = await store.begin_invocation(
            task="llm",
            provider_id="deepseek",
            model="deepseek-deepseek-v4-pro",
            tenant_id="tenant-1",
            gateway_key_id="graph-production",
            request_id="request-1",
            response_id="response-1",
        )
        assert completed is not None
        await store.finish_invocation(
            completed,
            status="completed",
            usage=Usage(
                input_tokens=100,
                cached_input_tokens=25,
                output_tokens=20,
                total_tokens=120,
                measurement=UsageMeasurement(
                    mode="provider",
                    exact=True,
                    exact_fields=[
                        "input_tokens",
                        "cached_input_tokens",
                        "output_tokens",
                        "total_tokens",
                    ],
                ),
            ),
        )
        # Finishing the same invocation twice must not duplicate terminal totals.
        await store.finish_invocation(completed, status="failed", error_code="LATE")

        incomplete = await store.begin_invocation(
            task="embedding",
            provider_id="deepseek",
            model="deepseek-embedding-v1",
            tenant_id="tenant-1",
            gateway_key_id="graph-production",
            request_id="request-2",
        )
        assert incomplete is not None
        await store.finish_invocation(
            incomplete,
            status="incomplete",
            error_code="PROVIDER_BAD_RESPONSE",
            error_type="provider_error",
            error_message="上游响应格式无效（已脱敏）",
        )
        await store.record_replay(
            task="llm",
            provider_id="deepseek",
            model="deepseek-deepseek-v4-pro",
            gateway_key_id="graph-production",
        )

        daily = await store.daily(completed.day)
        assert daily["calls"] == 2
        assert daily["successes"] == 1
        assert daily["failures"] == 1
        assert daily["incompletes"] == 1
        assert daily["replay_count"] == 1
        assert daily["tokens"]["input_tokens"] == 100
        assert daily["tokens"]["total_tokens"] == 120
        assert daily["cache_hit_rate"] == 0.25
        assert daily["cache_eligible_samples"] == 1

        providers = await store.rankings(completed.day, "provider")
        assert providers["items"][0]["id"] == "deepseek"
        assert providers["items"][0]["calls"] == 2
        keys = await store.rankings(completed.day, "gateway_key")
        assert keys["items"][0]["id"] == "graph-production"
        hourly = await store.hourly(completed.day)
        assert len(hourly["items"]) == 24
        assert sum(item["calls"] for item in hourly["items"]) == 2
        assert sum(item["successes"] for item in hourly["items"]) == 1
        assert sum(item["total_tokens"] or 0 for item in hourly["items"]) == 120
        active_hour = next(item for item in hourly["items"] if item["calls"])
        assert active_hour["input_tokens"] == 100
        assert active_hour["cached_input_tokens"] == 25
        assert active_hour["output_tokens"] == 20
        assert active_hour["failures"] == 1
        assert active_hour["cache_hit_rate"] == 0.25
        assert active_hour["success_rate"] == 0.5
        empty_hour = next(item for item in hourly["items"] if not item["calls"])
        assert empty_hour["total_tokens"] == 0
        assert empty_hour["input_tokens"] == 0
        assert empty_hour["output_tokens"] == 0
        assert empty_hour["cached_input_tokens"] == 0
        key_usage = await store.gateway_key_usage()
        assert key_usage["graph-production"]["calls"] == 2
        assert key_usage["graph-production"]["successes"] == 1
        assert key_usage["graph-production"]["total_tokens"] == 120
        assert key_usage["graph-production"]["token_samples"] == 1
        assert str(key_usage["graph-production"]["last_used_at"]).startswith("20")

        recent = await store.recent_invocations("all", limit=10)
        successful = await store.recent_invocations("success", limit=10)
        failed = await store.recent_invocations("failure", limit=10)
        selected_day = await store.recent_invocations("all", limit=10, day=completed.day)
        assert len(recent["items"]) == 2
        assert selected_day["date"] == completed.day
        assert len(selected_day["items"]) == 2
        assert successful["items"][0]["status"] == "completed"
        assert failed["items"][0]["status"] == "incomplete"
        assert failed["items"][0]["error_code"] == "PROVIDER_BAD_RESPONSE"
        assert failed["items"][0]["error_type"] == "provider_error"
        assert failed["items"][0]["error_message"] == "上游响应格式无效（已脱敏）"
        assert failed["items"][0]["provider_model"] == "embedding-v1"
        assert failed["items"][0]["gateway_key_id"] == "graph-production"
        assert failed["items"][0]["tokens"]["total_tokens"] is None
        assert "request_id" not in failed["items"][0]
        assert "tenant_id" not in failed["items"][0]

        missing = await store.daily("2000-01-01")
        assert missing["calls"] == 0
        assert missing["tokens"]["input_tokens"] is None
        assert missing["cache_hit_rate"] is None

        with sqlite3.connect(completed.database) as connection:
            unknown = connection.execute(
                "SELECT input_tokens, cached_input_tokens FROM invocations WHERE invocation_id=?",
                (incomplete.invocation_id,),
            ).fetchone()
        assert unknown == (None, None)

    asyncio.run(scenario())


def test_invocation_history_is_paginated_beyond_one_hundred_and_filters_hour(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = StatisticsStore(tmp_path / "storage")
        day = "2026-07-28"
        database = store._path_for_day(day)
        store._initialize_database(database)

        rows: list[tuple[str, ...]] = []
        for index in range(125):
            hour = 9 if index < 75 else 10
            local_started = datetime(
                2026,
                7,
                28,
                hour,
                index % 60,
                tzinfo=store.timezone,
            )
            rows.append(
                (
                    f"inv-page-{index:03d}",
                    day,
                    "llm",
                    "example",
                    "example-model",
                    "tenant",
                    f"request-page-{index:03d}",
                    local_started.astimezone(timezone.utc).isoformat(),
                    "completed",
                )
            )
        with sqlite3.connect(database) as connection:
            connection.executemany(
                """INSERT INTO invocations(
                    invocation_id, day, task, provider_id, model, tenant_id,
                    request_id, started_at, status
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                rows,
            )

        first = await store.recent_invocations("all", day=day, limit=20)
        sixth = await store.recent_invocations(
            "all", day=day, limit=20, offset=100
        )
        seventh = await store.recent_invocations(
            "all", day=day, limit=20, offset=120
        )
        nine_oclock = await store.recent_invocations(
            "all", day=day, hour=9, limit=20
        )

        assert first["total"] == 125
        assert len(first["items"]) == 20
        assert sixth["total"] == 125
        assert len(sixth["items"]) == 20
        assert len(seventh["items"]) == 5
        assert nine_oclock["hour"] == 9
        assert nine_oclock["total"] == 75

    asyncio.run(scenario())


def test_gateway_key_usage_counts_tool_action_as_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = StatisticsStore(tmp_path / "storage")
        handle = await store.begin_invocation(
            task="llm",
            provider_id="example",
            model="example-tool-model",
            tenant_id="tenant",
            gateway_key_id="agent-key",
            request_id="request-tool-call",
        )
        assert handle is not None
        await store.finish_invocation(handle, status="requires_action")

        usage = await store.gateway_key_usage()

        assert usage["agent-key"]["calls"] == 1
        assert usage["agent-key"]["successes"] == 1

    asyncio.run(scenario())


def test_llm_and_retrieval_executors_count_actual_calls_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = StatisticsStore(tmp_path / "storage")
        await store.initialize()

        llm_registry = ProviderRegistry()
        llm_registry.register(FakeProvider())
        llm = GatewayExecutor(
            llm_registry, InMemoryExecutionStore(), statistics=store
        )
        context = llm.make_context(
            tenant_id="tenant",
            subject_id="agent",
            request_id="req_1",
            gateway_key_id="llm-key",
        )
        await llm.execute(request(stream=False), context)
        replay_context = llm.make_context(
            tenant_id="tenant",
            subject_id="agent",
            request_id="req_1",
            gateway_key_id="llm-key",
        )
        await llm.execute(request(stream=False), replay_context)

        retrieval_registry = ProviderRegistry()
        provider = FakeRetrievalProvider()
        retrieval_registry.register(provider)
        retrieval = RetrievalExecutor(retrieval_registry, statistics=store)
        embed_request = EmbeddingRequest.model_validate(embedding_body())
        embed_context = retrieval.make_context(
            tenant_id="tenant",
            subject_id="graph",
            request_id=embed_request.request_id,
            gateway_key_id="graph-key",
        )
        await retrieval.embeddings(embed_request, embed_context)
        await retrieval.embeddings(embed_request, embed_context)

        rerank_request = RerankRequest.model_validate(rerank_body())
        rerank_context = retrieval.make_context(
            tenant_id="tenant",
            subject_id="graph",
            request_id=rerank_request.request_id,
            gateway_key_id="graph-key",
        )
        await retrieval.rerank(rerank_request, rerank_context)

        day = next((tmp_path / "storage" / "daily").rglob("*.sqlite3")).stem
        daily = await store.daily(day)
        assert daily["calls"] == 3
        assert daily["successes"] == 3
        assert daily["replay_count"] == 2
        assert provider.embedding_calls == 1
        assert provider.rerank_calls == 1

    asyncio.run(scenario())


def test_admin_statistics_api_is_protected_and_validates_queries(tmp_path: Path) -> None:
    root = project(tmp_path / "project")
    app = create_app(
        Settings(),
        live_config_root=root,
        statistics_root=tmp_path / "statistics",
        discover_providers=False,
    )
    with TestClient(app) as client:
        response = client.get("/admin/api/statistics/daily", params={"date": "2000-01-01"})
        assert response.status_code == 200
        assert response.json()["calls"] == 0
        assert response.json()["cache_hit_rate"] is None

        invalid_date = client.get(
            "/admin/api/statistics/daily", params={"date": "not-a-date"}
        )
        assert invalid_date.status_code == 422
        invalid_hourly_date = client.get(
            "/admin/api/statistics/hourly", params={"date": "not-a-date"}
        )
        assert invalid_hourly_date.status_code == 422
        invalid_dimension = client.get(
            "/admin/api/statistics/rankings",
            params={"date": "2000-01-01", "dimension": "tenant"},
        )
        assert invalid_dimension.status_code == 422
        invalid_range = client.get(
            "/admin/api/statistics/series",
            params={"from": "2026-07-26", "to": "2026-07-20"},
        )
        assert invalid_range.status_code == 422
        invocation_logs = client.get(
            "/admin/api/statistics/invocations",
            params={"outcome": "failure", "limit": 10, "date": "2000-01-01"},
        )
        assert invocation_logs.status_code == 200
        assert invocation_logs.json()["outcome"] == "failure"
        assert invocation_logs.json()["date"] == "2000-01-01"
        assert invocation_logs.json()["page"] == 1
        assert invocation_logs.json()["page_size"] == 10
        assert invocation_logs.json()["total"] == 0
        assert invocation_logs.json()["pages"] == 1
        selected_hour = client.get(
            "/admin/api/statistics/invocations",
            params={
                "date": "2000-01-01",
                "hour": 23,
                "page": 2,
                "page_size": 20,
            },
        )
        assert selected_hour.status_code == 200
        assert selected_hour.json()["hour"] == 23
        assert selected_hour.json()["page"] == 2
        invalid_invocation_date = client.get(
            "/admin/api/statistics/invocations",
            params={"date": "not-a-date"},
        )
        assert invalid_invocation_date.status_code == 422
        invalid_invocation_hour = client.get(
            "/admin/api/statistics/invocations",
            params={"date": "2000-01-01", "hour": 24},
        )
        assert invalid_invocation_hour.status_code == 422
