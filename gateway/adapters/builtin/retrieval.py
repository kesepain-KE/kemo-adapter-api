from __future__ import annotations

import hashlib
import math

from core.provider_contract import ProviderEmbedding, ProviderEmbeddingResult, ProviderRerankItem, ProviderRerankResult
from gateway.adapters.builtin.common import DiscoverableAdapter, unsupported
from gateway.adapters.contract import AdapterManifest
from gateway.metering import normalize_usage


class OpenAIEmbedding(DiscoverableAdapter):
    manifest = AdapterManifest(
        id="openai-embedding", name="OpenAI Embeddings", operations=("embedding",),
        usage_semantics="prompt_tokens inclusive; no output generation",
        endpoints={"embedding": "/embeddings", "discovery": "/models"},
        media={"input": ("text",), "output": ("embedding",)},
        embedding_input_types=("query", "document"), embedding_normalization="optional",
        body_fields=("model", "input", "encoding_format", "dimensions"),
    )

    async def embed(self, request, context, model, upstream, headers):
        if request.truncate != "none":
            raise unsupported("此适配器不在本地猜测 tokenizer，不支持截断")
        body = {"model": model, "input": [item.text for item in request.inputs], "encoding_format": "float"}
        if request.dimensions:
            body["dimensions"] = request.dimensions
        raw = await self.transport.json(upstream, "/embeddings", headers, body=body)
        vectors = []
        for item in raw["data"]:
            vector = item["embedding"]
            if request.normalize:
                norm = math.sqrt(sum(v * v for v in vector))
                if norm:
                    vector = [v / norm for v in vector]
            vectors.append(ProviderEmbedding(item["index"], vector))
        usage_raw = {**(raw.get("usage") or {}), "output": 0}
        usage = normalize_usage(usage_raw, input_field="prompt_tokens", output_field="output")
        # Separate deployments have independent vector spaces unless explicitly shared.
        space = hashlib.sha256(f"{upstream.id}:{model}:{request.dimensions}".encode()).hexdigest()[:24]
        return ProviderEmbeddingResult(vectors, f"vs_{space}", usage=usage)


class CohereRerank(DiscoverableAdapter):
    manifest = AdapterManifest(
        id="cohere-rerank", name="Cohere Rerank", operations=("rerank",),
        discovery=False, usage_semantics="billed search units are not tokens; missing token counts remain unknown",
        endpoints={"rerank": "/rerank"},
        media={"input": ("text",), "output": ("score",)},
        body_fields=("model", "query", "documents", "top_n"),
    )

    async def rerank(self, request, context, model, upstream, headers):
        body = {"model": model, "query": request.query, "documents": [d.text for d in request.documents]}
        if request.top_n:
            body["top_n"] = request.top_n
        raw = await self.transport.json(upstream, "/rerank", headers, body=body)
        items = [ProviderRerankItem(i["index"], i["relevance_score"]) for i in raw["results"]]
        if request.score_threshold is not None:
            items = [i for i in items if i.relevance_score >= request.score_threshold]
        return ProviderRerankResult(items, provider_response_id=raw.get("id"))
