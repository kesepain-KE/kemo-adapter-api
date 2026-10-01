from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.server import create_app
from core.config import PrincipalConfig, Settings
from core.models import LimitsCapabilities, ModelCapabilities, Usage, UsageMeasurement
from core.provider_contract import (
    ProviderBatchResult,
    ProviderChoiceResult,
    ProviderPackage,
    ProviderResult,
    RequestContext,
)
from tests.support.project import project


MODEL = "batch-model"


class BatchProvider(ProviderPackage):
    provider_id = "batch"

    def __init__(self, *, missing_choice: bool = False) -> None:
        self.calls = 0
        self.missing_choice = missing_choice

    @property
    def models(self) -> frozenset[str]:
        return frozenset({MODEL})

    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            protocol_version="2.0",
            model=model,
            provider_id=self.provider_id,
            provider_model="model",
            input_modalities=["text"],
            output_modalities=["text"],
            streaming=False,
            supports_multiple_choices=True,
            limits=LimitsCapabilities(max_choices=2),
        )

    async def execute(
        self, request, context: RequestContext
    ) -> ProviderBatchResult:
        del context
        self.calls += 1
        choices = [
            ProviderChoiceResult(
                choice_index=index,
                result=ProviderResult(
                    status="completed",
                    output=[
                        {
                            "id": f"msg_choice_{index}",
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "status": "completed",
                            "content": [
                                {"type": "text", "text": f"choice-{index}"}
                            ],
                        }
                    ],
                ),
            )
            for index in range(1 if self.missing_choice else request.generation.n)
        ]
        return ProviderBatchResult(
            responses=choices,
            usage=Usage(
                input_tokens=40,
                output_tokens=60,
                total_tokens=100,
                measurement=UsageMeasurement(
                    mode="provider",
                    exact=True,
                    exact_fields=["input_tokens", "output_tokens", "total_tokens"],
                ),
            ),
        )


def _app(tmp_path: Path, provider: BatchProvider):
    root = project(tmp_path)
    app = create_app(
        Settings(
            api_keys={
                "batch-token": PrincipalConfig(
                    "tenant-batch",
                    "subject-batch",
                    frozenset({"owner", "model:invoke"}),
                )
            }
        ),
        live_config_root=root,
        statistics_root=root / "storage",
        discover_providers=False,
    )
    app.state.registry.register(provider)
    return app


def _headers(request_id: str = "req_batch_1") -> dict[str, str]:
    return {
        "Authorization": "Bearer batch-token",
        "X-Kemo-Protocol-Version": "2.0",
        "Idempotency-Key": request_id,
    }


def _body(request_id: str = "req_batch_1") -> dict:
    return {
        "protocol_version": "2.0",
        "request_id": request_id,
        "attempt": 1,
        "model": MODEL,
        "stream": False,
        "system_prompt": "",
        "generation": {"n": 2, "max_output_tokens": 32},
        "output": {"modalities": ["text"]},
        "tools": [],
        "input": [
            {
                "id": "msg_batch_user",
                "type": "message",
                "role": "user",
                "status": "completed",
                "content": [{"type": "text", "text": "two answers"}],
            }
        ],
        "provider_options": {},
        "metadata": {},
        "extensions": {},
    }


def test_batch_uses_aggregate_usage_stable_ids_replay_and_candidate_query(
    tmp_path: Path,
) -> None:
    provider = BatchProvider()
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        first = client.post("/model/responses", headers=_headers(), json=_body())
        replay = client.post("/model/responses", headers=_headers(), json=_body())

        assert first.status_code == 200, first.text
        payload = first.json()
        assert payload["object"] == "kemo.response_batch"
        assert payload["usage"]["total_tokens"] == 100
        assert [item["choice_index"] for item in payload["responses"]] == [0, 1]
        assert all(item["choice_count"] == 2 for item in payload["responses"])
        assert len({item["id"] for item in payload["responses"]}) == 2
        assert all(item["usage"]["total_tokens"] is None for item in payload["responses"])
        assert replay.json() == payload
        assert provider.calls == 1

        for candidate in payload["responses"]:
            queried = client.get(
                f"/model/responses/{candidate['id']}",
                headers=_headers(),
            )
            assert queried.status_code == 200
            assert queried.json()["id"] == candidate["id"]


def test_batch_missing_candidate_is_shared_provider_error_and_not_reexecuted(
    tmp_path: Path,
) -> None:
    provider = BatchProvider(missing_choice=True)
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        first = client.post("/model/responses", headers=_headers(), json=_body())
        replay = client.post("/model/responses", headers=_headers(), json=_body())

    assert first.status_code == 502
    assert first.json()["error"]["code"] == "PROVIDER_BAD_RESPONSE"
    assert replay.status_code == 502
    assert replay.json()["error"]["code"] == "PROVIDER_BAD_RESPONSE"
    assert provider.calls == 1
