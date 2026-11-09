from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.server import create_app
from tests.support.gateway import install_fixtures
from core.config import PrincipalConfig, Settings
from core.models import ModelCapabilities
from tests.support.project import project
from tests.support.llm import FakeProvider, request
from tests.support.retrieval import FakeRetrievalProvider


class BrokenCapabilitiesProvider:
    provider_id = "broken"

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"broken-model"})

    async def capabilities(self, model: str) -> ModelCapabilities:
        del model
        raise RuntimeError("secret provider failure")


def catalog_project(tmp_path: Path) -> Path:
    return project(tmp_path)


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
        )
    install_fixtures(app.state.registry, FakeProvider(), FakeRetrievalProvider())
    return app


def auth(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Kemo-Protocol-Version": "2.0",
    }


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
    assert payload["count"] == 3
    assert [item["id"] for item in payload["data"]] == [
        "fake-model",
        "retrieval-embed-v1",
        "retrieval-rerank-v1",
    ]
    assert all(item["capabilities_available"] for item in payload["data"])
    fake = payload["data"][0]
    assert fake["provider_id"] == "gateway"
    assert fake["provider_model"] == "fake-model"
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
        unknown = client.get(
            "/model/capabilities",
            headers=auth("all-token"),
            params={"model": "missing-model"},
        )

    assert compatible.json() == {
        "object": "list",
        "data": [
            {"id": "fake-model", "object": "model", "created": 0, "owned_by": "gateway"}
        ],
    }
    assert compatible.headers["cache-control"] == "no-store"
    assert query_style.json() == path_style.json()
    assert path_style.json()["task"] == "llm"
    assert blocked_by_whitelist.status_code == 403
    assert blocked_by_whitelist.json()["error"]["code"] == "PERMISSION_DENIED"
    assert blocked_by_whitelist.json()["error"]["details"]["kind"] == "model_not_allowed"
    assert blocked_by_scope.status_code == 403
    assert blocked_by_scope.json()["error"]["code"] == "PERMISSION_DENIED"
    assert blocked_by_scope.json()["error"]["details"]["kind"] == "model_task_not_allowed"
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "MODEL_NOT_FOUND"


def test_catalog_excludes_globally_disabled_models(tmp_path: Path) -> None:
    root = catalog_project(tmp_path)
    app = create_app(
        Settings(
            api_keys={
                "all-token": PrincipalConfig(
                    "tenant", "agent", frozenset({"model:invoke"})
                )
            }
        ),
        live_config_root=root,
        )
    install_fixtures(app.state.registry, FakeRetrievalProvider(), disabled=("retrieval-rerank-v1",))

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
        "X-Kemo-Protocol-Version": "2.0",
        "Idempotency-Key": body["request_id"],
    }
    with TestClient(app) as client:
        response = client.post("/model/responses", headers=headers, json=body)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert response.json()["error"]["details"]["kind"] == "model_task_not_allowed"
