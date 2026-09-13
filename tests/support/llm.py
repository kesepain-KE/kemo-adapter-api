"""共享的无网络文本 Provider 与请求构造器。"""
from __future__ import annotations
from collections.abc import AsyncIterator
from core.models import KemoRequest, ModelCapabilities, Usage, UsageMeasurement
from core.provider_contract import ProviderEvent, ProviderEventKind, ProviderPackage, ProviderResult, RequestContext

class FakeProvider(ProviderPackage):
    provider_id = "fake"

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"fake-model"})

    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            model=model,
            input_modalities=["text"],
            output_modalities=["text"],
            streaming=True,
        )

    def result(self) -> ProviderResult:
        return ProviderResult(
            status="completed",
            output=[
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "status": "completed",
                    "content": [{"type": "text", "text": "ok"}],
                    "metadata": {},
                    "extensions": {},
                }
            ],
            usage=Usage(
                input_tokens=10,
                output_tokens=4,
                reasoning_tokens=3,
                total_tokens=14,
                measurement=UsageMeasurement(
                    mode="provider",
                    exact=True,
                    exact_fields=["input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"],
                ),
            ),
            provider_response_id="vendor_1",
        )

    async def execute(self, request: KemoRequest, context: RequestContext) -> ProviderResult:
        return self.result()

    async def _stream(
        self, request: KemoRequest, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(
            kind=ProviderEventKind.TEXT_DELTA,
            item_id="msg_1",
            content_index=0,
            delta="ok",
            provider_response_id="vendor_1",
        )
        yield ProviderEvent(kind=ProviderEventKind.USAGE, usage=self.result().usage)
        yield ProviderEvent(kind=ProviderEventKind.COMPLETED, result=self.result())

    def stream(
        self, request: KemoRequest, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        return self._stream(request, context)


def request(*, stream: bool, system_prompt: str = "system") -> KemoRequest:
    return KemoRequest(
        protocol_version="1.0",
        request_id="req_1",
        attempt=1,
        model="fake-model",
        stream=stream,
        system_prompt=system_prompt,
        generation={},
        output={"modalities": ["text"]},
        tools=[],
        input=[
            {
                "id": "msg_user_1",
                "type": "message",
                "role": "user",
                "status": "completed",
                "content": [{"type": "text", "text": "hello"}],
            }
        ],
        provider_options={},
        metadata={},
        extensions={},
    )
