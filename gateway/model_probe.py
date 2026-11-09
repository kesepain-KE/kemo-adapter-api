"""Explicit control-plane model tests and capability checks, with metered calls."""
from __future__ import annotations

import asyncio
import base64
from contextlib import aclosing
import hashlib
import json
from pathlib import Path
import re
import secrets
import struct
import time
from uuid import uuid4
import zlib

import jsonschema

from core.models import EmbeddingRequest, KemoRequest, RerankRequest, Usage
from core.provider_contract import ProviderEventKind as Kind, ProviderException, RequestContext
from core.yaml_config import read_yaml
from gateway.domain import Deployment, PublicModel
from gateway.extensions import WorkerAdapter
from gateway.runtime import adapter_modalities
from gateway.store import utc_now
from gateway.transport import upstream_error
from gateway.tracing import document


def response_text(result):
    return "\n".join(
        block["text"] for item in result.output if item["type"] == "message"
        for block in item.get("content", []) if block["type"] == "text"
    )


def probe_image():
    color, rgb = secrets.choice((("red", (255, 0, 0)), ("green", (0, 255, 0)), ("blue", (0, 0, 255))))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    pixels = (b"\x00" + bytes(rgb) * 64) * 64
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
    return color, "data:image/png;base64," + base64.b64encode(png).decode()


