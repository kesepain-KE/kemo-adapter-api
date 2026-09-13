"""共享的无网络检索 Provider 与请求数据。"""
from __future__ import annotations
from core.models import EmbeddingCapabilities, EmbeddingRequest, ModelCapabilities, RerankCapabilities, RerankRequest, Usage, UsageMeasurement
from core.provider_contract import ProviderEmbedding, ProviderEmbeddingResult, ProviderPackage, ProviderRerankItem, ProviderRerankResult, RequestContext

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
