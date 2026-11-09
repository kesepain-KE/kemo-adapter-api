"""Native Kemo upstream. Public envelopes terminate at each gateway."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
import hashlib
import json
import logging
import re
from urllib.parse import quote

from pydantic import ValidationError

from core.assets import AssetStoreFailure
from core.models import (
    AssetDescriptor, EmbeddingRequest, EmbeddingResponse, ErrorObject, KemoRequest, KemoResponse,
    KemoResponseBatch, ModelCapabilities, ModelCatalogResponse, ProviderStreamEvent, RerankRequest, RerankResponse,
)
from core.provider_contract import (
    ProviderBatchResult, ProviderChoiceResult, ProviderEmbedding, ProviderEmbeddingResult,
    ProviderEvent, ProviderEventKind as Kind, ProviderException, ProviderRerankItem,
    ProviderRerankResult, ProviderResult,
)
from provider.protocol.streaming import StreamProtocolError, StreamSequenceGuard
from provider.protocol.models import ProviderState
from gateway.adapters.builtin.common import DiscoverableAdapter, system_text, unsupported
from gateway.adapters.contract import AdapterManifest
from gateway.auth import BEARER, RequestHeaders
from gateway.federation import HOPS_HEADER, MAX_HOPS, current_hops
from gateway.transport import upstream_error


logger = logging.getLogger(__name__)
MEDIA = {"image", "audio", "video", "file"}


def bad_response(reason="KEMO_INVALID_RESPONSE"):
    return upstream_error(code=reason, retryable=False)


class AssetTransfer:
    def __init__(self, adapter, context, upstream, headers):
        self.adapter, self.context, self.upstream, self.headers = adapter, context, upstream, headers
        self.inputs, self.outputs = {}, {}

    async def upload(self, value):
        if isinstance(value, list):
            return [await self.upload(item) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("type") in {"message", "tool_result"}:
            return {**value, "content": await self.upload(value["content"])}
        if value.get("type") in MEDIA:
            source = value.get("source") or {}
            identity = value.get("asset_id")
            if not identity and source.get("kind") == "object_store":
                identity = source.get("file_id") or (source.get("uri") or "").removeprefix("asset://")
            if identity:
                if self.context.assets is None:
                    raise unsupported("Kemo 媒体转发需要 Asset 存储")
                if identity not in self.inputs:
                    try:
                        asset = self.context.assets.resolve(identity)
                    except AssetStoreFailure as exc:
                        raise ProviderException(exc.error) from None
                    headers = RequestHeaders(self.headers, secrets=self.headers.secrets)
                    headers["Idempotency-Key"] = "asset_" + hashlib.sha256(
                        (asset.descriptor.checksum_sha256 + ":" + identity).encode()).hexdigest()
                    headers["X-Content-SHA256"] = asset.descriptor.checksum_sha256
                    with asset.path.open("rb") as content:
                        raw = await self.adapter.json(
                            self.upstream, "/assets", headers, files=[
                                ("file", (asset.descriptor.filename, content, asset.descriptor.mime_type)),
                            ])
                    descriptor = self.adapter.asset_descriptor(raw)
                    if (descriptor.checksum_sha256 != asset.descriptor.checksum_sha256 or descriptor.status != "ready"
                            or descriptor.mime_type != asset.descriptor.mime_type or descriptor.size != asset.descriptor.size):
                        raise bad_response("KEMO_ASSET_UPLOAD_MISMATCH")
                    self.inputs[identity] = descriptor.id
                value = {**value, "asset_id": self.inputs[identity]}
                value.pop("source", None)
        return value

    def metadata(self, value):
        multimodal = value.get("metadata", {}).get("multimodal", {})
        for asset in multimodal.get("assets", []):
            if asset.get("asset_id") in self.inputs:
                asset["asset_id"] = self.inputs[asset["asset_id"]]

    async def publish(self, value):
        if isinstance(value, list):
            return [await self.publish(item) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("type") in {"message", "tool_result"}:
            return {**value, "content": await self.publish(value["content"])}
        identity = value.get("asset_id")
        if value.get("type") in MEDIA and identity:
            if not re.fullmatch(r"asset_[A-Za-z0-9_-]+", identity):
                raise bad_response("KEMO_INVALID_ASSET_ID")
            if self.context.assets is None:
                raise unsupported("Kemo 媒体结果需要 Asset 存储")
            if identity not in self.outputs:
                raw = await self.adapter.json(
                    self.upstream, "/assets/" + identity, self.headers, method="GET")
                descriptor = self.adapter.asset_descriptor(raw)
                if (descriptor.id != identity or descriptor.status != "ready"
                        or descriptor.mime_type != value.get("mime_type")
                        or descriptor.checksum_sha256 != value.get("checksum_sha256")):
                    raise bad_response("KEMO_ASSET_DESCRIPTOR_MISMATCH")
                async with self.adapter.transport.response(
                    self.upstream, "/assets/" + identity + "/content", self.headers, method="GET",
                ) as response:
                    try:
                        local = await self.context.assets.store_output(
                            filename=descriptor.filename, mime_type=descriptor.mime_type,
                            content=response.aiter_bytes(), checksum_sha256=descriptor.checksum_sha256,
                            metadata={"request_id": self.context.request_id, "upstream_asset_id": identity},
                        )
                    except AssetStoreFailure as exc:
                        raise ProviderException(exc.error) from None
                if local.size != descriptor.size:
                    raise bad_response("KEMO_ASSET_SIZE_MISMATCH")
                self.outputs[identity] = local
            local = self.outputs[identity]
            if (local.mime_type != value.get("mime_type")
                    or local.checksum_sha256 != value.get("checksum_sha256")):
                raise bad_response("KEMO_ASSET_DESCRIPTOR_MISMATCH")
            value = {**value, "asset_id": local.id}
            value.pop("source", None)
        return value


class KemoV2(DiscoverableAdapter):
    version = "2.0"
    manifest = AdapterManifest(
        id="kemo-v2", name="Kemo Protocol 2.0", operations=("llm", "embedding", "rerank"),
        auth_schemes=(BEARER,), streaming=True, capability_discovery=True, dynamic_reasoning=True,
        endpoints={"llm": "/model/responses", "embedding": "/model/embeddings",
                   "rerank": "/model/rerank", "discovery": "/model/models"},
        media={"input": ("text", "image", "audio", "video", "file"),
               "output": ("text", "image", "audio", "video", "file", "embedding", "score")},
        multiple_choices=True, structured_output=True, persisted_reasoning=True, multimodal_tool_results=True,
        reasoning_returns=("none", "auto", "summary", "content"), prediction=True,
        service_tiers=True, logprobs=True, verbosity=True,
        body_fields=tuple(dict.fromkeys((*KemoRequest.model_fields, *EmbeddingRequest.model_fields, *RerankRequest.model_fields))),
        usage_semantics="native Kemo usage; unknown values remain unknown",
    )

    def headers(self, headers, request_id=None):
        hops = current_hops.get() + 1
        if hops > MAX_HOPS:
            raise ProviderException(ErrorObject(type="federation", code="PROTOCOL_ERROR",
                                               message="网关联邦超过最大跳数", retryable=False))
        values = RequestHeaders(
            {name: value for name, value in headers.items()
             if name.lower() not in {"x-kemo-protocol-version", HOPS_HEADER.lower(), "idempotency-key"}},
            secrets=getattr(headers, "secrets", ()),
        )
        values.update({"X-Kemo-Protocol-Version": self.version, HOPS_HEADER: str(hops)})
        if request_id:
            values["Idempotency-Key"] = request_id
        return values

    @staticmethod
    def request_id(request_id, context, model, upstream):
        namespace = (context.tenant_id, context.subject_id, context.gateway_key_id,
                     upstream.id, upstream.base_url, model, request_id)
        return "req_" + hashlib.sha256(json.dumps(namespace).encode()).hexdigest()

    def encode(self, request, context, model, upstream):
        value = request.model_dump(mode="json", by_alias=True, exclude_none=True)
        value.update(protocol_version=self.version, model=model,
                     request_id=self.request_id(request.request_id, context, model, upstream))
        if "parent_request_id" in value:
            value["parent_request_id"] = self.request_id(value["parent_request_id"], context, model, upstream)
        if isinstance(request, KemoRequest):
            value["system_prompt"] = system_text(request, context)
            for item in value["input"]:
                state = item.get("provider_state")
                if state:
                    try:
                        wrapped = json.loads(state["data"])
                        if (state.get("version") != "kemo-federation-1"
                                or wrapped["upstream"] != [upstream.id, upstream.base_url, model]):
                            raise ValueError("state belongs to another deployment")
                        item["provider_state"] = ProviderState.model_validate(wrapped["state"]).model_dump(mode="json")
                    except (ValueError, KeyError, TypeError):
                        raise unsupported("持久推理状态不属于当前 Kemo 上游，不能跨来源重用") from None
        return value

    @staticmethod
    def export_state(result, request, model, upstream):
        for item in result.output:
            state = item.get("provider_state")
            if state:
                item["provider_state"] = {
                    **state, "kind": "opaque", "provider": "gateway", "model": request.model,
                    "version": "kemo-federation-1",
                    "data": json.dumps({"upstream": [upstream.id, upstream.base_url, model], "state": state}),
                }
        return result

    def decode(self, value):
        if value.get("protocol_version") != self.version:
            raise bad_response("KEMO_PROTOCOL_VERSION_MISMATCH")
        return value

    def asset_descriptor(self, value):
        try:
            return AssetDescriptor.model_validate(self.decode(value))
        except ValidationError:
            raise bad_response("KEMO_INVALID_ASSET") from None

    def error(self, value):
        return ErrorObject.model_validate(value)

    def http_error(self, exc):
        body = (exc.error.details or {}).get("upstream_body")
        if body:
            try:
                value = json.loads(body)
                if value.get("object") == "kemo.error" and value.get("protocol_version") == self.version:
                    error = self.error(value["error"])
                    error.provider_status = exc.error.provider_status
                    return ProviderException(error)
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        return exc

    async def json(self, upstream, path, headers, **kwargs):
        try:
            return await self.transport.json(upstream, path, headers, **kwargs)
        except ProviderException as exc:
            raise self.http_error(exc) from exc

    async def discover(self, upstream, headers):
        value = self.decode(await self.json(upstream, "/model/models", self.headers(headers), method="GET"))
        try:
            return [item.id for item in ModelCatalogResponse.model_validate(value).data]
        except ValidationError:
            raise bad_response("KEMO_INVALID_CATALOG") from None

    async def describe_model(self, upstream, model, headers):
        value = self.decode(await self.json(
            upstream, "/model/capabilities?model=" + quote(model, safe=""), self.headers(headers), method="GET"))
        try:
            caps = ModelCapabilities.model_validate(value)
        except ValidationError:
            raise bad_response("KEMO_INVALID_CAPABILITIES") from None
        if caps.model != model:
            raise bad_response("KEMO_CAPABILITIES_MODEL_MISMATCH")
        return {"capabilities": caps.model_dump(mode="json")}

    def result(self, raw, request_id, model):
        response = KemoResponse.model_validate(self.decode(raw))
        if response.request_id != request_id or response.model != model:
            raise bad_response("KEMO_RESPONSE_IDENTITY_MISMATCH")
        return ProviderResult(
            status=response.status, output=[item.model_dump(mode="json", by_alias=True) for item in response.output],
            usage=response.usage, provider_response_id=response.id, error=response.error,
            incomplete_details=response.incomplete_details.model_dump(mode="json") if response.incomplete_details else None,
            metadata=response.metadata, extensions=response.extensions,
        )

    async def execute(self, request, context, model, upstream, headers):
        body = self.encode(request, context, model, upstream)
        headers = self.headers(headers, body["request_id"])
        transfer = AssetTransfer(self, context, upstream, headers)
        body["input"] = await transfer.upload(body["input"])
        transfer.metadata(body)
        raw = await self.json(upstream, "/model/responses", headers, body=body)
        try:
            if request.generation.n > 1:
                batch = KemoResponseBatch.model_validate(self.decode(raw))
                if batch.request_id != body["request_id"] or batch.model != model:
                    raise bad_response("KEMO_RESPONSE_IDENTITY_MISMATCH")
                results = []
                for response in batch.responses:
                    item = response.model_dump(mode="json", by_alias=True)
                    result = self.export_state(self.result(item, body["request_id"], model), request, model, upstream)
                    result.output = await transfer.publish(result.output)
                    results.append(ProviderChoiceResult(response.choice_index, result))
                return ProviderBatchResult(results, batch.usage)
            result = self.export_state(self.result(raw, body["request_id"], model), request, model, upstream)
            result.output = await transfer.publish(result.output)
            return result
        except (ValidationError, ValueError, KeyError, TypeError):
            raise bad_response() from None
        except ProviderException as exc:
            # The request has already executed. Asset/read failures must not generate it again.
            exc.error.retryable = False
            raise

    def event(self, value):
        return ProviderStreamEvent.model_validate(value)

    def stream_guard(self):
        return StreamSequenceGuard()

    async def stream(self, request, context, model, upstream, headers):
        body = self.encode(request, context, model, upstream)
        headers = self.headers(headers, body["request_id"])
        transfer = AssetTransfer(self, context, upstream, headers)
        body["input"] = await transfer.upload(body["input"])
        transfer.metadata(body)
        guard, terminal, response_id = self.stream_guard(), False, None
        try:
            async with aclosing(self.transport.events(upstream, "/model/responses", headers, body)) as events:
                async for raw in events:
                    event = self.event(raw)
                    if (event.request_id != body["request_id"]
                            or event.response is not None and (
                                event.response.id != event.response_id
                                or event.response.request_id != event.request_id)):
                        raise bad_response("KEMO_RESPONSE_IDENTITY_MISMATCH")
                    if not guard.accept(event):
                        continue
                    response_id = event.response_id
                    name = event.type
                    if name in {"response.created", "response.in_progress"}:
                        continue
                    if event.terminal:
                        terminal = True
                        if name == "error":
                            yield ProviderEvent(Kind.ERROR, error=event.error, provider_response_id=response_id)
                        else:
                            result = self.export_state(
                                self.result(raw["response"], body["request_id"], model), request, model, upstream)
                            result.output = await transfer.publish(result.output)
                            yield ProviderEvent(
                                Kind("provider." + name.split(".")[1]), result=result, provider_response_id=response_id)
                        continue
                    item = event.item.model_dump(mode="json", by_alias=True) if event.item else None
                    if item and name == "output_media.completed":
                        item = await transfer.publish(item)
                    yield ProviderEvent(
                        Kind(name), item_id=event.item_id, content_index=event.content_index,
                        call_id=event.call_id, name=event.name, delta=event.delta, text=event.text,
                        item=item, usage=event.usage, provider_response_id=response_id,
                    )
            if not terminal:
                raise bad_response("KEMO_STREAM_MISSING_TERMINAL")
        except (ValidationError, ValueError, KeyError, TypeError, StreamProtocolError):
            raise bad_response("KEMO_INVALID_STREAM") from None
        except ProviderException as exc:
            error = self.http_error(exc)
            if response_id:
                error.error.retryable = False
            raise error from exc
        finally:
            if response_id and not terminal:
                try:
                    async with asyncio.timeout(3):
                        await self.json(
                            upstream, "/model/responses/" + quote(response_id, safe="") + "/cancel",
                            headers, body={})
                except (ProviderException, TimeoutError):
                    logger.warning("Kemo upstream cancellation failed for %s", response_id)

    async def embed(self, request, context, model, upstream, headers):
        body = self.encode(request, context, model, upstream)
        raw = await self.json(upstream, "/model/embeddings", self.headers(headers, body["request_id"]), body=body)
        try:
            value = EmbeddingResponse.model_validate(self.decode(raw))
        except ValidationError:
            raise bad_response("KEMO_INVALID_EMBEDDINGS") from None
        if value.request_id != body["request_id"] or value.model != model:
            raise bad_response("KEMO_RESPONSE_IDENTITY_MISMATCH")
        if [(item.id, item.index) for item in value.data] != [(item.id, i) for i, item in enumerate(request.inputs)]:
            raise bad_response("KEMO_EMBEDDING_IDENTITY_MISMATCH")
        return ProviderEmbeddingResult(
            [ProviderEmbedding(item.index, item.vector) for item in value.data], value.vector_space_id,
            truncated_ids=value.truncated_ids, truncation_reported="truncated_ids" in raw,
            usage=value.usage, model_version=value.model_version, provider_response_id=value.provider_response_id,
            metadata=value.metadata, extensions=value.extensions,
        )

    async def rerank(self, request, context, model, upstream, headers):
        body = self.encode(request, context, model, upstream)
        # The local executor applies filtering and top_n after validating all scores.
        body.update(top_n=len(request.documents), return_documents=False)
        body.pop("score_threshold", None)
        raw = await self.json(upstream, "/model/rerank", self.headers(headers, body["request_id"]), body=body)
        try:
            value = RerankResponse.model_validate(self.decode(raw))
        except ValidationError:
            raise bad_response("KEMO_INVALID_RERANK") from None
        if value.request_id != body["request_id"] or value.model != model:
            raise bad_response("KEMO_RESPONSE_IDENTITY_MISMATCH")
        if any(item.index >= len(request.documents) or item.document_id != request.documents[item.index].id
               for item in value.results):
            raise bad_response("KEMO_RERANK_IDENTITY_MISMATCH")
        return ProviderRerankResult(
            [ProviderRerankItem(item.index, item.relevance_score) for item in value.results],
            usage=value.usage, model_version=value.model_version, provider_response_id=value.provider_response_id,
            metadata=value.metadata, extensions=value.extensions,
        )