class ModelProbe:
    def __init__(self, registry, generation, upstream, credential, deployment, task, *, assets, subject="capability-probe"):
        self.registry, self.generation, self.upstream = registry, generation, upstream
        self.adapter = generation.adapters[upstream.adapter_id]
        self.source = json.loads(upstream.worker_document) if isinstance(self.adapter, WorkerAdapter) else upstream
        self.headers = registry.router.headers(generation, upstream, credential)
        self.credential = credential
        self.deployment, self.task = deployment, task
        self.assets = assets
        self.subject = subject
        self.checks = {}

    def request(self, text="Reply with OK.", **fields):
        return KemoRequest.create(
            model=self.deployment.public_model, stream=False,
            input=[{"type": "message", "id": "msg_probe", "role": "user",
                    "content": [{"type": "text", "text": text}]}],
            generation={"max_output_tokens": 128}, **fields,
        )

    async def call(self, name, request, *, method="execute", accept, stream_report=None):
        identity = uuid4().hex
        context = RequestContext(
            tenant_id="gateway", subject_id=self.subject, request_id="req_" + identity,
            response_id="resp_" + identity, trace_id=identity,
            assets=self.assets.bind("gateway", self.subject),
        )
        started = self.registry.router.metering.start(
            context, 1, self.deployment, self.upstream, request=request, method=method,
            credential=self.credential, task=self.task,
        )
        started.secrets = getattr(self.headers, "secrets", ())
        call_started = time.perf_counter()
        usage, error, cancelled = Usage(), None, False
        try:
            async with asyncio.timeout(min(self.upstream.timeout, 20)):
                if method == "stream":
                    terminal, text = False, False
                    async with aclosing(started.events(self.adapter.stream(
                        request, context, self.deployment.upstream_model, self.source, self.headers,
                    ))) as events:
                        async for event in events:
                            if event.usage is not None:
                                usage = event.usage
                            if event.result is not None:
                                usage = event.result.usage
                                started.response = document(event.result)
                            if event.kind == Kind.TEXT_DELTA:
                                started.text(event.delta)
                            if event.error is not None:
                                raise ProviderException(event.error)
                            if stream_report is not None and event.kind == Kind.TEXT_DELTA and event.delta:
                                if stream_report["first_token_ms"] is None:
                                    stream_report["first_token_ms"] = round((time.perf_counter() - call_started) * 1000)
                                stream_report["preview"] = (stream_report["preview"] + event.delta)[:1000]
                            text |= event.kind == Kind.TEXT_DELTA and bool(event.delta)
                            terminal |= event.kind == Kind.COMPLETED
                    result = terminal and text
                    if stream_report is not None:
                        stream_report["usage"] = usage.model_dump(mode="json")
                else:
                    with started.capture():
                        result = await getattr(self.adapter, method)(
                            request, context, self.deployment.upstream_model, self.source, self.headers,
                        )
                    usage = result.usage
                    started.response = document(result)
                    if method == "execute" and result.error is not None:
                        raise ProviderException(result.error)
                supported = accept(result)
                self.registry.router.record_credential(self.generation, self.upstream, self.credential)
                self.checks[name] = {"status": "supported" if supported else "unknown",
                                     "reason": "response-confirmed" if supported else "response-not-confirmed"}
                return result if supported else None
        except ProviderException as exc:
            error = exc.error
            self.registry.router.record_credential(self.generation, self.upstream, self.credential, error)
            self.checks[name] = {
                "status": "unsupported" if error.code == "CAPABILITY_ERROR" or error.provider_status in {400, 404, 415, 422} else "unknown",
                "reason": error.code, "provider_status": error.provider_status,
            }
        except TimeoutError:
            error = upstream_error(code="UPSTREAM_TIMEOUT").error
            self.registry.router.record_credential(self.generation, self.upstream, self.credential, error)
            self.checks[name] = {"status": "unknown", "reason": "PROVIDER_TIMEOUT"}
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self.registry.router.metering.finish(
                context, 1, started, self.deployment, usage, error=error, cancelled=cancelled,
                upstream=self.upstream, task=self.task,
            )

    async def metadata(self, caps):
        if not self.adapter.manifest.discovery:
            return {}
        try:
            async with asyncio.timeout(min(self.upstream.timeout, 20)):
                raw = await self.adapter.describe_model(
                    self.source, self.deployment.upstream_model, self.headers,
                )
            limits = raw.get("limits", {})
            declared = [effort for effort in raw.get("reasoning_efforts", [])
                        if self.checks.get("reasoning." + effort, {}).get("status") != "unsupported"]
            if declared and self.adapter.manifest.reasoning_returns:
                reasoning = caps.setdefault("reasoning", {
                    "supported": True, "efforts": [],
                    "returns": list(self.adapter.manifest.reasoning_returns),
                })
                for effort in declared:
                    if effort not in reasoning["efforts"]:
                        reasoning["efforts"].append(effort)
                    if self.checks.get("reasoning." + effort, {}).get("status") != "supported":
                        self.checks["reasoning." + effort] = {"status": "declared", "reason": "upstream-metadata"}
            self.checks["limits"] = {"status": "declared" if limits else "unknown",
                                     "reason": "upstream-metadata" if limits else "metadata-not-provided"}
            return limits
        except (ProviderException, TimeoutError, ValueError, TypeError):
            self.checks["limits"] = {"status": "unknown", "reason": "metadata-unavailable"}
            return {}

    async def basic(self):
        if self.task == "embedding":
            request = EmbeddingRequest(
                protocol_version="2.0", request_id="req_" + uuid4().hex, model=self.deployment.public_model,
                input_type="document", inputs=[{"id": "probe", "text": "hello"}],
            )
            return await self.call("embedding", request, method="embed",
                                   accept=lambda value: bool(value.embeddings) and bool(value.embeddings[0].vector))
        if self.task == "rerank":
            request = RerankRequest(
                protocol_version="2.0", request_id="req_" + uuid4().hex, model=self.deployment.public_model,
                query="hello", documents=[{"id": "one", "text": "hello"}, {"id": "two", "text": "goodbye"}],
            )
            return await self.call("rerank", request, method="rerank", accept=lambda value: bool(value.results))
        return await self.call("text", self.request(),
                               accept=lambda value: value.status == "completed" and bool(response_text(value)))

    async def test(self, *, first_token=False):
        started = time.perf_counter()
        streaming = first_token and self.task == "llm" and self.adapter.manifest.streaming
        stream_report = {"first_token_ms": None, "preview": ""}
        if streaming:
            result = await self.call(
                "text", self.request().model_copy(update={"stream": True}),
                method="stream", accept=bool, stream_report=stream_report,
            )
        else:
            result = await self.basic()
        check = self.checks[self.task if self.task != "llm" else "text"]
        report = {**check, "latency_ms": round((time.perf_counter() - started) * 1000)}
        if first_token:
            report.update(streaming=streaming, first_token_ms=stream_report["first_token_ms"])
        if result is not None:
            if streaming:
                report.update(stream_report)
            else:
                report["usage"] = result.usage.model_dump(mode="json")
            if self.task == "llm":
                if not streaming:
                    report["preview"] = response_text(result)[:1000]
            elif self.task == "embedding":
                report["dimensions"] = len(result.embeddings[0].vector)
            else:
                report["result_count"] = len(result.results)
        return report

    async def run(self):
        caps = {"input_modalities": ["text"], "output_modalities": ["text"], "streaming": False}
        result = await self.basic()
        if self.task == "embedding":
            if result is None:
                raise upstream_error(code="EMBEDDING_PROBE_FAILED")
            dimensions = len(result.embeddings[0].vector)
            caps.update(output_modalities=[], embedding={
                "input_types": list(self.adapter.manifest.embedding_input_types),
                "default_dimensions": dimensions,
                "supported_dimensions": [dimensions], "max_batch_size": 1,
                "normalization": self.adapter.manifest.embedding_normalization,
            })
        elif self.task == "rerank":
            if result is None:
                raise upstream_error(code="RERANK_PROBE_FAILED")
            caps.update(output_modalities=[], rerank={"max_documents": 2})
        else:
            if result is not None:
                await self.llm(caps)
            modalities = adapter_modalities(self.adapter)
            for modality in ("audio", "video", "file"):
                if modality not in modalities["input"]:
                    self.checks[modality] = {"status": "unsupported", "reason": "adapter-not-implemented"}
                elif result is None:
                    self.checks[modality] = {"status": "unknown", "reason": "basic-test-failed"}
                elif isinstance(self.adapter, WorkerAdapter):
                    self.checks[modality] = {"status": "unknown", "reason": "asset-access-unavailable"}
                else:
                    await self.media(modality, caps)
            for modality in ("image", "audio", "video", "file"):
                self.checks["output." + modality] = {
                    "status": "unknown" if modality in modalities["output"] else "unsupported",
                    "reason": "sample-required" if modality in modalities["output"] else "adapter-not-implemented",
                }
        caps["limits"] = await self.metadata(caps)
        supported = self.checks.get(self.task if self.task != "llm" else "text", {}).get("status") == "supported"
        model = PublicModel(
            id=self.deployment.public_model, task=self.task, capabilities=caps,
            verification="verified" if supported else "unknown",
            verified_at=utc_now() if supported else None,
            evidence=json.dumps({"source": "registration-probe", "upstream_id": self.upstream.id,
                                 "upstream_model": self.deployment.upstream_model, "checks": self.checks}),
        )
        return model, {"model": model.id, "verification": model.verification, "checks": self.checks}

    async def media(self, modality, caps):
        root = Path(__file__).with_name("probe_samples")
        sample = read_yaml(root / "manifest.yaml")["samples"][modality]
        data = (root / sample["filename"]).read_bytes()
        checksum = hashlib.sha256(data).hexdigest()
        async def chunks():
            yield data
        asset = await self.assets.store_input(
            tenant_id="gateway", subject_id=self.subject,
            filename=sample["filename"], mime_type=sample["mime_type"], chunks=chunks(),
            metadata={"probe_sample": sample["id"]},
            idempotency_key=f'probe:{sample["id"]}:{checksum}', checksum_sha256=checksum,
        )
        block = {"type": modality, "mime_type": sample["mime_type"], "asset_id": asset.id}
        if modality == "file":
            block["filename"] = sample["filename"]
        operation = sample.get("operation")
        document = self.request(
            sample["prompt"], metadata={"capability": operation} if operation else {},
        ).model_dump(mode="json")
        document["input"][0]["content"].append(block)
        result = await self.call(
            modality, KemoRequest.model_validate(document),
            accept=lambda value: value.status == "completed" and bool(
                re.search(sample["answer_pattern"], response_text(value), re.IGNORECASE | re.DOTALL)),
        )
        self.checks[modality]["sample_id"] = sample["id"]
        if result is not None:
            caps["input_modalities"].append(modality)
            if operation:
                caps.setdefault("extensions", {}).setdefault("operations", {})[operation] = True

    async def llm(self, caps):
        manifest = self.adapter.manifest
        if self.adapter.manifest.streaming:
            caps["streaming"] = bool(await self.call(
                "streaming", self.request().model_copy(update={"stream": True}), method="stream", accept=bool,
            ))
        if manifest.tools:
            tool = {
                "type": "function", "name": "kemo_capability_probe", "description": "Return the probe value.",
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                               "required": ["value"], "additionalProperties": False}, "strict": False,
            }
            result = await self.call(
                "tools", self.request(
                    "Call kemo_capability_probe with value ok.",
                    tools=[tool],
                    tool_choice={"mode": "named", "name": "kemo_capability_probe"}, parallel_tool_calls=False,
                ),
                accept=lambda value: any(item["type"] == "tool_call" and item.get("name") == "kemo_capability_probe"
                                         and item.get("arguments") == {"value": "ok"} for item in value.output),
            )
            if result is not None:
                caps["tools"] = {"function_calling": True, "parallel_calls": False,
                                 "tool_choice_modes": ["auto", "none", "named"]}
                parallel = await self.call(
                    "tools.parallel_calls", self.request(
                        "Call kemo_capability_probe twice in the same response: once with value first, "
                        "and once with value second.",
                        tools=[tool], tool_choice={"mode": "auto"}, parallel_tool_calls=True,
                    ),
                    accept=lambda value: all(any(
                        item["type"] == "tool_call" and item.get("name") == "kemo_capability_probe"
                        and item.get("arguments") == {"value": expected} for item in value.output
                    ) for expected in ("first", "second")),
                )
                caps["tools"]["parallel_calls"] = parallel is not None
        if manifest.structured_output:
            schema = {"type": "object", "properties": {"ok": {"type": "boolean", "const": True}},
                      "required": ["ok"], "additionalProperties": False}
            def valid_json(result):
                try:
                    jsonschema.validate(json.loads(response_text(result)), schema)
                    return True
                except (ValueError, jsonschema.ValidationError):
                    return False
            result = await self.call("structured_output", self.request(
                'Return JSON with ok set to true.',
                structured_output={"type": "json_schema", "schema_name": "kemo_probe", "schema": schema},
            ), accept=valid_json)
            if result is not None:
                caps["structured_output"] = {"supported": True, "strict": False, "schema_subset": ["json_schema"]}
        if "image" in manifest.media.input:
            color, uri = probe_image()
            request = self.request("What color is this square? Reply with one lowercase color word.")
            document = request.model_dump(mode="json")
            document["input"][0]["content"].append({
                "type": "image", "mime_type": "image/png", "source": {"kind": "data_url", "uri": uri},
            })
            if await self.call("vision", KemoRequest.model_validate(document),
                               accept=lambda value: bool(re.search(r"\b" + color + r"\b", response_text(value).lower()))):
                caps["input_modalities"].append("image")
                caps["extensions"] = {"operations": {"vision": True}}
        if manifest.reasoning_returns:
            efforts = []
            candidates = self.upstream.profile.get("reasoning_effort_map", manifest.reasoning_levels)
            for effort in candidates:
                result = await self.call(
                    "reasoning." + effort,
                    self.request("What is 17 times 19? Reply briefly.",
                                 reasoning={"enabled": True, "effort": effort, "return": "auto"}),
                    accept=lambda value: bool(value.usage.reasoning_tokens) or any(
                        item["type"] == "reasoning" for item in value.output),
                )
                if result is not None:
                    efforts.append(effort)
            if efforts:
                caps["reasoning"] = {
                    "supported": True, "efforts": efforts,
                    "returns": list(manifest.reasoning_returns),
                    "summary": "summary" in manifest.reasoning_returns,
                }
