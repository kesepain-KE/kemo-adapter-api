from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.server import create_app
from core.config import PrincipalConfig, Settings
from core.models import ModelCapabilities
from core.provider_contract import ProviderPackage
from tests.test_live_config import project, write_json
from tests.test_provider_boundary import FakeProvider, request
from tests.test_retrieval_api import FakeRetrievalProvider


class BrokenCapabilitiesProvider(ProviderPackage):
    provider_id = "broken"

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"broken-model"})

    async def capabilities(self, model: str) -> ModelCapabilities:
        del model
        raise RuntimeError("secret provider failure")


def catalog_project(tmp_path: Path) -> Path:
    root = project(tmp_path)
    write_json(root / "api" / "keys.json", {"keys": {}})
    return root


def catalog_app(tmp_path: Path):
    settings = Settings(
        api_keys={
            "all-token": PrincipalConfig(
                "tenant", "agent", frozenset({"model:invoke"})
            ),
            "embedding-token": PrincipalConfig(
                "tenant", "embedder", frozenset({"embedding:invoke"})
            ),
            "limited-token": PrincipalConfig(
                "tenant",
                "limited",
                frozenset({"model:invoke"}),
                allowed_models=frozenset({"fake-model"}),
            ),
            "deny-token": PrincipalConfig(
                "tenant",
                "denied",
                frozenset({"model:invoke"}),
                allowed_models=frozenset(),
            ),
            "asset-token": PrincipalConfig(
                "tenant", "asset", frozenset({"asset:read"})
            ),
        }
    )
    app = create_app(
        settings,
        live_config_root=catalog_project(tmp_path),
        discover_providers=False,
    )
    app.state.registry.register(FakeProvider())
    app.state.registry.register(FakeRetrievalProvider())
    app.state.registry.register(BrokenCapabilitiesProvider())
    return app


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_catalog_requires_auth_and_filters_by_key_scope_and_whitelist(
    tmp_path: Path,
) -> None:
    app = catalog_app(tmp_path)
    with TestClient(app) as client:
        unauthenticated = client.get("/model/models")
        all_models = client.get("/model/models", headers=auth("all-token"))
        llm_models = client.get(
            "/model/models", headers=auth("all-token"), params={"task": "llm"}
        )
        embeddings = client.get(
            "/model/models", headers=auth("embedding-token")
        )
        limited = client.get("/model/models", headers=auth("limited-token"))
        denied = client.get("/model/models", headers=auth("deny-token"))
        unrelated_scope = client.get(
            "/model/models", headers=auth("asset-token")
        )

    assert unauthenticated.status_code == 401
    assert all_models.status_code == 200
    payload = all_models.json()
    assert payload["object"] == "kemo.model_list"
    assert payload["count"] == 4
    assert [item["id"] for item in payload["data"]] == [
        "broken-model",
        "fake-model",
        "retrieval-embed-v1",
        "retrieval-rerank-v1",
    ]
    assert payload["data"][0]["capabilities_available"] is False
    assert payload["data"][0]["task"] == "unknown"
    fake = payload["data"][1]
    assert fake["provider_id"] == "fake"
    assert fake["provider_model"] == "model"
    assert fake["capabilities_url"] == "/model/models/fake-model/capabilities"
    assert all_models.headers["cache-control"] == "no-store"
    assert all_models.headers["vary"] == "Authorization"
    assert [item["id"] for item in llm_models.json()["data"]] == ["fake-model"]
    assert [item["id"] for item in embeddings.json()["data"]] == [
        "retrieval-embed-v1"
    ]
    assert [item["id"] for item in limited.json()["data"]] == ["fake-model"]
    assert denied.json()["data"] == []
    assert unrelated_scope.json()["data"] == []


def test_compatible_list_and_both_capability_routes_share_policy(tmp_path: Path) -> None:
    app = catalog_app(tmp_path)
    with TestClient(app) as client:
        compatible = client.get("/v1/models", headers=auth("limited-token"))
        query_style = client.get(
            "/model/capabilities",
            headers=auth("limited-token"),
            params={"model": "fake-model"},
        )
        path_style = client.get(
            "/model/models/fake-model/capabilities",
            headers=auth("limited-token"),
        )
        blocked_by_whitelist = client.get(
            "/model/models/retrieval-embed-v1/capabilities",
            headers=auth("limited-token"),
        )
        blocked_by_scope = client.get(
            "/model/models/fake-model/capabilities",
            headers=auth("embedding-token"),
        )
        broken = client.get(
            "/model/models/broken-model/capabilities",
            headers=auth("all-token"),
        )

    assert compatible.json() == {
        "object": "list",
        "data": [
            {"id": "fake-model", "object": "model", "created": 0, "owned_by": "fake"}
        ],
    }
    assert compatible.headers["cache-control"] == "no-store"
    assert query_style.json() == path_style.json()
    assert path_style.json()["task"] == "llm"
    assert blocked_by_whitelist.status_code == 403
    assert blocked_by_whitelist.json()["error"]["code"] == "MODEL_NOT_ALLOWED"
    assert blocked_by_scope.status_code == 403
    assert blocked_by_scope.json()["error"]["code"] == "MODEL_TASK_NOT_ALLOWED"
    assert broken.status_code == 502
    assert broken.json()["error"]["code"] == "CAPABILITIES_UNAVAILABLE"
    assert "secret provider failure" not in broken.text


def test_catalog_excludes_globally_disabled_models(tmp_path: Path) -> None:
    root = catalog_project(tmp_path)
    write_json(
        root / "core" / "live_control.json",
        {
            "highest_priority_system_prompt": "",
            "disabled_providers": [],
            "disabled_models": ["retrieval-rerank-v1"],
        },
    )
    app = create_app(
        Settings(
            api_keys={
                "all-token": PrincipalConfig(
                    "tenant", "agent", frozenset({"model:invoke"})
                )
            }
        ),
        live_config_root=root,
        discover_providers=False,
    )
    app.state.registry.register(FakeRetrievalProvider())

    with TestClient(app) as client:
        response = client.get("/model/models", headers=auth("all-token"))

    assert [item["id"] for item in response.json()["data"]] == [
        "retrieval-embed-v1"
    ]


def test_non_model_scope_cannot_invoke_llm(tmp_path: Path) -> None:
    app = catalog_app(tmp_path)
    body = request(stream=False).model_dump(mode="json")
    headers = {
        **auth("asset-token"),
        "X-Kemo-Protocol-Version": "1.0",
        "Idempotency-Key": body["request_id"],
    }
    with TestClient(app) as client:
        response = client.post("/model/responses", headers=headers, json=body)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MODEL_TASK_NOT_ALLOWED"
