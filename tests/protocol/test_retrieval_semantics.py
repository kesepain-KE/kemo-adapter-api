from __future__ import annotations

import asyncio

import pytest

from core.models import EmbeddingRequest, ModelCapabilities, RerankRequest
from core.provider_contract import (
    ProviderEmbedding,
    ProviderEmbeddingResult,
    ProviderRerankItem,
    ProviderRerankResult,
)
from core.retrieval_executor import RetrievalExecutor
from provider.protocol.serialization import request_fingerprint


def _request(**updates):
    payload = {
        "protocol_version": "2.0",
        "request_id": "req_rerank_semantics",
        "model": "rerank-model",
        "query": "q",
        "documents": [
            {"id": "a", "text": "a"},
            {"id": "b", "text": "b"},
            {"id": "c", "text": "c"},
        ],
    }
    payload.update(updates)
    return RerankRequest.model_validate(payload)


def test_threshold_filters_before_top_n_and_ties_keep_document_order() -> None:
    executor = RetrievalExecutor(None)  # response builder is side-effect free
    response = executor._build_rerank_response(
        _request(top_n=2, score_threshold=0.5, return_documents=False),
        ProviderRerankResult(
            results=[
                ProviderRerankItem(index=2, relevance_score=0.8),
                ProviderRerankItem(index=0, relevance_score=0.8),
                ProviderRerankItem(index=1, relevance_score=0.2),
            ]
        ),
    )
    assert [item.document_id for item in response.results] == ["a", "c"]
    assert response.filtered_count == 1


def test_threshold_equal_value_is_retained() -> None:
    executor = RetrievalExecutor(None)
    response = executor._build_rerank_response(
        _request(score_threshold=0.5),
        ProviderRerankResult(
            results=[
                ProviderRerankItem(index=0, relevance_score=0.5),
                ProviderRerankItem(index=1, relevance_score=0.4),
            ]
        ),
    )
    assert [item.document_id for item in response.results] == ["a"]
    assert response.filtered_count == 1


def test_retrieval_request_hash_uses_protocol_fingerprint() -> None:
    request = _request(request_id="req_hash_contract")
    executor = RetrievalExecutor(None)
    assert executor._records == {}
    # The helper is intentionally module-local; compare it with the Agent
    # artifact directly so both retrieval operations share the LLM hash rules.
    from core.retrieval_executor import _request_hash
    assert _request_hash(request) == request_fingerprint(request)


def test_embedding_truncate_requires_provider_report() -> None:
    class Package:
        provider_id = "embed-provider"

        async def capabilities(self, model):
            return ModelCapabilities(
                protocol_version="2.0", model=model, provider_id=self.provider_id,
                provider_model=model, task="embedding", input_modalities=["text"],
                output_modalities=["embedding"],
                embedding={"input_types": ["query"], "default_dimensions": 2,
                           "max_batch_size": 2, "supports_truncate": True},
            )

        async def embed(self, request, context):
            return ProviderEmbeddingResult(
                embeddings=[ProviderEmbedding(index=0, vector=[0.1, 0.2])],
                vector_space_id="space",
                truncation_reported=False,
            )

    request = EmbeddingRequest(
        protocol_version="2.0", request_id="req_truncate_contract", model="embed",
        input_type="query", inputs=[{"id": "q", "text": "long"}], truncate="end",
    )
    with pytest.raises(Exception, match="截断回执"):
        asyncio.run(
            RetrievalExecutor(None)._embed_once_with_package(
                request,
                RetrievalExecutor(None).make_context(
                    tenant_id="tenant", subject_id="subject", request_id=request.request_id
                ),
                Package(),
            )
        )
