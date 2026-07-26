from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.server import create_app
from core.config import PrincipalConfig, Settings
from core.models import (
    EmbeddingCapabilities,
    EmbeddingRequest,
    ModelCapabilities,
    RerankCapabilities,
    RerankRequest,
    Usage,
    UsageMeasurement,
)
from core.provider_contract import (
    ProviderEmbedding,
    ProviderEmbeddingResult,
    ProviderPackage,
    ProviderRerankItem,
    ProviderRerankResult,
    RequestContext,
)
from tests.test_live_config import project, write_json


EMBED_HEADERS = {
    "Authorization": "Bearer embedding-token",
    "X-Kemo-Protocol-Version": "1.0",
    "Idempotency-Key": "embed_req_1",
}
RERANK_HEADERS = {
    "Authorization": "Bearer rerank-token",
    "X-Kemo-Protocol-Version": "1.0",
    "Idempotency-Key": "rerank_req_1",
}


class FakeRetrievalProvider(ProviderPackage):
    provider_id = "retrieval"

    def __init__(self) -> None:
        self.embedding_calls = 0
        self.rerank_calls = 0

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"retrieval-embed-v1", "retrieval-rerank-v1"})

    async def capabilities(self, model: str) -> ModelCapabilities:
        if model == "retrieval-embed-v1":
            return ModelCapabilities(
                model=model,
                task="embedding",
                input_modalities=["text"],
                output_modalities=["embedding"],
                streaming=False,
                embedding=EmbeddingCapabilities(
                    input_types=["query", "document"],
                    default_dimensions=3,
                    supported_dimensions=[3],
                    max_batch_size=16,
                    max_input_tokens_per_item=8192,
                    normalization="always",
                ),
            )
        if model == "retrieval-rerank-v1":
            return ModelCapabilities(
                model=model,
                task="rerank",
                input_modalities=["text"],
                output_modalities=["score"],
                streaming=False,
                rerank=RerankCapabilities(
                    max_documents=100,
                    max_query_tokens=512,
                    max_document_tokens=4096,
                ),
            )
        raise LookupError(model)

    async def embed(
        self, request: EmbeddingRequest, context: RequestContext
    ) -> ProviderEmbeddingResult:
        del context
        self.embedding_calls += 1
        # 故意逆序，验证网关按 index 恢复 kemo-graph 的输入顺序。
        embeddings = [
            ProviderEmbedding(index=index, vector=[float(index), 0.5, 1.0])
            for index in reversed(range(len(request.inputs)))
        ]
        return ProviderEmbeddingResult(
            embeddings=embeddings,
            vector_space_id="retrieval-embed-v1@2026-07:3:normalized",
            model_version="2026-07",
            provider_response_id="vendor_embed_1",
            usage=Usage(
                input_tokens=7,
                total_tokens=7,
                measurement=UsageMeasurement(
                    mode="provider",
                    exact=True,
                    exact_fields=["input_tokens", "total_tokens"],
                ),
            ),
        )

    async def rerank(
        self, request: RerankRequest, context: RequestContext
    ) -> ProviderRerankResult:
        del request, context
        self.rerank_calls += 1
        # 故意不排序，统一执行器必须按 higher_is_more_relevant 排序。
        return ProviderRerankResult(
            results=[
                ProviderRerankItem(index=0, relevance_score=0.2),
                ProviderRerankItem(index=2, relevance_score=0.9),
                ProviderRerankItem(index=1, relevance_score=0.5),
            ],
            model_version="2026-07",
            provider_response_id="vendor_rerank_1",
            usage=Usage(input_tokens=11, total_tokens=11),
        )


class BrokenEmbeddingProvider(FakeRetrievalProvider):
    provider_id = "broken_retrieval"

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"broken_retrieval-embed-v1"})

    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            model=model,
            task="embedding",
            input_modalities=["text"],
            output_modalities=["embedding"],
            streaming=False,
            embedding=EmbeddingCapabilities(
                input_types=["document"],
                default_dimensions=3,
                max_batch_size=4,
                normalization="always",
            ),
        )

    async def embed(
        self, request: EmbeddingRequest, context: RequestContext
    ) -> ProviderEmbeddingResult:
        del request, context
        return ProviderEmbeddingResult(
            embeddings=[ProviderEmbedding(index=0, vector=[1.0, float("nan"), 3.0])],
            vector_space_id="broken",
        )


def retrieval_project(tmp_path: Path) -> Path:
    root = project(tmp_path)
    write_json(root / "api" / "keys.json", {"keys": {}})
    return root


def retrieval_app(tmp_path: Path):
    settings = Settings(
        api_keys={
            "embedding-token": PrincipalConfig(
                "graph", "embedder", frozenset({"embedding:invoke"})
            ),
            "rerank-token": PrincipalConfig(
                "graph", "reranker", frozenset({"rerank:invoke"})
            ),
            "model-token": PrincipalConfig(
                "graph", "graph-worker", frozenset({"model:invoke"})
            ),
        }
    )
    app = create_app(
        settings,
        live_config_root=retrieval_project(tmp_path),
        discover_providers=False,
    )
    provider = FakeRetrievalProvider()
    app.state.registry.register(provider)
    return app, provider


def embedding_body() -> dict:
    return {
        "protocol_version": "1.0",
        "request_id": "embed_req_1",
        "model": "retrieval-embed-v1",
        "input_type": "document",
        "inputs": [
            {"id": "node-a", "text": "alpha"},
            {"id": "node-b", "text": "beta"},
        ],
        "normalize": True,
    }


