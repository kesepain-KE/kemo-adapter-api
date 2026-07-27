"""Authenticated model discovery and capability declarations."""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from api.dependencies import get_registry
from api.middleware import (
    Principal,
    authenticated_principal,
    can_access_model_task,
    ensure_model_allowed,
    ensure_model_task_allowed,
)
from core.models import (
    CompatibleModelItem,
    CompatibleModelList,
    ModelCapabilities,
    ModelCatalogItem,
    ModelCatalogResponse,
)
from core.registry import ProviderRegistry


router = APIRouter(tags=["models"])
ModelTask = Literal["llm", "embedding", "rerank"]


def _private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"


def _catalog_item(
    *,
    model: str,
    provider_id: str,
    task: ModelTask | Literal["unknown"],
    capabilities_available: bool,
) -> ModelCatalogItem:
    prefix = f"{provider_id}-"
    provider_model = model[len(prefix) :] if model.startswith(prefix) else model
    return ModelCatalogItem(
        id=model,
        provider_id=provider_id,
        provider_model=provider_model,
        task=task,
        capabilities_available=capabilities_available,
        capabilities_url=f"/model/models/{quote(model, safe='')}/capabilities",
    )


async def _load_capabilities(
    model: str,
    principal: Principal,
    registry: ProviderRegistry,
) -> ModelCapabilities:
    ensure_model_allowed(principal, model)
    try:
        package = registry.resolve(model)
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "MODEL_NOT_FOUND", "message": f"未知或不可用模型: {model}"},
        ) from exc
    try:
        declared = await package.capabilities(model)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "CAPABILITIES_UNAVAILABLE",
                "message": "Provider 暂时无法提供模型能力声明",
            },
        ) from exc
    if declared.model != model:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "INVALID_CAPABILITIES",
                "message": "Provider 返回了不匹配的模型能力声明",
            },
        )
    ensure_model_task_allowed(principal, declared.task)
    return declared


async def _visible_models(
    principal: Principal,
    registry: ProviderRegistry,
    task_filter: ModelTask | None = None,
) -> list[ModelCatalogItem]:
    items: list[ModelCatalogItem] = []
    broad_model_access = bool(principal.scopes.intersection({"owner", "model:invoke"}))
    for provider_id, registered_package in sorted(registry.providers.items()):
        for model in sorted(registered_package.models):
            if principal.allowed_models is not None and model not in principal.allowed_models:
                continue
            try:
                package = registry.resolve(model)
            except LookupError:
                continue
            try:
                declared = await package.capabilities(model)
                if declared.model != model:
                    raise ValueError("capabilities model mismatch")
            except Exception:
                # A broken declaration must not break discovery for every other Provider.
                # Task-specific keys cannot safely be shown a model whose task is unknown.
                if broad_model_access and task_filter is None:
                    items.append(
                        _catalog_item(
                            model=model,
                            provider_id=provider_id,
                            task="unknown",
                            capabilities_available=False,
                        )
                    )
                continue
            if task_filter is not None and declared.task != task_filter:
                continue
            if not can_access_model_task(principal, declared.task):
                continue
            items.append(
                _catalog_item(
                    model=model,
                    provider_id=provider_id,
                    task=declared.task,
                    capabilities_available=True,
                )
            )
    return items


@router.get("/model/models", response_model=ModelCatalogResponse)
async def list_models(
    response: Response,
    task: ModelTask | None = Query(default=None),
    principal: Principal = Depends(authenticated_principal),
    registry: ProviderRegistry = Depends(get_registry),
) -> ModelCatalogResponse:
    """Return only models the authenticated gateway key may actually invoke."""
    _private_response(response)
    data = await _visible_models(principal, registry, task)
    return ModelCatalogResponse(count=len(data), data=data)


@router.get("/v1/models", response_model=CompatibleModelList)
async def list_models_compatible(
    response: Response,
    principal: Principal = Depends(authenticated_principal),
    registry: ProviderRegistry = Depends(get_registry),
) -> CompatibleModelList:
    """Common model-list shape for clients that probe ``GET /v1/models``."""
    _private_response(response)
    visible = await _visible_models(principal, registry)
    return CompatibleModelList(
        data=[
            CompatibleModelItem(id=item.id, owned_by=item.provider_id)
            for item in visible
        ]
    )


@router.get(
    "/model/models/{model}/capabilities",
    response_model=ModelCapabilities,
)
async def model_capabilities(
    response: Response,
    model: str = Path(min_length=1),
    principal: Principal = Depends(authenticated_principal),
    registry: ProviderRegistry = Depends(get_registry),
) -> ModelCapabilities:
    _private_response(response)
    return await _load_capabilities(model, principal, registry)


@router.get("/model/capabilities", response_model=ModelCapabilities)
async def capabilities(
    response: Response,
    model: str = Query(min_length=1),
    principal: Principal = Depends(authenticated_principal),
    registry: ProviderRegistry = Depends(get_registry),
) -> ModelCapabilities:
    """Backward-compatible query-style capability endpoint."""
    _private_response(response)
    return await _load_capabilities(model, principal, registry)
