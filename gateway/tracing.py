"""One context-local trace per metered upstream attempt; authentication headers are excluded."""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import aclosing, contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
import json
import time
from urllib.parse import quote, quote_plus


current_trace: ContextVar["AttemptTrace | None"] = ContextVar("gateway_attempt_trace", default=None)


def redact_secrets(text, secrets):
    # Query credentials may be echoed in URLs by upstreams or exception messages.
    variants = set()
    for secret in secrets:
        if secret:
            for value in (secret, quote(secret, safe=""), quote_plus(secret, safe="")):
                variants.update((value, json.dumps(value, ensure_ascii=False)[1:-1],
                                 json.dumps(value, ensure_ascii=True)[1:-1]))
    for value in sorted(variants, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


def document(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if is_dataclass(value):
        return json.loads(json.dumps(asdict(value), default=str))
    return value


@dataclass
class AttemptTrace:
    request: dict | None
    tenant_id: str = "gateway"
    operation: str | None = None
    started: float = field(default_factory=time.monotonic)
    response: object = None
    exchanges: list = field(default_factory=list)
    stream_bodies: list = field(default_factory=list, repr=False)
    first_token_ms: float | None = None
    secrets: tuple[str, ...] = ()
    capture_details: bool = True
    @contextmanager
    def capture(self):
        token = current_trace.set(self)
        try:
            yield
        finally:
            current_trace.reset(token)

    async def events(self, source):
        # Never hold a ContextVar token across a yield to the execution owner:
        # terminal generators may be closed by a different asyncio task.
        async with aclosing(source):
            while True:
                with self.capture():
                    try:
                        event = await anext(source)
                    except StopAsyncIteration:
                        return
                yield event

    def text(self, delta):
        if delta and self.first_token_ms is None:
            self.first_token_ms = (time.monotonic() - self.started) * 1000

    def payload(self, max_bytes=4 * 1024 * 1024):
        # Redact before truncation so a cut cannot leave a partial credential.
        for exchange, chunks in self.stream_bodies:
            exchange["response"] = b"".join(chunks).decode("utf-8", errors="replace")
        text = json.dumps({
            "tenant_id": self.tenant_id, "operation": self.operation,
            "request": self.request, "response": document(self.response),
            "upstream_exchanges": self.exchanges,
        }, ensure_ascii=False)
        text = redact_secrets(text, self.secrets)
        encoded = text.encode("utf-8")
        if len(encoded) > max_bytes:
            result = {"truncated": True, "original_bytes": len(encoded), "body": ""}
            budget = max_bytes - len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            prefix = encoded[:budget].decode("utf-8", errors="ignore")
            result["body"] = prefix
            # Include JSON quoting and metadata in the stored byte budget.
            size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            while size > max_bytes:
                prefix_bytes = prefix.encode("utf-8")
                prefix = prefix_bytes[:len(prefix_bytes) - (size - max_bytes)].decode("utf-8", errors="ignore")
                result["body"] = prefix
                size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            return result
        return json.loads(text)