def rerank_body() -> dict:
    return {
        "protocol_version": "1.0",
        "request_id": "rerank_req_1",
        "model": "retrieval-rerank-v1",
        "query": "graph retrieval",
        "documents": [
            {"id": "doc-a", "text": "a", "metadata": {"node": "a"}},
            {"id": "doc-b", "text": "b", "metadata": {"node": "b"}},
            {"id": "doc-c", "text": "c", "metadata": {"node": "c"}},
        ],
        "top_n": 2,
        "return_documents": True,
    }


def test_embeddings_preserve_ids_order_usage_and_idempotency(tmp_path: Path) -> None:
    app, provider = retrieval_app(tmp_path)
    with TestClient(app) as client:
        first = client.post("/model/embeddings", headers=EMBED_HEADERS, json=embedding_body())
        replay = client.post("/model/embeddings", headers=EMBED_HEADERS, json=embedding_body())

    assert first.status_code == 200
    payload = first.json()
    assert payload["object"] == "kemo.embedding_list"
    assert payload["vector_space_id"] == "retrieval-embed-v1@2026-07:3:normalized"
    assert payload["dimensions"] == 3
    assert [item["id"] for item in payload["data"]] == ["node-a", "node-b"]
    assert [item["index"] for item in payload["data"]] == [0, 1]
    assert payload["usage"]["input_tokens"] == 7
    assert replay.json() == payload
    assert provider.embedding_calls == 1
    assert app.state.runtime_state.snapshot()["active_executions"] == 0


def test_embedding_idempotency_conflict_and_scope(tmp_path: Path) -> None:
    app, _ = retrieval_app(tmp_path)
    with TestClient(app) as client:
        assert client.post(
            "/model/embeddings", headers=RERANK_HEADERS, json=embedding_body()
        ).status_code == 403
        assert client.post(
            "/model/embeddings", headers=EMBED_HEADERS, json=embedding_body()
        ).status_code == 200
        changed = embedding_body()
        changed["inputs"][0]["text"] = "changed"
        conflict = client.post(
            "/model/embeddings", headers=EMBED_HEADERS, json=changed
        )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_rerank_returns_stable_ids_sorted_scores_and_optional_documents(
    tmp_path: Path,
) -> None:
    app, provider = retrieval_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/model/rerank", headers=RERANK_HEADERS, json=rerank_body())

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "kemo.rerank"
    assert payload["score_semantics"] == "higher_is_more_relevant"
    assert [item["document_id"] for item in payload["results"]] == ["doc-c", "doc-b"]
    assert [item["rank"] for item in payload["results"]] == [1, 2]
    assert payload["results"][0]["document"]["metadata"] == {"node": "c"}
    assert provider.rerank_calls == 1


def test_capabilities_expose_retrieval_task_contracts(tmp_path: Path) -> None:
    app, _ = retrieval_app(tmp_path)
    headers = {"Authorization": "Bearer model-token"}
    with TestClient(app) as client:
        embedding = client.get(
            "/model/capabilities",
            headers=headers,
            params={"model": "retrieval-embed-v1"},
        )
        rerank = client.get(
            "/model/capabilities",
            headers=headers,
            params={"model": "retrieval-rerank-v1"},
        )

    assert embedding.json()["task"] == "embedding"
    assert embedding.json()["embedding"]["default_dimensions"] == 3
    assert rerank.json()["task"] == "rerank"
    assert rerank.json()["rerank"]["max_documents"] == 100


def test_task_mismatch_and_bad_provider_result_are_sanitized(tmp_path: Path) -> None:
    app, _ = retrieval_app(tmp_path)
    broken = BrokenEmbeddingProvider()
    app.state.registry.register(broken)
    mismatch = embedding_body()
    mismatch["model"] = "retrieval-rerank-v1"
    broken_body = embedding_body()
    broken_body["request_id"] = "broken_req_1"
    broken_body["model"] = "broken_retrieval-embed-v1"
    broken_body["inputs"] = [{"id": "node", "text": "secret source text"}]
    broken_headers = {**EMBED_HEADERS, "Idempotency-Key": "broken_req_1"}

    with TestClient(app) as client:
        wrong_task = client.post(
            "/model/embeddings", headers=EMBED_HEADERS, json=mismatch
        )
        bad_result = client.post(
            "/model/embeddings", headers=broken_headers, json=broken_body
        )

    assert wrong_task.status_code == 400
    assert wrong_task.json()["error"]["code"] == "MODEL_TASK_MISMATCH"
    assert bad_result.status_code == 502
    assert bad_result.json()["error"]["code"] == "PROVIDER_BAD_RESPONSE"
    assert "secret source text" not in bad_result.text


def test_strict_request_validation_rejects_unstable_graph_ids(tmp_path: Path) -> None:
    app, _ = retrieval_app(tmp_path)
    duplicate_embeddings = embedding_body()
    duplicate_embeddings["inputs"][1]["id"] = "node-a"
    invalid_rerank = rerank_body()
    invalid_rerank["top_n"] = 4

    with TestClient(app) as client:
        embedding_response = client.post(
            "/model/embeddings", headers=EMBED_HEADERS, json=duplicate_embeddings
        )
        rerank_response = client.post(
            "/model/rerank", headers=RERANK_HEADERS, json=invalid_rerank
        )

    assert embedding_response.status_code == 422
    assert rerank_response.status_code == 422


def test_unknown_retrieval_model_has_task_specific_error(tmp_path: Path) -> None:
    app, _ = retrieval_app(tmp_path)
    body = embedding_body()
    body["model"] = "missing-embed-model"

    with TestClient(app) as client:
        response = client.post("/model/embeddings", headers=EMBED_HEADERS, json=body)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MODEL_NOT_FOUND"
