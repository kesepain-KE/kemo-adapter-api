from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from api.sse import encode_sse
from core.models import (
    AssetDescriptor,
    EmbeddingRequest,
    EmbeddingResponse,
    KemoRequest,
    KemoResponse,
    ModelCapabilities,
    ModelCatalogResponse,
    RerankRequest,
    RerankResponse,
    SSEEvent,
)
from tests.contracts.kemo_v1.fixture_loader import (
    load_bundle,
    materialize,
    stream_events,
    validate_stream_envelope,
)


BUNDLE = load_bundle()
MODELS = {
    "AssetDescriptor": AssetDescriptor,
    "EmbeddingRequest": EmbeddingRequest,
    "EmbeddingResponse": EmbeddingResponse,
    "KemoRequest": KemoRequest,
    "KemoResponse": KemoResponse,
    "ModelCapabilities": ModelCapabilities,
    "ModelCatalogResponse": ModelCatalogResponse,
    "RerankRequest": RerankRequest,
    "RerankResponse": RerankResponse,
    "SSEEvent": SSEEvent,
}


def _id(case: dict[str, object]) -> str:
    return str(case["id"])


@pytest.mark.parametrize("case", BUNDLE["valid_cases"], ids=_id)
def test_gateway_models_accept_shared_valid_fixture(case) -> None:
    payload = materialize(BUNDLE, case)
    model = MODELS[case["model"]]
    parsed = model.model_validate(payload)
    wire = parsed.model_dump(mode="json", by_alias=True, exclude_none=True)
    encoded = json.dumps(wire, ensure_ascii=False, allow_nan=False).encode("utf-8")
    reparsed = model.model_validate_json(encoded)
    assert reparsed.model_dump(mode="json", by_alias=True, exclude_none=True) == wire


@pytest.mark.parametrize("case", BUNDLE["invalid_cases"], ids=_id)
def test_gateway_models_reject_shared_invalid_fixture(case) -> None:
    payload = materialize(BUNDLE, case)
    with pytest.raises(ValidationError):
        MODELS[case["model"]].model_validate(payload)


@pytest.mark.parametrize("stream", BUNDLE["valid_streams"], ids=_id)
def test_gateway_sse_encoder_preserves_shared_stream_contract(stream) -> None:
    payloads = stream_events(BUNDLE, stream)
    assert validate_stream_envelope(payloads) == stream["accepted_sequences"]
    encoded = []
    for payload in payloads:
        event = SSEEvent.model_validate(payload)
        frame = encode_sse(event)
        assert frame.startswith(f"id: {event.event_id}\n".encode("utf-8"))
        data_line = next(line for line in frame.splitlines() if line.startswith(b"data: "))
        decoded = json.loads(data_line[6:].decode("utf-8"))
        reparsed = SSEEvent.model_validate(decoded)
        assert reparsed.event_id == event.event_id
        encoded.append(frame)
    assert b"".join(encoded).count(b"\n\n") == len(payloads)


@pytest.mark.parametrize("stream", BUNDLE["invalid_streams"], ids=_id)
def test_shared_stream_envelope_rejects_invalid_sequences(stream) -> None:
    with pytest.raises(ValueError):
        validate_stream_envelope(stream_events(BUNDLE, stream))


def test_dynamic_reasoning_and_nullable_item_time_are_locked_by_fixture() -> None:
    request = KemoRequest.model_validate(BUNDLE["documents"]["request_text"])
    response = KemoResponse.model_validate(BUNDLE["documents"]["response_completed"])
    capabilities = ModelCapabilities.model_validate(BUNDLE["documents"]["capability_llm"])
    assert request.reasoning is not None and request.reasoning.effort == "adaptive-high"
    assert "adaptive-high" in capabilities.reasoning.efforts
    assert response.output[0].created_at is None
