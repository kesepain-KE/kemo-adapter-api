"""Runnable example for a fictional native JSON + SSE upstream.

POST /native/generate: {"engine": ..., "prompt": ..., "stream": bool}
JSON result: {"answer": "...", "usage": {"in": 1, "out": 2}}
SSE data: {"delta": "..."}, then {"done": true, "usage": {...}}
GET /native/models: {"engines": ["model-id"]}
Replace these mappings with the actual upstream protocol before installation.
"""
from core.provider_contract import ProviderEvent, ProviderEventKind as Kind, ProviderResult
from gateway.adapters.builtin.common import identifier, message, system_text, text_blocks
from gateway.metering import normalize_usage
from gateway.transport import upstream_error


class Adapter:
    def __init__(self, transport):
        self.transport = transport

    async def execute(self, request, context, upstream_model, upstream, headers):
        raw = await self.transport.json(
            upstream, "/native/generate", headers, body=self.encode(request, context, upstream_model, False),
        )
        return ProviderResult("completed", output=[message(raw["answer"])], usage=self.usage(raw.get("usage")))

    @staticmethod
    def encode(request, context, model, stream):
        prompt = "\n".join(text_blocks(item) for item in request.input if item.type == "message")
        return {"engine": model, "prompt": system_text(request, context) + "\n" + prompt, "stream": stream}

    @staticmethod
    def usage(raw):
        return normalize_usage(raw, input_field="in", output_field="out")

    async def stream(self, request, context, upstream_model, upstream, headers):
        item_id, text, finished = identifier("msg"), "", False
        async for raw in self.transport.events(
            upstream, "/native/generate", headers, self.encode(request, context, upstream_model, True),
        ):
            if raw.get("delta"):
                text += raw["delta"]
                yield ProviderEvent(Kind.TEXT_DELTA, item_id=item_id, content_index=0, delta=raw["delta"])
            if raw.get("done"):
                finished = True
                result = ProviderResult("completed", output=[message(text, item_id=item_id)],
                                        usage=self.usage(raw.get("usage")))
                yield ProviderEvent(Kind.COMPLETED, result=result)
                return
        if not finished:
            raise upstream_error(code="UPSTREAM_TRUNCATED_STREAM")

    async def discover(self, upstream, headers):
        raw = await self.transport.json(upstream, "/native/models", headers, method="GET")
        return raw["engines"]


def create_adapter(transport):
    return Adapter(transport)
